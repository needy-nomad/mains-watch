#!/bin/sh
# Install and enable mains-watch (starts on every boot).
#   sudo ./install.sh                 first install: guided setup finds the sensor for you
#   sudo ./install.sh --setup         run the guided setup again
#   sudo ./install.sh <SENSOR_IP>     skip the wizard, use this IP
#   sudo ./install.sh                 (later) reinstall code, keep existing config
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

SETUP=0
[ "${1:-}" = "--setup" ] && { SETUP=1; shift; }
SENSOR="${1:-}"
if [ -n "$SENSOR" ]; then
    if [ -f "$CONF" ] && grep -q '^SENSOR_IP=' "$CONF"; then
        sed -i "s|^SENSOR_IP=.*|SENSOR_IP=$SENSOR|" "$CONF"
    else
        echo "SENSOR_IP=$SENSOR" >> "$CONF"
    fi
fi
if [ "$SETUP" = 1 ] || ! grep -q '^SENSOR_IP=.\+' "$CONF" 2>/dev/null; then
    python3 "$HERE/onboard.py" "$CONF" || exit 1
fi
grep -q '^SENSOR_IP=.\+' "$CONF" 2>/dev/null || { echo "give the sensor IP: sudo $0 <SENSOR_IP>" >&2; exit 1; }

install -d /opt/mains-watch
install -m 755 "$HERE/mains_watch.py" "$HERE/gpu_guard.py" "$HERE/dashboard.py" "$HERE/onboard.py" /opt/mains-watch/
install -m 644 "$HERE/mains-watch.service" /etc/systemd/system/mains-watch.service
install -m 644 "$HERE/mains-watch.desktop" /usr/share/applications/mains-watch.desktop
systemctl daemon-reload
systemctl enable mains-watch.service
systemctl restart mains-watch.service
sleep 2
systemctl --no-pager --full status mains-watch.service | head -15
echo; echo "Dashboard: http://127.0.0.1:8099  (or the Mains Watch app in the menu)"
