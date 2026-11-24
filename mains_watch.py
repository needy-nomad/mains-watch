#!/usr/bin/env python3
"""
Mains-power watcher for a Windows or Linux desktop.

Use case: your PC is on a UPS or inverter, but a Wi-Fi router is also on backup
power. A Wi-Fi smart plug or smart device sits in a wall socket that is NOT
backed up. When mains fails, that device drops off the network while the router
keeps answering. The PC sees this and switches to power saver, and hibernates
if the outage lasts too long. When mains returns, normal performance is restored.

Requirements:
  * A Wi-Fi device on a non-backed-up socket, with a fixed IP (DHCP reservation).
  * A router/gateway on backup power, so ROUTER_IP keeps answering during a cut.
  * The PC on the UPS, so it keeps running long enough to react.

Logic:
  * Sensor answers                          -> mains is on.
  * Sensor silent, router answers, for
    OUTAGE_CONFIRM checks in a row          -> mains is off: switch to power saver.
  * Sensor silent for HIBERNATE_AFTER s     -> hibernate (optional).
  * Sensor answers again for RESTORE_CONFIRM checks -> restore normal performance.
  * Sensor silent AND router silent         -> ambiguous (Wi-Fi/router problem),
                                               so no power-saver switch is made.

GPU (Linux + NVIDIA): when mains is lost every process holding a CUDA context is
frozen (SIGSTOP) and the graphics clock is locked to its minimum, keeping the GPU
under GPU_CAP_WATTS; on restore the clock is released and the processes are
continued (SIGCONT). See gpu_guard.py. Run as root via the systemd unit
(install.sh), which starts it on every boot.

Standard library only. Python 3.8+.
"""

import logging
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

# ---- settings: defaults; override in /etc/mains-watch.conf (KEY=VALUE lines) or env
SENSOR_CHECK = "ping"          # how the sensor is probed: ping | tcp:<port> | http://host/path
SENSOR_IP = ""                 # Wi-Fi device on a non-UPS socket (DHCP-reserved). Required.
ROUTER_IP = ""                 # router / gateway on backup power; "" = default gateway
CHECK_EVERY = 1.0              # seconds between checks
OUTAGE_CONFIRM = 3             # sensor misses in a row before "mains off"
RESTORE_CONFIRM = 2            # sensor answers in a row before "mains back"
HIBERNATE_AFTER = None         # seconds on battery before hibernating; None = never
GPU_CAP_WATTS = 150            # GPU draw that must not be exceeded while on battery
GPU_LOCK_MHZ = None            # graphics clock while halted; None = lowest supported
STATE_FILE = "/run/mains-watch/state.json"
CONFIG_FILE = os.environ.get("MAINS_WATCH_CONF", "/etc/mains-watch.conf")
LOG_FILE = None                # extra log file; journald already captures stdout
# -------------------------------------------------------------------------------

IS_WINDOWS = platform.system() == "Windows"


def ping(ip: str) -> bool:
    """One ICMP ping with a short timeout. True if the host replied."""
    if IS_WINDOWS:
        cmd = ["ping", "-n", "1", "-w", "1000", ip]
    else:
        cmd = ["ping", "-c", "1", "-W", "1", ip]
    try:
        result = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=4)
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def probe(ip: str, method: str) -> bool:
    """Is the sensor alive? method: 'ping', 'tcp:<port>' or an http(s):// URL (any reply counts)."""
    method = method.strip()
    if method.startswith("tcp:"):
        import socket
        try:
            with socket.create_connection((ip, int(method[4:])), timeout=1):
                return True
        except (OSError, ValueError):
            return False
    if method.startswith(("http://", "https://")):
        import urllib.error
        import urllib.request
        try:
            urllib.request.urlopen(method, timeout=1).close()
            return True
        except urllib.error.HTTPError:
            return True                         # the device answered, just not with 200
        except (OSError, ValueError):
            return False
    return ping(ip)


def run(cmd: list) -> None:
    try:
        subprocess.run(cmd, check=False, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logging.warning("Command %s failed: %s", cmd, exc)


def set_power_saver() -> None:
    if IS_WINDOWS:
        run(["powercfg", "/setactive", "SCHEME_MAX"])        # built-in Power saver
    elif shutil.which("powerprofilesctl"):
        run(["powerprofilesctl", "set", "power-saver"])      # no root needed
    else:
        logging.warning("powerprofilesctl not found; CPU power saver not applied")


def set_normal() -> None:
    if IS_WINDOWS:
        run(["powercfg", "/setactive", "SCHEME_BALANCED"])
    elif shutil.which("powerprofilesctl"):
        run(["powerprofilesctl", "set", "balanced"])


def hibernate() -> None:
    if IS_WINDOWS:
        run(["shutdown", "/h"])
    else:
        run(["systemctl", "hibernate"])


class Watcher:
    """Pure state machine: feed it sensor/router results, it returns an action or None."""

    def __init__(self, on_battery: bool = False, now: float = 0.0) -> None:
        self.on_battery = on_battery            # True when resuming after a restart mid-outage
        self.sensor_misses = 0
        self.sensor_hits = 0
        self.battery_since: Optional[float] = now if on_battery else None
        self.hibernated = False

    def step(self, sensor_ok: bool, router_ok: bool, now: float) -> Optional[str]:
        if sensor_ok:
            self.sensor_misses = 0
            self.sensor_hits += 1
            if self.on_battery and self.sensor_hits >= RESTORE_CONFIRM:
                self.on_battery = False
                self.battery_since = None
                self.hibernated = False
                return "NORMAL"
            return None

        self.sensor_hits = 0
        self.sensor_misses += 1

        if self.on_battery:
            # Keep counting toward hibernate even if the network itself is down.
            if (HIBERNATE_AFTER is not None and not self.hibernated
                    and now - self.battery_since >= HIBERNATE_AFTER):
                self.hibernated = True
                return "HIBERNATE"
            return None

        # Not on battery yet: only trust a sensor miss if the router still answers.
        if router_ok and self.sensor_misses >= OUTAGE_CONFIRM:
            self.on_battery = True
            self.battery_since = now
            return "POWER_SAVER"
        return None


def load_config(path: str) -> None:
    """Apply KEY=VALUE lines from `path`, then environment variables, to the settings."""
    values = {}
    try:
        with open(path) as fh:
            for line in fh:
                line = line.split("#", 1)[0].strip()
                if "=" in line:
                    k, v = line.split("=", 1)
                    values[k.strip()] = v.strip().strip("\"'")
    except OSError:
        pass
    g = globals()
    for key in ("SENSOR_IP", "ROUTER_IP", "CHECK_EVERY", "OUTAGE_CONFIRM", "RESTORE_CONFIRM",
                "HIBERNATE_AFTER", "SENSOR_CHECK", "GPU_CAP_WATTS", "GPU_LOCK_MHZ", "STATE_FILE", "LOG_FILE"):
        raw = os.environ.get(key, values.get(key))
        if raw is None:
            continue
        default = g[key]
        if raw.lower() in ("", "none"):
            g[key] = None if not isinstance(default, str) or key == "LOG_FILE" else ""
        elif isinstance(default, str):
            g[key] = raw
        elif isinstance(default, (int, float)) and not isinstance(default, bool):
            g[key] = type(default)(raw)
        else:                                   # None-default numeric settings
            g[key] = int(raw)


def default_gateway() -> str:
    try:
        out = subprocess.run(["ip", "route", "show", "default"], capture_output=True,
                             text=True, timeout=5).stdout.split()
        return out[out.index("via") + 1]
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return ""


def main() -> None:
    load_config(CONFIG_FILE)
    handlers = [logging.StreamHandler()]
    if LOG_FILE:
        handlers.append(logging.FileHandler(LOG_FILE))
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(message)s")
    if not SENSOR_IP:
        sys.exit("SENSOR_IP is not set (edit %s)" % CONFIG_FILE)
    router_ip = ROUTER_IP or default_gateway()
    if not router_ip:
        sys.exit("ROUTER_IP is not set and no default gateway found")

    gpu = None
    if not IS_WINDOWS:
        from gpu_guard import GpuGuard
        gpu = GpuGuard(STATE_FILE, cap_watts=GPU_CAP_WATTS, lock_mhz=GPU_LOCK_MHZ)
        if not gpu.available():
            logging.warning("nvidia-smi not found; GPU control disabled")
            gpu = None
        elif os.geteuid() != 0:
            logging.warning("not running as root: cannot freeze other users' GPU "
                            "processes or lock clocks")

    now = time.monotonic()
    # A restart in the middle of an outage must not wake the GPU up.
    watcher = Watcher(on_battery=bool(gpu and gpu.halted), now=now)
    logging.info("Watching sensor %s, router %s on %s (GPU control %s%s)",
                 SENSOR_IP, router_ip, platform.system(), "on" if gpu else "off",
                 ", resuming in halted state" if watcher.on_battery else "")

    def power_saver() -> None:
        if gpu:
            gpu.halt()                          # first: this is the urgent part
        set_power_saver()

    def normal() -> None:
        if gpu:
            gpu.resume()
        set_normal()

    actions = {
        "POWER_SAVER": ("Mains lost: halting GPU, switching to power saver", power_saver),
        "NORMAL": ("Mains back: resuming GPU, restoring normal performance", normal),
        "HIBERNATE": ("Mains off too long: hibernating", hibernate),
    }

    def shutdown(signum, _frame) -> None:
        # Never leave processes frozen behind a dead watcher.
        logging.info("Signal %s: releasing GPU and exiting", signum)
        if gpu:
            gpu.resume()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    if gpu and not watcher.on_battery and gpu.halted:
        gpu.resume()                            # stale state with nothing to hold it

    pool = ThreadPoolExecutor(max_workers=2)
    while True:
        started = time.monotonic()
        # Ping both at once so a miss costs one timeout, not two.
        f_sensor, f_router = pool.submit(probe, SENSOR_IP, SENSOR_CHECK), pool.submit(ping, router_ip)
        sensor_ok = f_sensor.result()
        router_ok = True if sensor_ok else f_router.result()

        action = watcher.step(sensor_ok, router_ok, time.monotonic())
        if action:
            message, do_it = actions[action]
            logging.info(message)
            do_it()
        elif watcher.on_battery and gpu:
            w = gpu.enforce()                   # catch jobs started during the outage
            if w is not None and int(time.monotonic()) % 30 == 0:
                logging.info("on battery, GPU %.0f W", w)

        time.sleep(max(0.0, CHECK_EVERY - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
