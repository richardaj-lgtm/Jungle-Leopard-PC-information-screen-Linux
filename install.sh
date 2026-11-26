#!/usr/bin/env bash
# Installs jlscreen on Ubuntu / Debian. Run from the unpacked folder:  sudo ./install.sh
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Please run with sudo: sudo ./install.sh"; exit 1; }
cd "$(dirname "$0")"

echo "==> Installing packages"
apt-get update -qq
apt-get install -y python3 python3-psutil python3-pil python3-serial lm-sensors fonts-dejavu-core

echo "==> Copying files"
install -d /opt/jlscreen
install -m 755 jlscreen.py /opt/jlscreen/jlscreen.py
ln -sf /opt/jlscreen/jlscreen.py /usr/local/bin/jlscreen
if [ ! -f /etc/jlscreen.conf ]; then
    install -m 644 jlscreen.conf /etc/jlscreen.conf
else
    echo "    keeping your existing /etc/jlscreen.conf"
fi
install -m 644 99-jlscreen.rules /etc/udev/rules.d/99-jlscreen.rules
install -m 644 jlscreen.service /etc/systemd/system/jlscreen.service

echo "==> Loading motherboard sensor drivers (fans)"
# Non-interactive: accept the safe defaults and write /etc/modules entries
yes "" | sensors-detect --auto >/dev/null 2>&1 || true
systemctl restart kmod 2>/dev/null || true

echo "==> Starting service"
udevadm control --reload-rules
udevadm trigger --subsystem-match=tty
systemctl daemon-reload
systemctl enable --now jlscreen.service

echo
echo "Done. Useful commands:"
echo "  jlscreen list                 # is the screen detected?"
echo "  jlscreen sensors              # which temps/fans this PC exposes"
echo "  sudo systemctl stop jlscreen && jlscreen test --rotate 90   # orientation check"
echo "  sudo nano /etc/jlscreen.conf && sudo systemctl restart jlscreen"
echo "  journalctl -u jlscreen -f     # service log"
