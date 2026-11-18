#!/usr/bin/env bash
# Install/update vogel-spion on the Raspberry Pi. Run from this folder on the Pi.
set -euo pipefail
cd "$(dirname "$0")"

if ! python3 -c 'import picamera2' 2>/dev/null; then
  sudo apt-get update
  sudo apt-get install -y --no-install-recommends python3-picamera2
fi

sudo install -d /opt/vogel-spion
sudo install -m 755 vogel_spion.py /opt/vogel-spion/
if [ ! -f /etc/default/vogel-spion ]; then
  sudo install -m 644 vogel-spion.default /etc/default/vogel-spion
fi
sed "s/@USER@/${SUDO_USER:-$USER}/" vogel-spion.service | sudo tee /etc/systemd/system/vogel-spion.service >/dev/null
# Robustness for an unattended Pi: Wi-Fi power save off, persistent logs, watchdogs.
sudo install -D -m 644 system/wifi-powersave-off.conf /etc/NetworkManager/conf.d/wifi-powersave-off.conf
sudo install -D -m 644 system/journald-persistent.conf /etc/systemd/journald.conf.d/persistent.conf
sudo install -D -m 644 system/watchdog.conf /etc/systemd/system.conf.d/watchdog.conf
sudo install -m 755 system/wifi-watchdog.sh /usr/local/sbin/wifi-watchdog.sh
sudo install -m 644 system/wifi-watchdog.service system/wifi-watchdog.timer /etc/systemd/system/
sudo install -d /var/log/journal
sudo systemctl restart systemd-journald
sudo journalctl --flush  # start writing to /var/log/journal now, not only after a reboot
sudo systemctl daemon-reexec  # applies the hardware watchdog setting
sudo systemctl reload NetworkManager
sudo /sbin/iw dev wlan0 set power_save off 2>/dev/null || true  # apply now without reconnecting

sudo systemctl daemon-reload
sudo systemctl enable --now wifi-watchdog.timer
sudo systemctl enable vogel-spion
sudo systemctl restart vogel-spion
echo "vogel-spion running: http://$(hostname).local:$(. /etc/default/vogel-spion; echo "${VOGEL_PORT:-8000}")/"
