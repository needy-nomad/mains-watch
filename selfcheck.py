"""Will mains-watch protect the GPU after a restart? Run `python3 mains_watch.py --check`
(exit 0 = yes) or press "Run restart check" on the dashboard.

Each check is {"name", "status": ok|warn|fail, "detail", "boot": bool}. `boot` checks decide the
verdict; the rest are informational. Pass `probe`/`ping` so tests can fake the network."""

import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

IS_WINDOWS = platform.system() == "Windows"
UNIT = "mains-watch.service"
TASK = "mains-watch"


def _run(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "").strip()
    except (OSError, subprocess.TimeoutExpired):
        return 127, ""


def _c(name, ok, detail, boot=True, warn=False):
    return {"name": name, "status": "ok" if ok else ("warn" if warn else "fail"),
            "detail": detail, "boot": boot}


def _writable(path: str) -> bool:
    d = Path(path).parent
    while not d.exists() and d != d.parent:
        d = d.parent
    return os.access(d, os.W_OK)


def _linux_boot_checks(cfg):
    out = []
    rc, state = _run(["systemctl", "is-enabled", UNIT])
    out.append(_c("Starts at boot", state == "enabled",
                  "service is enabled" if state == "enabled" else
                  f"service is '{state or 'not installed'}': run  sudo ./install.sh"))
    rc, restart = _run(["systemctl", "show", "-p", "Restart", "--value", UNIT])
    out.append(_c("Restarts if it crashes", restart == "always", f"Restart={restart or '?'}"))
    rc, text = _run(["systemctl", "cat", UNIT])
    m = re.search(r"^ExecStart=(\S+)\s+(\S+)", text, re.M)
    ok = bool(m) and os.path.exists(m.group(1)) and os.path.exists(m.group(2))
    out.append(_c("Program files present", ok, f"{m.group(2)}" if ok else
                  "installed program is missing: run  sudo ./install.sh"))
    out.append(_c("Runs as root", os.geteuid() == 0 or rc == 0 and "User=" not in text,
                  "service runs as root (needed to freeze GPU jobs and lock clocks)"))
    # Wi-Fi must come up before anyone logs in, otherwise the sensor cannot be reached after a reboot.
    if shutil.which("nmcli"):
        rc, active = _run(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show", "--active"])
        wifi = [l.split(":")[0] for l in active.splitlines() if "wireless" in l or "ethernet" in l]
        if wifi:
            rc, props = _run(["nmcli", "-g", "connection.autoconnect,connection.permissions",
                              "connection", "show", wifi[0]])
            lines = props.splitlines() + ["", ""]
            ok = lines[0].strip() == "yes" and not lines[1].strip()
            out.append(_c("Network connects before login", ok,
                          f"'{wifi[0]}' auto-connects for all users" if ok else
                          f"'{wifi[0]}' is limited to one user or not auto-connecting: in Network "
                          "settings enable 'Make available to other users' and 'Connect automatically'"))
        else:
            out.append(_c("Network connects before login", False, "no active connection found", warn=True))
    else:
        out.append(_c("Network connects before login", False,
                      "NetworkManager not found; could not verify", warn=True))
    return out


def _windows_boot_checks(cfg):
    out = []
    rc, xml = _run(["schtasks", "/Query", "/TN", TASK, "/XML"])
    if rc != 0:
        return [_c("Starts at boot", False, f"scheduled task '{TASK}' not found: run install.ps1")]
    out.append(_c("Starts at boot", "<BootTrigger>" in xml and "<Enabled>false" not in xml,
                  "scheduled task runs at system start" if "<BootTrigger>" in xml
                  else "task has no start-up trigger: run install.ps1"))
    out.append(_c("Runs without login, as SYSTEM", "S-1-5-18" in xml or "SYSTEM" in xml,
                  "task runs as SYSTEM"))
    out.append(_c("Runs on battery", "<DisallowStartIfOnBatteries>false" in xml
                  and "<StopIfGoingOnBatteries>false" in xml,
                  "task is allowed to start and keep running on battery"))
    m = re.search(r"<Command>(.*?)</Command>", xml)
    exe = m.group(1).strip('"') if m else ""
    out.append(_c("Python reachable by SYSTEM", bool(exe) and os.path.exists(exe)
                  and "\\users\\" not in exe.lower(),
                  exe or "no command found", ))
    return out


def run(cfg, probe, ping):
    """List of check dicts. cfg: conf, sensor, sensor_check, router, pause_file, events_file, state_file."""
    out = _windows_boot_checks(cfg) if IS_WINDOWS else _linux_boot_checks(cfg)
    out.append(_c("Settings saved", bool(cfg.get("sensor")),
                  f"sensor {cfg['sensor']}" if cfg.get("sensor") else "no sensor set: finish setup on the dashboard"))
    from gpu_guard import privileged
    writable = all(_writable(cfg[k]) for k in ("pause_file", "events_file", "state_file") if cfg.get(k))
    out.append(_c("Can store history and pause state", writable,
                  "data folders are writable" if writable else
                  "cannot write the data folders (re-run with sudo / as administrator to verify)",
                  warn=not privileged()))
    if cfg.get("sensor"):
        out.append(_c("Sensor answers now", probe(cfg["sensor"], cfg.get("sensor_check", "ping")),
                      f"{cfg['sensor']} ({cfg.get('sensor_check', 'ping')})", boot=False, warn=True))
    out.append(_c("Router answers now", bool(cfg.get("router")) and ping(cfg["router"]),
                  cfg.get("router") or "no router found", boot=False, warn=True))
    if shutil.which("nvidia-smi"):
        rc, w = _run(["nvidia-smi", "--query-gpu=name,power.draw", "--format=csv,noheader"])
        out.append(_c("GPU readable", rc == 0 and bool(w), w or "nvidia-smi gave no data", boot=False, warn=True))
    else:
        out.append(_c("GPU readable", False, "nvidia-smi not found: GPU halting is off", boot=False, warn=True))
    try:
        from dashboard import Pause
        paused = Pause(cfg["pause_file"]).paused()
    except (ImportError, KeyError, OSError):
        paused = False
    out.append(_c("Protection not paused", not paused,
                  "protection is on" if not paused else "PAUSED: power cuts will not halt the GPU",
                  boot=False, warn=True))
    return out


def verdict(checks) -> dict:
    failed = [c for c in checks if c["boot"] and c["status"] == "fail"]
    warns = [c for c in checks if c["status"] == "warn" or (c["status"] == "fail" and not c["boot"])]
    return {"restart_proof": not failed, "failed": [c["name"] for c in failed],
            "warnings": [c["name"] for c in warns], "checks": checks}


def cli(conf_path: str) -> int:
    """Used by `mains_watch.py --check`; loads the config the same way the daemon does."""
    import mains_watch as m
    from netutil import default_gateway, ping
    m.load_config(conf_path)
    cfg = dict(conf=conf_path, sensor=m.SENSOR_IP, sensor_check=m.SENSOR_CHECK,
               router=m.ROUTER_IP or default_gateway(), pause_file=m.PAUSE_FILE,
               events_file=m.EVENTS_FILE, state_file=m.STATE_FILE)
    v = verdict(run(cfg, m.probe, ping))
    mark = {"ok": "[ OK ]", "warn": "[WARN]", "fail": "[FAIL]"}
    for c in v["checks"]:
        print(f"{mark[c['status']]} {c['name']}: {c['detail']}")
    if v["restart_proof"]:
        print("\nRESULT: restart-proof - mains-watch will start by itself after a reboot.")
    else:
        print("\nRESULT: NOT restart-proof - fix: " + "; ".join(v["failed"]))
    return 0 if v["restart_proof"] else 1


if __name__ == "__main__":
    sys.exit(cli(os.environ.get("MAINS_WATCH_CONF", "/etc/mains-watch.conf")))
