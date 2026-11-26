#!/usr/bin/env bash
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Please run with sudo"; exit 1; }
systemctl disable --now jlscreen.service 2>/dev/null || true
rm -f /etc/systemd/system/jlscreen.service /etc/udev/rules.d/99-jlscreen.rules /usr/local/bin/jlscreen
rm -rf /opt/jlscreen
systemctl daemon-reload; udevadm control --reload-rules
echo "Removed (config left at /etc/jlscreen.conf)."
