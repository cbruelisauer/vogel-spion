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
sudo systemctl daemon-reload
sudo systemctl enable vogel-spion
sudo systemctl restart vogel-spion
echo "vogel-spion running: http://$(hostname).local:$(. /etc/default/vogel-spion; echo "${VOGEL_PORT:-8000}")/"
