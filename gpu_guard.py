"""Halt / resume NVIDIA GPU work when mains power is lost / restored.

Why SIGSTOP and not a power limit: consumer cards (e.g. RTX 5090) have a
hardware minimum power limit far above 150 W (400 W on the 5090), so
`nvidia-smi -pl 150` is refused. A GPU with no running kernels idles at a few
tens of watts, so the way to stay under the cap is to freeze every process that
holds a CUDA context (state is kept, nothing is killed) and lock the graphics
clock to its minimum as a second line of defence.

Needs root (SIGSTOP of other users' processes, `nvidia-smi -lgc`).
"""

import json
import logging
import os
import platform
import shutil
import signal
import subprocess
from pathlib import Path
from typing import Callable, Dict, Optional, Set

log = logging.getLogger("gpu_guard")
IS_WINDOWS = platform.system() == "Windows"
STOP = getattr(signal, "SIGSTOP", 19)
CONT = getattr(signal, "SIGCONT", 18)


def _win_process_start(pid: int):
    """Creation time of a process (identifies it against pid reuse), None if it is gone."""
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x1000, False, pid)             # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        t = [wintypes.FILETIME() for _ in range(4)]
        if not k32.GetProcessTimes(h, *[ctypes.byref(x) for x in t]):
            return None
        return (t[0].dwHighDateTime << 32) | t[0].dwLowDateTime
    finally:
        k32.CloseHandle(h)


def _win_suspend(pid: int, resume: bool) -> bool:
    import ctypes
    k32, nt = ctypes.windll.kernel32, ctypes.windll.ntdll
    h = k32.OpenProcess(0x0800, False, pid)             # PROCESS_SUSPEND_RESUME
    if not h:
        if k32.GetLastError() == 5:
            raise PermissionError(pid)
        return False
    try:
        return (nt.NtResumeProcess if resume else nt.NtSuspendProcess)(h) == 0
    finally:
        k32.CloseHandle(h)


def _proc_stat(pid: int):
    """(state, starttime), or None if the process is gone. state is 'T' when stopped.
    Windows cannot report a suspended state, so it always says 'S' there."""
    if IS_WINDOWS:
        start = _win_process_start(pid)
        return None if start is None else ("S", start)
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    # comm may contain spaces/parens: split after the last ')'
    fields = raw[raw.rindex(")") + 2:].split()
    return fields[0], int(fields[19])


def privileged() -> bool:
    """Root (Linux) or Administrator (Windows)."""
    if IS_WINDOWS:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    return os.geteuid() == 0


class GpuGuard:
    def __init__(self, state_path: str, cap_watts: float = 150.0,
                 lock_mhz: Optional[int] = None, nvsmi: str = "nvidia-smi",
                 list_pids: Optional[Callable[[], Set[int]]] = None) -> None:
        self.state_path = Path(state_path)
        self.cap_watts = cap_watts
        self.lock_mhz = lock_mhz
        self.nvsmi = nvsmi
        self._list_pids = list_pids
        self.stopped: Dict[int, int] = {}      # pid -> starttime, stopped by us
        self.clock_locked = False
        self.halted = False
        self._lock_tried = False
        self._ignore = {os.getpid(), os.getppid()}
        self._load_state()

    # ---- nvidia-smi ---------------------------------------------------------
    def available(self) -> bool:
        return shutil.which(self.nvsmi) is not None or os.path.isabs(self.nvsmi)

    def _smi(self, *args: str, timeout: float = 8.0) -> Optional[str]:
        try:
            r = subprocess.run([self.nvsmi, *args], capture_output=True,
                               text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warning("nvidia-smi %s failed: %s", " ".join(args), exc)
            return None
        if r.returncode != 0:
            log.warning("nvidia-smi %s rc=%s: %s", " ".join(args), r.returncode,
                        (r.stderr or r.stdout).strip()[:200])
            return None
        return r.stdout

    def gpu_pids(self) -> Set[int]:
        if self._list_pids is not None:
            return set(self._list_pids())
        out = self._smi("--query-compute-apps=pid", "--format=csv,noheader,nounits")
        pids = set()
        for line in (out or "").splitlines():
            line = line.strip()
            if line.isdigit():
                pids.add(int(line))
        return pids

    def power_draw(self) -> Optional[float]:
        """Highest power draw (W) across all GPUs, None if unreadable."""
        out = self._smi("--query-gpu=power.draw", "--format=csv,noheader,nounits",
                        timeout=5)
        vals = []
        for line in (out or "").splitlines():
            try:
                vals.append(float(line.strip()))
            except ValueError:
                pass
        return max(vals) if vals else None

    def _min_clock(self) -> Optional[int]:
        if self.lock_mhz:
            return self.lock_mhz
        out = self._smi("--query-supported-clocks=gr", "--format=csv,noheader,nounits")
        vals = [int(x) for x in (out or "").split() if x.isdigit()]
        return min(vals) if vals else None

    # ---- state file ---------------------------------------------------------
    def _load_state(self) -> None:
        try:
            data = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return
        self.halted = bool(data.get("halted"))
        self.clock_locked = bool(data.get("clock_locked"))
        self.stopped = {int(p): int(t) for p, t in data.get("pids", {}).items()}

    def _save_state(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"halted": self.halted,
                                       "clock_locked": self.clock_locked,
                                       "pids": self.stopped}))
            os.replace(tmp, self.state_path)
        except OSError as exc:
            log.warning("cannot write state %s: %s", self.state_path, exc)

    # ---- actions ------------------------------------------------------------
    def sweep(self) -> int:
        """Freeze every process currently holding a CUDA context. Returns #newly stopped."""
        n = 0
        for pid in self.gpu_pids() - self._ignore:
            st = _proc_stat(pid)
            if st is None:
                continue
            state, start = st
            if self.stopped.get(pid) == start:
                if state not in ("T", "t") and not IS_WINDOWS:   # someone continued it: stop again
                    self._signal(pid, STOP)
                continue
            if state in ("T", "t", "Z"):             # already stopped by someone else
                continue
            if self._signal(pid, STOP):
                self.stopped[pid] = start
                n += 1
                log.info("stopped GPU process %d (%s)", pid, self._cmd(pid))
        if n:
            self._save_state()
        return n

    def halt(self) -> None:
        self.halted = True
        n = self.sweep()
        self._save_state()
        if not self.clock_locked and not self._lock_tried:
            self._lock_tried = True
            mhz = self._min_clock()
            if mhz:
                self.clock_locked = True            # set first: a SIGTERM mid-call must still unlock
                if self._smi("-lgc", f"{mhz},{mhz}") is None:
                    self.clock_locked = False
                else:
                    log.info("graphics clock locked to %d MHz", mhz)
        self._save_state()
        log.info("GPU halted (%d newly stopped, %d total frozen)", n, len(self.stopped))

    def resume(self) -> None:
        if self.clock_locked:
            if self._smi("-rgc") is not None:
                self.clock_locked = False
                log.info("graphics clock unlocked")
        for pid, start in list(self.stopped.items()):
            st = _proc_stat(pid)
            if st is not None and st[1] == start:    # same process, not a reused pid
                if self._signal(pid, CONT):
                    log.info("resumed GPU process %d", pid)
                else:
                    continue
            del self.stopped[pid]
        self.halted = bool(self.stopped) or self.clock_locked
        self._lock_tried = False
        self._save_state()
        log.info("GPU resumed")

    def enforce(self, check_power: bool = True) -> Optional[float]:
        """Call every cycle while halted: freeze late starters; optionally check the power cap."""
        self.sweep()
        w = self.power_draw() if check_power else None
        if w is not None and w > self.cap_watts:
            log.warning("GPU at %.0f W exceeds the %.0f W cap while halted", w, self.cap_watts)
            if not self.clock_locked and not self._lock_tried:
                self.halt()
        return w

    # ---- helpers ------------------------------------------------------------
    @staticmethod
    def _signal(pid: int, sig: int) -> bool:
        try:
            if IS_WINDOWS:
                return _win_suspend(pid, resume=(sig == CONT))
            os.kill(pid, sig)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            log.error("no permission to signal pid %d (run as root/administrator)", pid)
            return False

    @staticmethod
    def _cmd(pid: int) -> str:
        try:
            return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(
                errors="replace").strip()[:100]
        except OSError:
            return "?"
