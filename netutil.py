"""Network helpers shared by the daemon and the setup page (Linux and Windows)."""

import ipaddress
import platform
import re
import socket
import subprocess
from typing import Optional

IS_WINDOWS = platform.system() == "Windows"


def ping(ip: str) -> bool:
    """One ICMP ping with a 1 s timeout. True only if the host itself replied."""
    cmd = ["ping", "-n", "1", "-w", "1000", ip] if IS_WINDOWS else ["ping", "-c", "1", "-W", "1", ip]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=4)
    except (subprocess.TimeoutExpired, OSError):
        return False
    if IS_WINDOWS:      # Windows returns 0 even for "Destination host unreachable"; a real reply has TTL=
        return r.returncode == 0 and "TTL=" in r.stdout.upper()
    return r.returncode == 0


def parse_gateway(text: str, windows: bool = IS_WINDOWS) -> Optional[str]:
    """Default gateway from `ip route show default` (Linux) or `route print -4 0.0.0.0` (Windows)."""
    if windows:
        best = None
        for m in re.finditer(r"^\s*0\.0\.0\.0\s+0\.0\.0\.0\s+(\d+\.\d+\.\d+\.\d+)\s+\S+\s+(\d+)\s*$",
                             text, re.M):
            if best is None or int(m.group(2)) < best[0]:
                best = (int(m.group(2)), m.group(1))
        return best[1] if best else None
    parts = text.split()
    return parts[parts.index("via") + 1] if "via" in parts else None


def default_gateway() -> str:
    cmd = ["route", "print", "-4", "0.0.0.0"] if IS_WINDOWS else ["ip", "-4", "route", "show", "default"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return parse_gateway(out) or ""


def own_ip(gateway: str) -> Optional[str]:
    """This PC's address on the route to `gateway` (a UDP connect sends no packets)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((gateway, 9))
            return s.getsockname()[0]
    except OSError:
        return None


def local_network():
    """(gateway, own_ip, /24 network) or None when not connected."""
    gateway = default_gateway()
    own = own_ip(gateway) if gateway else None
    if not own:
        return None
    return gateway, own, ipaddress.ip_network(f"{own}/24", strict=False)
