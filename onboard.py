#!/usr/bin/env python3
"""Setup backend for the dashboard's /setup page: finds the sensor by watching which
device disappears when it is unplugged, then writes the config and restarts the service."""

import ipaddress
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from netutil import local_network, ping  # noqa: F401  (re-exported for callers/tests)


def scan(network, ping_fn=ping) -> set:
    """Addresses that answer ping, probed twice so one lost packet doesn't hide a device."""
    hosts = [str(h) for h in network.hosts()]
    alive = set()
    with ThreadPoolExecutor(max_workers=64) as pool:
        for _ in range(2):
            alive |= {h for h, ok in zip(hosts, pool.map(ping_fn, hosts)) if ok}
    return alive


def vanished(before: set, after: set, ignore: set) -> list:
    """Devices seen before the unplug and gone after, sorted by address."""
    return sorted(before - after - ignore, key=ipaddress.ip_address)


def set_key(path: str, key: str, value) -> None:
    """Set KEY=value in a KEY=VALUE config file, keeping everything else."""
    p = Path(path)
    lines = p.read_text().splitlines() if p.exists() else []
    new = f"{key}={value}"
    for i, line in enumerate(lines):
        if re.match(rf"\s*{key}\s*=", line):
            lines[i] = new
            break
    else:
        lines.append(new)
    p.write_text("\n".join(lines) + "\n")



class Setup:
    """State for one browser setup session. Methods return JSON-able dicts."""

    def __init__(self, conf: str, restart, ping_fn=ping, network=None, settle: float = 4.0) -> None:
        self.conf, self.restart, self.ping_fn, self.settle = conf, restart, ping_fn, settle
        self.net_info = network or local_network()
        self.before: set = set()

    def _ignore(self) -> set:
        return {self.net_info[0], self.net_info[1]} if self.net_info else set()

    def scan(self) -> dict:
        if not self.net_info:
            return {"error": "No network connection found. Connect to Wi-Fi/LAN first."}
        self.before = scan(self.net_info[2], self.ping_fn) - self._ignore()
        return {"found": sorted(self.before, key=ipaddress.ip_address), "router": self.net_info[0]}

    def unplugged(self) -> dict:
        if not self.before:
            return {"error": "Run the scan first."}
        time.sleep(self.settle)                     # let the device drop off the network
        after = scan(self.net_info[2], self.ping_fn)
        return {"gone": vanished(self.before, after, self._ignore())}

    def check(self, ip: str) -> dict:
        return {"online": self._valid_ip(ip) and self.ping_fn(ip)}

    def _valid_ip(self, ip) -> bool:
        try:
            return ipaddress.ip_address(ip).version == 4
        except ValueError:
            return False

    def config(self) -> dict:
        values = {}
        try:
            for line in Path(self.conf).read_text().splitlines():
                line = line.split("#")[0]
                if "=" in line:
                    k, v = line.split("=", 1)
                    values[k.strip()] = v.strip()
        except OSError:
            pass
        return values

    def save(self, data: dict) -> dict:
        try:
            sensor = str(data["sensor"]).strip()
            cap, every, confirm = float(data["cap"]), float(data["every"]), int(data["confirm"])
        except (KeyError, ValueError, TypeError):
            return {"error": "Invalid values."}
        if not self._valid_ip(sensor) or sensor in self._ignore():
            return {"error": "Sensor must be a valid IPv4 address, not the router or this PC."}
        if not (50 <= cap <= 600 and 0.2 <= every <= 60 and 1 <= confirm <= 20):
            return {"error": "Out of range: cap 50-600 W, interval 0.2-60 s, misses 1-20."}
        for key, value in (("SENSOR_IP", sensor), ("GPU_CAP_WATTS", cap), ("CHECK_EVERY", every),
                           ("OUTAGE_CONFIRM", confirm)):
            set_key(self.conf, key, value)
        threading.Timer(1.0, self.restart).start()  # systemd brings us back with the new config
        return {"ok": True}
