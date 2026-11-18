#!/usr/bin/env python3
"""vogel-spion: on-demand camera web server for a Raspberry Pi + Camera Module 3.

The camera is only opened (and powered) while a browser is watching the live
stream or a photo is being taken. When the last viewer leaves, the camera is
closed again after a short grace period.

An IR light on a GPIO pin is optional: set VOGEL_IR_GPIO to enable it. Without
it (or if the GPIO library is missing) everything works the same, minus the light.

Configuration is via environment variables (see CONFIG below).
"""

import io
import json
import logging
import os
import signal
import threading
import time
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

log = logging.getLogger("vogel-spion")


# --------------------------------------------------------------------------- config

def _env_size(name, default):
    value = os.environ.get(name, default).strip().lower()
    if not value:
        return None
    w, h = value.split("x")
    return int(w), int(h)


def _env_focus(name, default):
    value = os.environ.get(name, default).strip().lower()
    return None if value in ("", "auto") else float(value)


CONFIG = {
    "port": int(os.environ.get("VOGEL_PORT", "8000")),
    "stream_size": _env_size("VOGEL_STREAM_SIZE", "1280x720"),
    "stream_fps": float(os.environ.get("VOGEL_STREAM_FPS", "10")),
    # Live view data rate in bit/s. ~7 Mbit/s overloaded a busy 2.4 GHz network.
    "stream_bitrate": int(float(os.environ.get("VOGEL_STREAM_BITRATE", "2500000"))),
    # Empty = full sensor resolution; falls back to FALLBACK_STILL_SIZE if memory is short.
    "still_size": _env_size("VOGEL_STILL_SIZE", ""),
    "still_quality": int(os.environ.get("VOGEL_STILL_QUALITY", "90")),
    # "auto" = continuous autofocus, or a lens position in dioptres (1/metres, e.g. 8 ≈ 12.5 cm).
    "focus": _env_focus("VOGEL_FOCUS", "auto"),
    "flip": os.environ.get("VOGEL_FLIP", "0") == "1",  # rotate 180°
    "stop_delay": float(os.environ.get("VOGEL_STOP_DELAY", "3")),
    "ir_gpio": os.environ.get("VOGEL_IR_GPIO", "").strip(),
}

FALLBACK_STILL_SIZE = (2304, 1296)  # Camera Module 3 binned full-FoV mode


# --------------------------------------------------------------------------- IR light

class IRLight:
    """Optional IR light on a GPIO pin. A no-op if not configured or unavailable."""

    def __init__(self, pin):
        self._device = None
        if not pin:
            log.info("IR light: not configured")
            return
        try:
            from gpiozero import OutputDevice
            self._device = OutputDevice(int(pin), initial_value=False)
            log.info("IR light: GPIO %s", pin)
        except Exception as exc:  # missing library, wrong pin, no permission...
            log.warning("IR light disabled (GPIO %s unavailable: %s)", pin, exc)

    @property
    def present(self):
        return self._device is not None

    def on(self):
        if self._device:
            self._device.on()

    def off(self):
        if self._device:
            self._device.off()


# --------------------------------------------------------------------------- camera

class FrameBuffer(io.BufferedIOBase):
    """Receives MJPEG frames from the encoder and hands the newest one to viewers."""

    def __init__(self):
        self.frame = None
        self.seq = 0
        self._cond = threading.Condition()

    def write(self, buf):
        with self._cond:
            self.frame = bytes(buf)
            self.seq += 1
            self._cond.notify_all()
        return len(buf)

    def wait_newer(self, seq, timeout):
        with self._cond:
            self._cond.wait_for(lambda: self.seq != seq, timeout)
            return self.frame, self.seq


class Camera:
    """Owns the Picamera2 instance; opens it only while it is actually needed."""

    def __init__(self, config, ir):
        from picamera2 import Picamera2  # imported here so --help etc. work without it
        from picamera2.encoders import MJPEGEncoder
        from picamera2.outputs import FileOutput
        from libcamera import Transform, controls

        self._Picamera2 = Picamera2
        self._MJPEGEncoder = MJPEGEncoder
        self._FileOutput = FileOutput
        self._controls = controls
        self._transform = Transform(hflip=1, vflip=1) if config["flip"] else Transform()

        self.cfg = config
        self.ir = ir
        self.buffer = FrameBuffer()
        self.focus = config["focus"]
        self._lock = threading.RLock()
        self._picam = None
        self._streaming = False
        self._busy = None  # "photo" while a still is being taken
        self._viewers = 0
        self._stop_generation = 0

        # Probe once at startup to learn the camera's capabilities, then release it.
        self._open()
        self.model = self._picam.camera_properties.get("Model", "unknown")
        self.has_af = "AfMode" in self._picam.camera_controls
        lens = self._picam.camera_controls.get("LensPosition")
        self.lens_range = (lens[0], lens[1]) if lens else None
        self._close()
        log.info("Camera: %s, autofocus: %s, lens range: %s", self.model, self.has_af, self.lens_range)

    # -- low level -----------------------------------------------------------

    def _open(self):
        if self._picam is None:
            self._picam = self._Picamera2()
            self.ir.on()
            log.info("Camera on")

    def _close(self):
        if self._picam is not None:
            try:
                self._picam.close()
            finally:
                self._picam = None
                self._streaming = False
                self.ir.off()
                log.info("Camera off")

    def _apply_focus(self):
        if not self.has_af:
            return
        c = self._controls.AfModeEnum
        if self.focus is None:
            self._picam.set_controls({"AfMode": c.Continuous})
        else:
            self._picam.set_controls({"AfMode": c.Manual, "LensPosition": self.focus})

    def _start_stream(self):
        self._open()
        size = self.cfg["stream_size"]
        video = self._picam.create_video_configuration(
            main={"size": size, "format": "YUV420"},
            controls={"FrameRate": self.cfg["stream_fps"]},
            transform=self._transform,
            buffer_count=4,
        )
        self._picam.configure(video)
        self._apply_focus()
        bitrate = self.cfg["stream_bitrate"]
        self._picam.start_recording(self._MJPEGEncoder(bitrate=bitrate), self._FileOutput(self.buffer))
        self._streaming = True
        log.info("Streaming %sx%s @ %s fps, %.1f Mbit/s",
                 size[0], size[1], self.cfg["stream_fps"], bitrate / 1e6)

    def _stop_stream(self):
        if self._streaming:
            self._picam.stop_recording()
            self._streaming = False

    # -- viewers ---------------------------------------------------------------

    def add_viewer(self):
        with self._lock:
            self._viewers += 1
            self._stop_generation += 1  # cancels any pending idle stop
            if not self._streaming and self._busy is None:
                self._start_stream()

    def remove_viewer(self):
        with self._lock:
            self._viewers -= 1
            if self._viewers > 0:
                return
            self._stop_generation += 1
            generation = self._stop_generation
        timer = threading.Timer(self.cfg["stop_delay"], self._idle_stop, args=(generation,))
        timer.daemon = True
        timer.start()

    def _idle_stop(self, generation):
        with self._lock:
            if generation == self._stop_generation and self._viewers == 0:
                self._stop_stream()
                self._close()

    # -- still photo -------------------------------------------------------------

    def snapshot(self):
        with self._lock:
            self._busy = "photo"
            try:
                self._stop_stream()
                self._open()
                size = self.cfg["still_size"] or self._picam.sensor_resolution
                try:
                    return self._capture_still(size)
                except Exception as exc:
                    if tuple(size) == FALLBACK_STILL_SIZE:
                        raise
                    log.warning("Still at %sx%s failed (%s), retrying at %sx%s",
                                size[0], size[1], exc, *FALLBACK_STILL_SIZE)
                    self._close()  # start from a clean state
                    self._open()
                    return self._capture_still(FALLBACK_STILL_SIZE)
            finally:
                self._busy = None
                if self._viewers > 0:  # resume (or start) the stream for anyone watching
                    try:
                        self._start_stream()
                    except Exception:
                        log.exception("Could not resume stream after photo")
                        self._close()
                else:
                    self._close()

    def _capture_still(self, size):
        t0 = time.monotonic()
        still = self._picam.create_still_configuration(
            main={"size": tuple(size)}, transform=self._transform, buffer_count=1
        )
        self._picam.configure(still)
        self._picam.options["quality"] = self.cfg["still_quality"]
        self._picam.start()
        try:
            if self.has_af and self.focus is None:
                self._picam.autofocus_cycle()
            else:
                self._apply_focus()
            time.sleep(1.0)  # let exposure / white balance settle
            out = io.BytesIO()
            self._picam.capture_file(out, format="jpeg")
        finally:
            self._picam.stop()
        log.info("Photo %sx%s, %d kB, %.1f s", size[0], size[1], out.tell() // 1024, time.monotonic() - t0)
        return out.getvalue()

    # -- focus / status ------------------------------------------------------------

    def set_focus(self, position):
        """position: None for autofocus, otherwise lens position in dioptres."""
        with self._lock:
            if position is not None and self.lens_range:
                position = max(self.lens_range[0], min(self.lens_range[1], position))
            self.focus = position
            if self._streaming:
                self._apply_focus()

    def status(self):
        return {
            "model": self.model,
            "camera_on": self._picam is not None,
            "streaming": self._streaming,
            "busy": self._busy,
            "viewers": self._viewers,
            "has_af": self.has_af,
            "lens_range": self.lens_range,
            "focus": self.focus,
            "ir_light": self.ir.present,
            "cpu_temp": _cpu_temp(),
        }

    def shutdown(self):
        with self._lock:
            self._stop_stream()
            self._close()


def _cpu_temp():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return round(int(f.read()) / 1000, 1)
    except OSError:
        return None


# --------------------------------------------------------------------------- web

INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Vogel-Spion</title>
<style>
  :root { color-scheme: light dark; --bg:#f4f2ec; --fg:#1d1f1c; --muted:#6b6f68; --card:#fff; --accent:#3b6e3a; --line:#d9d6cc; }
  @media (prefers-color-scheme: dark) { :root { --bg:#151714; --fg:#e8e6df; --muted:#9a9d95; --card:#1f221e; --accent:#8fc28c; --line:#33372f; } }
  * { box-sizing: border-box; }
  body { margin:0; font:15px/1.4 system-ui, sans-serif; background:var(--bg); color:var(--fg); }
  main { max-width: 980px; margin: 0 auto; padding: 16px; }
  header { display:flex; align-items:baseline; justify-content:space-between; gap:12px; flex-wrap:wrap; }
  h1 { font-size: 20px; margin: 0 0 12px; }
  #status { color: var(--muted); font-size: 13px; }
  .view { position:relative; background:#000; border-radius:10px; overflow:hidden; aspect-ratio:16/9; }
  .view img { width:100%; height:100%; object-fit:contain; display:block; }
  .overlay { position:absolute; inset:0; display:flex; align-items:center; justify-content:center; color:#ddd; background:rgba(0,0,0,.55); font-size:15px; }
  .overlay[hidden] { display:none; }
  .controls { display:flex; flex-wrap:wrap; gap:12px 20px; align-items:center; margin:14px 0; padding:12px 14px; background:var(--card); border:1px solid var(--line); border-radius:10px; }
  button { font:inherit; padding:8px 16px; border-radius:8px; border:1px solid var(--accent); background:var(--accent); color:var(--bg); cursor:pointer; }
  button:disabled { opacity:.5; cursor:wait; }
  .focus { display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
  .focus input[type=range] { width: 200px; }
  #photo { margin-top: 14px; }
  #photo img { max-width:100%; border-radius:10px; display:block; }
  #photo a { display:inline-block; margin-top:8px; color:var(--accent); }
</style>
</head>
<body>
<main>
  <header><h1>Vogel-Spion</h1><span id="status">connecting…</span></header>
  <div class="view">
    <img id="live" alt="Live view">
    <div class="overlay" id="overlay">Starting camera…</div>
  </div>
  <div class="controls">
    <button id="snap">Take photo</button>
    <div class="focus" id="focusbox" hidden>
      <label><input type="checkbox" id="af" checked> Autofocus</label>
      <input type="range" id="lens" min="0" max="15" step="0.1" value="8" disabled>
      <span id="lensval"></span>
    </div>
  </div>
  <div id="photo"></div>
</main>
<script>
const BLANK = 'data:image/gif;base64,R0lGODlhAQABAAAAACw=';
const live = document.getElementById('live');
const overlay = document.getElementById('overlay');
const statusEl = document.getElementById('status');
const snap = document.getElementById('snap');
const af = document.getElementById('af');
const lens = document.getElementById('lens');
const lensval = document.getElementById('lensval');
let retry = null, focusInit = false, wanted = false;

function showOverlay(text) { overlay.textContent = text; overlay.hidden = !text; }

// The camera on the Pi runs only while this stream is open.
function startStream() {
  clearTimeout(retry);
  wanted = true;
  showOverlay('Starting camera…');
  live.src = '/stream.mjpg?t=' + Date.now();
}
function stopStream() {
  clearTimeout(retry);
  wanted = false;
  live.src = BLANK;  // aborts the stream request -> Pi turns camera off
  showOverlay('Paused (tab hidden)');
}
live.addEventListener('load', () => { if (live.src !== BLANK) { showOverlay(''); refresh(); } });
live.addEventListener('error', () => {
  if (!wanted || document.hidden) return;
  showOverlay('Connection lost, retrying…');
  retry = setTimeout(startStream, 3000);
});
document.addEventListener('visibilitychange', () => document.hidden ? stopStream() : (startStream(), refresh()));
window.addEventListener('pagehide', stopStream);

function lensText(v) { return v > 0 ? `${v.toFixed(1)} dpt ≈ ${Math.round(100 / v)} cm` : '∞'; }

async function refresh() {
  if (document.hidden) return;
  try {
    const s = await (await fetch('/api/status')).json();
    const parts = [s.model, s.streaming ? 'streaming' : (s.busy ? 'taking photo' : 'camera off')];
    if (s.cpu_temp != null) parts.push(s.cpu_temp + ' °C');
    if (s.ir_light) parts.push('IR light');
    statusEl.textContent = parts.join(' · ');
    if (s.has_af && !focusInit) {
      focusInit = true;
      document.getElementById('focusbox').hidden = false;
      if (s.lens_range) { lens.min = s.lens_range[0]; lens.max = s.lens_range[1]; }
      af.checked = s.focus == null;
      lens.disabled = af.checked;
      if (s.focus != null) lens.value = s.focus;
      lensval.textContent = af.checked ? '' : lensText(+lens.value);
    }
  } catch { statusEl.textContent = 'Pi not reachable'; }
}

async function sendFocus() {
  lens.disabled = af.checked;
  lensval.textContent = af.checked ? '' : lensText(+lens.value);
  await fetch('/api/focus', { method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ position: af.checked ? null : +lens.value }) });
}
af.addEventListener('change', sendFocus);
lens.addEventListener('input', () => lensval.textContent = lensText(+lens.value));
lens.addEventListener('change', sendFocus);

snap.addEventListener('click', async () => {
  snap.disabled = true; snap.textContent = 'Taking photo…';
  try {
    const r = await fetch('/snapshot.jpg', { cache: 'no-store' });
    if (!r.ok) throw new Error(await r.text());
    const url = URL.createObjectURL(await r.blob());
    const name = 'vogel-' + new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-') + '.jpg';
    const box = document.getElementById('photo');
    box.innerHTML = '';
    const img = new Image(); img.src = url; img.alt = 'Photo';
    const a = document.createElement('a'); a.href = url; a.download = name; a.textContent = 'Save ' + name;
    box.append(img, a);
  } catch (e) { alert('Photo failed: ' + e.message); }
  finally { snap.disabled = false; snap.textContent = 'Take photo'; }
});

if (!document.hidden) startStream(); else stopStream();
refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    camera: Camera = None  # set in main()
    timeout = 15  # socket timeout: a stalled viewer is dropped after this many seconds
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)

    def _send(self, status, body, ctype, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, status=HTTPStatus.OK):
        self._send(status, json.dumps(data).encode(), "application/json")

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self._send(HTTPStatus.OK, INDEX_HTML.encode(), "text/html; charset=utf-8")
        elif path == "/stream.mjpg":
            self._stream()
        elif path == "/snapshot.jpg":
            self._snapshot()
        elif path == "/api/status":
            self._json(self.camera.status())
        else:
            self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")

    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/api/focus":
            self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length) or b"{}")
            pos = data.get("position")
            self.camera.set_focus(None if pos is None else float(pos))
        except (ValueError, TypeError) as exc:
            self._send(HTTPStatus.BAD_REQUEST, str(exc).encode(), "text/plain")
            return
        self._json(self.camera.status())

    def _snapshot(self):
        try:
            jpeg = self.camera.snapshot()
        except Exception as exc:
            log.exception("Photo failed")
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, f"photo failed: {exc}".encode(), "text/plain")
            return
        name = datetime.now().strftime("vogel-%Y-%m-%d-%H-%M-%S.jpg")
        self._send(HTTPStatus.OK, jpeg, "image/jpeg", {"Content-Disposition": f'inline; filename="{name}"'})

    def _stream(self):
        try:
            self.camera.add_viewer()
        except Exception as exc:
            log.exception("Could not start camera")
            self.camera.remove_viewer()
            self._send(HTTPStatus.SERVICE_UNAVAILABLE, f"camera error: {exc}".encode(), "text/plain")
            return
        log.info("Viewer connected: %s", self.client_address[0])
        self.close_connection = True
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=FRAME")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            seq, idle_since = self.camera.buffer.seq, time.monotonic()
            while True:
                frame, new_seq = self.camera.buffer.wait_newer(seq, 1.0)
                if new_seq == seq:
                    # No frames (e.g. a photo is being taken). Give up if it lasts too long.
                    if time.monotonic() - idle_since > 60:
                        log.warning("No frames for 60 s, closing stream")
                        break
                    continue
                seq, idle_since = new_seq, time.monotonic()
                self.wfile.write(
                    b"--FRAME\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(frame)
                    + frame + b"\r\n"
                )
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            pass  # viewer went away
        finally:
            log.info("Viewer left: %s", self.client_address[0])
            self.camera.remove_viewer()


def main():
    logging.basicConfig(
        level=os.environ.get("VOGEL_LOG", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    ir = IRLight(CONFIG["ir_gpio"])
    camera = Camera(CONFIG, ir)
    Handler.camera = camera

    server = ThreadingHTTPServer(("", CONFIG["port"]), Handler)
    server.daemon_threads = True

    def stop(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log.info("Listening on http://0.0.0.0:%d", CONFIG["port"])
    try:
        server.serve_forever()
    finally:
        camera.shutdown()
        ir.off()


if __name__ == "__main__":
    main()
