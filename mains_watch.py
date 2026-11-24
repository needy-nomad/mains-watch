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

Standard library only. Python 3.8+.
"""

import logging
import platform
import shutil
import subprocess
import time
from typing import Optional

# ---- settings: edit these ----------------------------------------------------
SENSOR_IP = "192.168.1.100"    # Wi-Fi device on a non-UPS socket (DHCP-reserved)
ROUTER_IP = "192.168.1.1"      # router / gateway (on backup power)
CHECK_EVERY = 3                # seconds between checks
OUTAGE_CONFIRM = 3             # sensor misses in a row before "mains off" (~9 s)
RESTORE_CONFIRM = 2            # sensor answers in a row before "mains back"
HIBERNATE_AFTER = 10 * 60      # seconds on battery before hibernating; None = never
LOG_FILE = "mains_watch.log"   # written next to where you run the script
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

    def __init__(self) -> None:
        self.on_battery = False
        self.sensor_misses = 0
        self.sensor_hits = 0
        self.battery_since: Optional[float] = None
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


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(LOG_FILE)],
    )
    watcher = Watcher()
    logging.info("Watching sensor %s, router %s on %s",
                 SENSOR_IP, ROUTER_IP, platform.system())

    actions = {
        "POWER_SAVER": ("Mains lost: switching to power saver", set_power_saver),
        "NORMAL": ("Mains back: restoring normal performance", set_normal),
        "HIBERNATE": ("Mains off too long: hibernating", hibernate),
    }

    while True:
        sensor_ok = ping(SENSOR_IP)
        # Only check the router when the sensor is silent, to tell a mains cut
        # from a Wi-Fi problem.
        router_ok = True if sensor_ok else ping(ROUTER_IP)

        action = watcher.step(sensor_ok, router_ok, time.monotonic())
        if action:
            message, do_it = actions[action]
            logging.info(message)
            do_it()

        time.sleep(CHECK_EVERY)


if __name__ == "__main__":
    main()
