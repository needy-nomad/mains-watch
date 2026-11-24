"""GpuGuard tests with a fake nvidia-smi and real (sleeping) stand-in processes."""
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from gpu_guard import GpuGuard, _proc_stat  # noqa: E402

FAKE = str(HERE / "fakebin" / "nvidia-smi")


def state(pid):
    st = _proc_stat(pid)
    return st[0] if st else None


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        os.environ["FAKE_GPU_DIR"] = str(self.dir)
        self.procs = []
        self.state = str(self.dir / "state.json")

    def tearDown(self):
        for p in self.procs:
            p.kill()
            p.wait()
        self.tmp.cleanup()

    def spawn(self, on_gpu=True):
        p = subprocess.Popen(["sleep", "600"])
        self.procs.append(p)
        if on_gpu:
            self.set_pids([q.pid for q in self.procs if q.pid in self.gpu] + [p.pid]
                          if hasattr(self, "gpu") else [p.pid])
        return p

    def set_pids(self, pids):
        self.gpu = set(pids)
        (self.dir / "pids").write_text("\n".join(map(str, pids)))

    def guard(self, **kw):
        return GpuGuard(self.state, nvsmi=FAKE, **kw)

    def test_halt_stops_only_gpu_processes_and_resume_continues(self):
        gpu1, gpu2 = self.spawn(), self.spawn()
        other = self.spawn(on_gpu=False)
        g = self.guard()
        g.halt()
        self.assertEqual(state(gpu1.pid), "T")
        self.assertEqual(state(gpu2.pid), "T")
        self.assertEqual(state(other.pid), "S")
        self.assertEqual((self.dir / "locked").read_text(), "180,180")
        g.resume()
        self.assertEqual(state(gpu1.pid), "S")
        self.assertEqual(state(gpu2.pid), "S")
        self.assertFalse((self.dir / "locked").exists())
        self.assertFalse(g.halted)

    def test_process_started_during_outage_is_frozen(self):
        a = self.spawn()
        g = self.guard()
        g.halt()
        late = self.spawn()
        g.enforce()
        self.assertEqual(state(late.pid), "T")
        g.resume()
        self.assertEqual(state(late.pid), "S")

    def test_continued_by_someone_else_is_frozen_again(self):
        a = self.spawn()
        g = self.guard()
        g.halt()
        os.kill(a.pid, 18)  # SIGCONT
        import time; time.sleep(0.05)
        g.enforce()
        self.assertEqual(state(a.pid), "T")

    def test_already_stopped_process_is_not_resumed(self):
        a = self.spawn()
        os.kill(a.pid, 19)  # SIGSTOP by "the user"
        import time; time.sleep(0.05)
        g = self.guard()
        g.halt()
        g.resume()
        self.assertEqual(state(a.pid), "T")

    def test_recycled_pid_is_never_signalled(self):
        a = self.spawn()
        g = self.guard()
        g.halt()
        g.stopped[a.pid] += 1          # pretend the pid now belongs to a different process
        os.kill(a.pid, 18)
        g.resume()
        self.assertEqual(state(a.pid), "S")
        os.kill(a.pid, 19)             # prove resume() really skipped it
        import time; time.sleep(0.05)
        self.assertEqual(state(a.pid), "T")

    def test_state_survives_restart_and_resumes(self):
        a = self.spawn()
        self.guard().halt()
        g2 = self.guard()              # watcher restarted mid-outage
        self.assertTrue(g2.halted)
        self.assertTrue(g2.clock_locked)
        self.assertIn(a.pid, g2.stopped)
        g2.resume()
        self.assertEqual(state(a.pid), "S")
        self.assertFalse((self.dir / "locked").exists())

    def test_dead_process_is_tolerated(self):
        a = self.spawn()
        g = self.guard()
        g.halt()
        a.kill(); a.wait()
        g.resume()
        self.assertFalse(g.halted)

    def test_clock_lock_failure_does_not_stop_freezing_or_spam(self):
        (self.dir / "fail_lock").write_text("")
        a = self.spawn()
        g = self.guard()
        g.halt()
        for _ in range(5):
            g.enforce()
        self.assertEqual(state(a.pid), "T")
        self.assertEqual((self.dir / "calls.log").read_text().count("-lgc"), 1)

    def test_over_cap_power_is_reported(self):
        (self.dir / "power").write_text("320.5")
        g = self.guard(cap_watts=150)
        g.halt()
        with self.assertLogs("gpu_guard", "WARNING"):
            self.assertEqual(g.enforce(), 320.5)
        (self.dir / "power").write_text("35")
        self.assertEqual(g.enforce(), 35.0)

    def test_never_stops_itself(self):
        self.set_pids([os.getpid(), os.getppid()])
        g = self.guard()
        g.halt()
        self.assertEqual(g.stopped, {})


if __name__ == "__main__":
    unittest.main()
