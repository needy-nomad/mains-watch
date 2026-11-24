#!/bin/sh
# Install and enable mains-watch (starts on every boot).
#   sudo ./install.sh                 install; finish setup in the browser (opens by itself)
#   sudo ./install.sh <SENSOR_IP>     install and skip the browser setup
#   sudo ./install.sh --uninstall
set -eu
[ "$(id -u)" -eq 0 ] || { echo "run as root: sudo $0 $*" >&2; exit 1; }
HERE=$(cd "$(dirname "$0")" && pwd)
CONF=/etc/mains-watch.conf

if [ "${1:-}" = "--uninstall" ]; then
    systemctl disable --now mains-watch.service 2>/dev/null || true
    rm -f /etc/systemd/system/mains-watch.service
    rm -rf /opt/mains-watch
    rm -f /usr/share/applications/mains-watch.desktop
    systemctl daemon-reload
    echo "removed (config $CONF kept)"
    exit 0
fi

SENSOR="${1:-}"
if [ -n "$SENSOR" ]; then            # optional: skip the browser setup
    if [ -f "$CONF" ] && grep -q '^SENSOR_IP=' "$CONF"; then
        sed -i "s|^SENSOR_IP=.*|SENSOR_IP=$SENSOR|" "$CONF"
    else
        echo "SENSOR_IP=$SENSOR" >> "$CONF"
    fi
fi

install -d /opt/mains-watch
install -m 755 "$HERE/mains_watch.py" "$HERE/gpu_guard.py" "$HERE/dashboard.py" "$HERE/onboard.py" /opt/mains-watch/
install -m 644 "$HERE/mains-watch.service" /etc/systemd/system/mains-watch.service
install -m 644 "$HERE/mains-watch.desktop" /usr/share/applications/mains-watch.desktop
systemctl daemon-reload
systemctl enable mains-watch.service
systemctl restart mains-watch.service
sleep 2
systemctl --no-pager --full status mains-watch.service | head -15
URL=http://127.0.0.1:8099
echo; echo "Dashboard / setup: $URL  (also the Mains Watch app in the menu)"
if [ -n "${SUDO_USER:-}" ] && ! grep -q '^SENSOR_IP=.\+' "$CONF" 2>/dev/null; then
    sudo -u "$SUDO_USER" env DISPLAY="${DISPLAY:-:0}" xdg-open "$URL" >/dev/null 2>&1 || true
fi
