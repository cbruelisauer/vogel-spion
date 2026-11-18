# Vogel-Spion

Web camera for a Raspberry Pi Zero W + Camera Module 3 (NoIR) in a nest box.
Open `http://<pi-name>.local:8000/` in a browser on the home network:

- **Live view** (MJPEG, 1280×720 @ 10 fps, hardware encoded)
- **Take photo**: full 12 MP still, shown in the browser to save on the PC (nothing stored on the Pi)
- **Focus**: continuous autofocus or a fixed lens position (slider)

The camera is only switched on while the page is visible. Hiding the tab or minimising
the window closes the stream, and the Pi turns the camera off 3 s later.

## Install on the Pi (Raspberry Pi OS Bookworm Lite)

On the Pi:

```bash
sudo apt-get install -y git
git clone https://github.com/cbruelisauer/vogel-spion.git
cd vogel-spion && ./install.sh
```

`install.sh` installs `python3-picamera2` if missing, copies the app to `/opt/vogel-spion`,
and enables the `vogel-spion` systemd service. To update: `git pull && ./install.sh`.

Logs: `journalctl -u vogel-spion -f`

`install.sh` also hardens the Pi for running unattended (files in [system/](system/)):

- **Wi-Fi power save off.** Power saving makes the Pi Zero W drop off the network.
- **Persistent logs** (max 50 MB), so `journalctl -b -1` shows what happened before a crash or reboot.
- **Hardware watchdog:** the Pi reboots by itself if the system hangs.
- **Wi-Fi watchdog:** if the router doesn't answer, Wi-Fi is restarted every 3 minutes, and
  the Pi reboots after 15 minutes without a connection. See `journalctl -t wifi-watchdog`.

Booting takes about 2.5 minutes on a Pi Zero W before it is reachable.

## Settings

Edit `/etc/default/vogel-spion`, then run `sudo systemctl restart vogel-spion`. See
[vogel-spion.default](vogel-spion.default) for all options (resolution, fps, focus, flip, …).

## IR light (optional)

Not needed for the app to work. Once an IR light is connected through a MOSFET to a GPIO pin,
set `VOGEL_IR_GPIO=<BCM pin>`. The light is then on exactly while the camera is on.
If the variable is empty, or the GPIO can't be used, the app runs without it.
