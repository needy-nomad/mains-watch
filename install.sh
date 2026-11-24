#!/bin/sh
# Install and enable mains-watch on Linux (systemd). Starts on every boot.
#   ./install.sh                 install; finish setup in the browser (opens by itself)
#   ./install.sh <SENSOR_IP>     install and skip the browser setup
#   ./install.sh --check         will it work after a restart?
#   ./install.sh --uninstall
# Not root? It asks for your sudo password itself.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
CONF=/etc/mains-watch.conf

[ "$(uname -s)" = "Linux" ] || { echo "Linux only (Windows: use install.ps1)" >&2; exit 1; }
if [ "$(id -u)" -ne 0 ]; then
    echo "Administrator rights are needed to run at boot and control the GPU; asking sudo..."
    exec sudo -- "$0" "$@"
fi
command -v systemctl >/dev/null || { echo "systemd not found" >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 not found" >&2; exit 1; }
python3 -c 'import sys; sys.exit(sys.version_info < (3, 8))' || { echo "Python 3.8+ needed" >&2; exit 1; }

if [ "${1:-}" = "--check" ]; then
    exec python3 "$HERE/mains_watch.py" --check
fi

if [ "${1:-}" = "--uninstall" ]; then
    systemctl disable --now mains-watch.service 2>/dev/null || true
    rm -f /etc/systemd/system/mains-watch.service
    rm -rf /opt/mains-watch
    rm -f /usr/share/applications/mains-watch.desktop
    systemctl daemon-reload
    echo "removed (config $CONF and history in /var/lib/mains-watch kept)"
    exit 0
fi

SENSOR="${1:-}"
if [ -n "$SENSOR" ]; then            # optional: skip the browser setup
    case "$SENSOR" in *[!0-9.]*) echo "not an IPv4 address: $SENSOR" >&2; exit 1;; esac
    if [ -f "$CONF" ] && grep -q '^SENSOR_IP=' "$CONF"; then
        sed -i "s|^SENSOR_IP=.*|SENSOR_IP=$SENSOR|" "$CONF"
    else
        echo "SENSOR_IP=$SENSOR" >> "$CONF"
    fi
fi

install -d /opt/mains-watch
install -m 755 "$HERE"/mains_watch.py "$HERE"/gpu_guard.py "$HERE"/dashboard.py \
    "$HERE"/onboard.py "$HERE"/netutil.py "$HERE"/selfcheck.py /opt/mains-watch/
install -m 644 "$HERE/mains-watch.service" /etc/systemd/system/mains-watch.service
install -m 644 "$HERE/mains-watch.desktop" /usr/share/applications/mains-watch.desktop
systemctl daemon-reload
systemctl enable mains-watch.service
systemctl restart mains-watch.service
sleep 3
echo
echo "---- restart check ----"
python3 /opt/mains-watch/mains_watch.py --check || true

URL=http://127.0.0.1:8099
echo; echo "Dashboard / setup: $URL  (also the 'Mains Watch' app in the menu)"
if [ -n "${SUDO_USER:-}" ] && ! grep -q '^SENSOR_IP=.\+' "$CONF" 2>/dev/null; then
    sudo -u "$SUDO_USER" env DISPLAY="${DISPLAY:-:0}" xdg-open "$URL" >/dev/null 2>&1 || true
fi
