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
import threading
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
GPU_POLL_EVERY = 10.0          # seconds between GPU power readings (dashboard / cap check)
if platform.system() == "Windows":
    _DATA = os.path.join(os.environ.get("PROGRAMDATA", r"C:\ProgramData"), "mains-watch")
    STATE_FILE = os.path.join(_DATA, "state.json")
    PAUSE_FILE = os.path.join(_DATA, "pause.json")
    EVENTS_FILE = os.path.join(_DATA, "events.jsonl")
    _DEFAULT_CONF = os.path.join(_DATA, "mains-watch.conf")
else:
    STATE_FILE = "/run/mains-watch/state.json"
    PAUSE_FILE = "/var/lib/mains-watch/pause.json"      # present while protection is paused
    EVENTS_FILE = "/var/lib/mains-watch/events.jsonl"   # outage history (survives reboots)
    _DEFAULT_CONF = "/etc/mains-watch.conf"
DASHBOARD_PORT = 8099          # http://127.0.0.1:<port>; 0 = no dashboard
CONFIG_FILE = os.environ.get("MAINS_WATCH_CONF", _DEFAULT_CONF)
LOG_FILE = None                # extra log file; journald already captures stdout
# -------------------------------------------------------------------------------

IS_WINDOWS = platform.system() == "Windows"
EXIT = threading.Event()        # set to leave the main loop cleanly (setup page uses it to restart)

from netutil import default_gateway, ping  # noqa: E402


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
                "HIBERNATE_AFTER", "SENSOR_CHECK", "GPU_CAP_WATTS", "GPU_LOCK_MHZ", "GPU_POLL_EVERY", "STATE_FILE", "PAUSE_FILE", "EVENTS_FILE", "DASHBOARD_PORT", "LOG_FILE"):
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


def main() -> None:
    if "--check" in sys.argv:
        import selfcheck
        sys.exit(selfcheck.cli(CONFIG_FILE))
    load_config(CONFIG_FILE)
    if "--release" in sys.argv:                 # unfreeze whatever a dead watcher left frozen
        from gpu_guard import GpuGuard
        GpuGuard(STATE_FILE).resume()
        return
    handlers = [logging.StreamHandler()] if sys.stderr is not None else []   # pythonw has no console
    if LOG_FILE:
        os.makedirs(os.path.dirname(LOG_FILE) or ".", exist_ok=True)
        handlers.append(logging.FileHandler(LOG_FILE))
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(message)s")
    from dashboard import Pause, record, serve
    from gpu_guard import GpuGuard, privileged
    from onboard import Setup
    import selfcheck
    pause = Pause(PAUSE_FILE)
    live = {"setup_needed": not SENSOR_IP, "on_battery": False, "since": None, "sensor": SENSOR_IP,
            "cap_watts": GPU_CAP_WATTS, "gpu_watts": None, "frozen": 0,
            "paused": False, "paused_until": None}
    setup = Setup(CONFIG_FILE, restart=EXIT.set)
    router_ip = ROUTER_IP or default_gateway()
    cfg = dict(conf=CONFIG_FILE, sensor=SENSOR_IP, sensor_check=SENSOR_CHECK, router=router_ip,
               pause_file=PAUSE_FILE, events_file=EVENTS_FILE, state_file=STATE_FILE)
    if DASHBOARD_PORT:
        try:
            serve(DASHBOARD_PORT, EVENTS_FILE, live, setup, pause, lambda: selfcheck.verdict(selfcheck.run(cfg, probe, ping)))
        except OSError as exc:
            logging.warning("dashboard not started: %s", exc)
    if not SENSOR_IP:
        if not DASHBOARD_PORT:
            sys.exit("SENSOR_IP is not set (edit %s)" % CONFIG_FILE)
        logging.warning("SENSOR_IP not set: open http://127.0.0.1:%d to finish setup", DASHBOARD_PORT)
        EXIT.wait()                             # the setup page sets EXIT after saving the config
        sys.exit(3)                             # non-zero: the service manager starts us again
    if not router_ip:
        sys.exit("ROUTER_IP is not set and no default gateway found")

    gpu = GpuGuard(STATE_FILE, cap_watts=GPU_CAP_WATTS, lock_mhz=GPU_LOCK_MHZ)
    if not gpu.available():
        logging.warning("nvidia-smi not found; GPU control disabled")
        gpu = None
    elif not privileged():
        logging.warning("not running as root/administrator: cannot freeze other users' GPU "
                        "processes or lock clocks")

    now = time.monotonic()
    # A restart in the middle of an outage must not wake the GPU up.
    watcher = Watcher(on_battery=bool(gpu and gpu.halted), now=now)
    logging.info("Watching sensor %s, router %s on %s (GPU control %s%s)",
                 SENSOR_IP, router_ip, platform.system(), "on" if gpu else "off",
                 ", resuming in halted state" if watcher.on_battery else "")

    live.update(on_battery=watcher.on_battery, since=time.time() if watcher.on_battery else None)

    def power_saver() -> None:
        live.update(on_battery=True, since=time.time())
        record(EVENTS_FILE, "lost")
        if gpu:
            gpu.halt()                          # first: this is the urgent part
        set_power_saver()

    def normal() -> None:
        record(EVENTS_FILE, "restored", duration_s=round(time.time() - (live["since"] or time.time())))
        live.update(on_battery=False, since=None)
        if gpu:
            gpu.resume()
        set_normal()

    actions = {
        "POWER_SAVER": ("Mains lost: halting GPU, switching to power saver", power_saver),
        "NORMAL": ("Mains back: resuming GPU, restoring normal performance", normal),
        "HIBERNATE": ("Mains off too long: hibernating", hibernate),
    }

    def leave(code: int) -> None:
        # Never leave processes frozen behind a dead watcher.
        if gpu:
            gpu.resume()
        sys.exit(code)

    def on_signal(signum, _frame) -> None:
        logging.info("Signal %s: releasing GPU and exiting", signum)
        leave(0)

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    if gpu and not watcher.on_battery and gpu.halted:
        gpu.resume()                            # stale state with nothing to hold it

    pool = ThreadPoolExecutor(max_workers=2)
    was_paused = False
    last_power = 0.0                            # when the GPU power was last read (monotonic)
    while True:
        started = time.monotonic()
        if EXIT.is_set():
            logging.info("Restart requested (settings saved)")
            leave(3)
        until = pause.until()
        if until is not None:                   # user switched protection off: do nothing
            if not was_paused:
                logging.info("Protection paused: sensor will not halt the GPU")
                record(EVENTS_FILE, "paused")
                was_paused = True
                if watcher.on_battery or (gpu and gpu.halted):
                    normal()                    # release anything already frozen
                watcher = Watcher()
            live.update(paused=True, paused_until=None if until == float("inf") else until,
                        on_battery=False, since=None)
            EXIT.wait(max(0.0, CHECK_EVERY - (time.monotonic() - started)))
            continue
        if was_paused:
            logging.info("Protection back on")
            record(EVENTS_FILE, "unpaused")
            was_paused = False
            live.update(paused=False, paused_until=None)
        # Ping both at once so a miss costs one timeout, not two.
        f_sensor, f_router = pool.submit(probe, SENSOR_IP, SENSOR_CHECK), pool.submit(ping, router_ip)
        sensor_ok = f_sensor.result()
        router_ok = True if sensor_ok else f_router.result()

        action = watcher.step(sensor_ok, router_ok, time.monotonic())
        if action:
            message, do_it = actions[action]
            logging.info(message)
            do_it()
        if gpu:
            read_power = time.monotonic() - last_power >= GPU_POLL_EVERY
            if watcher.on_battery and not action:
                w = gpu.enforce(read_power)     # every cycle: freeze jobs started during the outage
            else:
                w = gpu.power_draw() if read_power else None
            if read_power:
                last_power = time.monotonic()
                live["gpu_watts"] = w
                if watcher.on_battery and w is not None:
                    logging.info("on battery, GPU %.0f W", w)
            live["frozen"] = len(gpu.stopped)
        EXIT.wait(max(0.0, CHECK_EVERY - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
