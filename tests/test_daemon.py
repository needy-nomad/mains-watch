"""End-to-end: run the real daemon with fake ping/nvidia-smi/powerprofilesctl and
simulate an outage and a restore. Run with: python3 -m unittest discover -s tests"""
import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import time
import unittest

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
from gpu_guard import _proc_stat  # noqa: E402

SENSOR, ROUTER = "10.9.9.100", "10.9.9.1"


def pstate(pid):
    st = _proc_stat(pid)
    return st[0] if st else None


class DaemonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = pathlib.Path(self.tmp.name)
        (self.d / "net").mkdir()
        (self.d / "gpu").mkdir()
        self.sensor_up(True)
        (self.d / "net" / ROUTER).write_text("")
        self.work = subprocess.Popen(["sleep", "600"])      # stand-in CUDA job
        (self.d / "gpu" / "pids").write_text(str(self.work.pid))
        self.daemon = None

    def tearDown(self):
        if self.daemon and self.daemon.poll() is None:
            self.daemon.kill()
            self.daemon.wait()
        if self.daemon:
            self.daemon.stdout.close()
        self.work.kill()
        self.work.wait()
        self.tmp.cleanup()

    def start(self):
        env = dict(os.environ, PATH=f"{HERE / 'fakebin'}:{os.environ['PATH']}",
                   FAKE_GPU_DIR=str(self.d / "gpu"), FAKE_NET_DIR=str(self.d / "net"),
                   MAINS_WATCH_CONF=str(self.d / "none.conf"), SENSOR_IP=SENSOR,
                   ROUTER_IP=ROUTER, CHECK_EVERY="0.2", STATE_FILE=str(self.d / "state.json"))
        self.daemon = subprocess.Popen([sys.executable, str(ROOT / "mains_watch.py")], env=env,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def sensor_up(self, up):
        f = self.d / "net" / SENSOR
        f.write_text("") if up else (f.unlink() if f.exists() else None)

    def wait_for(self, cond, timeout=8.0):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return time.time()
            time.sleep(0.05)
        self.fail("condition not met; daemon log:\n" + self._log())

    def _log(self):
        if self.daemon.poll() is None:
            return "(running)"
        return self.daemon.stdout.read()

    def profiles(self):
        p = self.d / "gpu" / "profile.log"
        return p.read_text().split("\n") if p.exists() else []

    def test_outage_halts_gpu_fast_and_restore_resumes(self):
        self.start()
        time.sleep(1.0)
        self.assertEqual(pstate(self.work.pid), "S")        # mains on: untouched
        self.sensor_up(False)
        t0 = time.time()
        t1 = self.wait_for(lambda: pstate(self.work.pid) == "T")
        self.assertLess(t1 - t0, 3.0, "halt should follow ~3 misses at 0.2 s")
        self.assertTrue((self.d / "gpu" / "locked").exists())
        self.wait_for(lambda: "set power-saver" in self.profiles())  # runs after the halt

        # job launched mid-outage is frozen too
        late = subprocess.Popen(["sleep", "600"])
        try:
            (self.d / "gpu" / "pids").write_text(f"{self.work.pid}\n{late.pid}")
            self.wait_for(lambda: pstate(late.pid) == "T")
            # stays halted while the outage lasts
            time.sleep(1.0)
            self.assertEqual(pstate(self.work.pid), "T")

            self.sensor_up(True)
            self.wait_for(lambda: pstate(self.work.pid) == "S" and pstate(late.pid) == "S")
            self.assertFalse((self.d / "gpu" / "locked").exists())
            self.wait_for(lambda: "set balanced" in self.profiles())
        finally:
            late.kill()
            late.wait()

    def test_router_down_too_is_not_an_outage(self):
        self.start()
        self.sensor_up(False)
        (self.d / "net" / ROUTER).unlink()
        time.sleep(2.0)
        self.assertEqual(pstate(self.work.pid), "S")

    def test_flicker_does_not_halt(self):
        self.start()
        for _ in range(4):
            self.sensor_up(False)
            time.sleep(0.3)         # ~1-2 misses
            self.sensor_up(True)
            time.sleep(0.6)
        self.assertEqual(pstate(self.work.pid), "S")

    def test_sigterm_releases_frozen_gpu(self):
        self.start()
        self.sensor_up(False)
        self.wait_for(lambda: pstate(self.work.pid) == "T")
        self.daemon.send_signal(signal.SIGTERM)
        self.daemon.wait(timeout=5)
        self.assertEqual(pstate(self.work.pid), "S")
        self.assertFalse((self.d / "gpu" / "locked").exists())

    def test_crash_mid_outage_stays_halted_then_resumes_on_restore(self):
        self.start()
        self.sensor_up(False)
        self.wait_for(lambda: pstate(self.work.pid) == "T")
        self.daemon.kill()                       # SIGKILL: no cleanup, like a crash
        self.daemon.wait()
        self.assertEqual(pstate(self.work.pid), "T")
        self.start()                             # systemd Restart=always
        time.sleep(1.5)                          # outage still on: must not wake the GPU
        self.assertEqual(pstate(self.work.pid), "T")
        self.sensor_up(True)
        self.wait_for(lambda: pstate(self.work.pid) == "S")

    def test_restart_with_mains_on_clears_stale_halt(self):
        self.start()
        self.sensor_up(False)
        self.wait_for(lambda: pstate(self.work.pid) == "T")
        self.daemon.kill()
        self.daemon.wait()
        self.sensor_up(True)                     # power returned while we were dead
        self.start()
        self.wait_for(lambda: pstate(self.work.pid) == "S")

    def test_missing_sensor_ip_refuses_to_run(self):
        env = dict(os.environ, MAINS_WATCH_CONF=str(self.d / "none.conf"), SENSOR_IP="")
        r = subprocess.run([sys.executable, str(ROOT / "mains_watch.py")], env=env,
                           capture_output=True, text=True, timeout=10)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("SENSOR_IP", r.stderr)


if __name__ == "__main__":
    unittest.main()
