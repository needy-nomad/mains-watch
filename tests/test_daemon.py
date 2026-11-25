"""End-to-end: run the real daemon with fake ping/nvidia-smi/powerprofilesctl and
simulate an outage and a restore. Run with: python3 -m unittest discover -s tests"""
import os
import pathlib
import json
import signal
import socket
import urllib.request
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
    return None if st is None else ("S" if st[0] in "RS" else st[0])


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
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]

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
                   ROUTER_IP=ROUTER, CHECK_EVERY="0.2", STATE_FILE=str(self.d / "state.json"),
                   EVENTS_FILE=str(self.d / "events.jsonl"), PAUSE_FILE=str(self.d / "pause.json"), GPU_POLL_EVERY="10", DASHBOARD_PORT=str(self.port))
        self.daemon = subprocess.Popen([sys.executable, str(ROOT / "mains_watch.py")], env=env,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def sensor_up(self, up):
        f = self.d / "net" / SENSOR
        f.write_text("") if up else (f.unlink() if f.exists() else None)

    def wait_for(self, cond, timeout=8.0):
        end = time.time() + timeout
        while time.time() < end:
            try:
                if cond():
                    return time.time()
            except OSError:                       # dashboard not listening yet
                pass
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
        self.wait_for(lambda: (self.d / "gpu" / "locked").exists())
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

    def api(self):
        return json.load(urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/status", timeout=2))

    def test_dashboard_counts_outages(self):
        self.start()
        self.wait_for(lambda: self.api().get("outages_total") == 0)
        self.assertFalse(self.api()["on_battery"])
        for n in (1, 2):
            self.sensor_up(False)
            self.wait_for(lambda: self.api()["on_battery"])
            self.wait_for(lambda: self.api()["frozen"] == 1)
            self.sensor_up(True)
            self.wait_for(lambda: not self.api()["on_battery"] and self.api()["outages_total"] == n)
            self.wait_for(lambda: self.api()["frozen"] == 0)
        d = self.api()
        self.assertEqual((d["outages_total"], d["outages_today"]), (2, 2))
        self.assertEqual([e["event"] for e in d["recent"]], ["restored", "lost", "restored", "lost"])
        page = urllib.request.urlopen(f"http://127.0.0.1:{self.port}/", timeout=2).read()
        self.assertIn(b"Mains watch", page)

    def post(self, path, body=None):
        rq = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", json.dumps(body or {}).encode(),
                                    {"X-Setup": "1", "Content-Type": "application/json"})
        return json.load(urllib.request.urlopen(rq, timeout=5))

    def test_pause_releases_gpu_ignores_outages_and_unpause_rearms(self):
        self.start()
        self.sensor_up(False)
        self.wait_for(lambda: pstate(self.work.pid) == "T")
        self.assertEqual(self.post("/api/pause", {"hours": 0}), {"ok": True})   # until switched back on
        self.wait_for(lambda: pstate(self.work.pid) == "S")                    # frozen job released
        self.wait_for(lambda: self.api()["paused"] and not self.api()["on_battery"])
        self.assertFalse((self.d / "gpu" / "locked").exists())
        time.sleep(2.0)                                                         # outage continues...
        self.assertEqual(pstate(self.work.pid), "S")                            # ...but no halt
        self.sensor_up(True)
        self.sensor_up(False)
        time.sleep(1.5)
        self.assertEqual(pstate(self.work.pid), "S")
        self.post("/api/unpause")
        self.wait_for(lambda: pstate(self.work.pid) == "T")                     # still out: protects again
        self.assertFalse(self.api()["paused"])
        events = [e["event"] for e in self.api()["recent"]]
        self.assertIn("paused", events)
        self.assertIn("unpaused", events)

    def test_pause_survives_daemon_restart(self):
        self.start()
        self.wait_for(lambda: self.api().get("outages_total") == 0)
        self.post("/api/pause", {"hours": 1})
        self.wait_for(lambda: self.api()["paused"])
        self.daemon.kill(); self.daemon.wait(); self.daemon.stdout.close()
        self.start()
        self.sensor_up(False)
        self.wait_for(lambda: self.api()["paused"])
        time.sleep(1.5)
        self.assertEqual(pstate(self.work.pid), "S")
        self.assertGreater(self.api()["paused_until"], time.time())

    def test_gpu_power_is_read_at_the_poll_interval_not_every_cycle(self):
        calls = self.d / "gpu" / "calls.log"
        self.start()                                           # CHECK_EVERY=0.2, GPU_POLL_EVERY=10
        time.sleep(3.0)                                        # ~15 cycles
        self.assertLessEqual(calls.read_text().count("power.draw"), 1)
        self.assertEqual(self.api()["gpu_watts"], 30.0)
        self.sensor_up(False)                                  # on battery: sweeps still every cycle
        self.wait_for(lambda: pstate(self.work.pid) == "T")
        before = calls.read_text().count("compute-apps")
        time.sleep(1.0)
        self.assertGreaterEqual(calls.read_text().count("compute-apps") - before, 3)
        self.assertLessEqual(calls.read_text().count("power.draw"), 2)

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
        self.wait_for(lambda: (self.d / "gpu" / "locked").exists())
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

    def test_tcp_and_http_sensor_checks(self):
        import http.server, socket, threading
        sys.path.insert(0, str(ROOT))
        import mains_watch as m
        srv = http.server.HTTPServer(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]
        try:
            self.assertTrue(m.probe("127.0.0.1", f"tcp:{port}"))
            self.assertTrue(m.probe("127.0.0.1", f"http://127.0.0.1:{port}/"))  # 501 still = alive
        finally:
            srv.shutdown(); srv.server_close()
        self.assertFalse(m.probe("127.0.0.1", f"tcp:{port}"))
        self.assertFalse(m.probe("127.0.0.1", f"http://127.0.0.1:{port}/"))
        self.assertFalse(m.probe("127.0.0.1", "tcp:notaport"))

    def test_config_file_overrides(self):
        sys.path.insert(0, str(ROOT))
        import mains_watch as m
        conf = self.d / "c.conf"
        conf.write_text("CHECK_EVERY=0.5\nGPU_CAP_WATTS=120  # lower\nSENSOR_CHECK=tcp:80\nHIBERNATE_AFTER=600\n")
        saved = (m.CHECK_EVERY, m.GPU_CAP_WATTS, m.SENSOR_CHECK, m.HIBERNATE_AFTER)
        try:
            m.load_config(str(conf))
            self.assertEqual((m.CHECK_EVERY, m.GPU_CAP_WATTS, m.SENSOR_CHECK, m.HIBERNATE_AFTER),
                             (0.5, 120, "tcp:80", 600))
        finally:
            m.CHECK_EVERY, m.GPU_CAP_WATTS, m.SENSOR_CHECK, m.HIBERNATE_AFTER = saved

    def test_config_accepts_saved_floats_and_survives_garbage(self):
        sys.path.insert(0, str(ROOT))
        import mains_watch as m
        conf = self.d / "f.conf"           # what the settings page writes, plus junk
        conf.write_text("GPU_CAP_WATTS=150.0\nOUTAGE_CONFIRM=3.0\nGPU_POLL_EVERY=30.0\nCHECK_EVERY=abc\n")
        keys = ("GPU_CAP_WATTS", "OUTAGE_CONFIRM", "GPU_POLL_EVERY", "CHECK_EVERY")
        saved = [getattr(m, k) for k in keys]
        try:
            m.load_config(str(conf))
            self.assertEqual((m.GPU_CAP_WATTS, m.OUTAGE_CONFIRM, m.GPU_POLL_EVERY), (150, 3, 30.0))
            self.assertEqual(m.CHECK_EVERY, saved[3])          # garbage ignored, default kept
        finally:
            for k, v in zip(keys, saved):
                setattr(m, k, v)

    def test_first_run_without_sensor_serves_setup_and_stays_up(self):
        import onboard
        info = onboard.local_network()
        if not info:
            self.skipTest("needs a network connection to derive the local subnet")
        prefix = str(info[2]).rsplit(".", 1)[0]                 # e.g. "10.0.0"
        router, sensor = f"{info[0]}", f"{prefix}.{254 if not info[1].endswith('.254') else 253}"
        env = dict(os.environ, PATH=f"{HERE / 'fakebin'}:{os.environ['PATH']}",
                   FAKE_NET_DIR=str(self.d / "net"), MAINS_WATCH_CONF=str(self.d / "new.conf"),
                   SENSOR_IP="", DASHBOARD_PORT=str(self.port), EVENTS_FILE=str(self.d / "e.jsonl"),
                   STATE_FILE=str(self.d / "s.json"))
        self.daemon = subprocess.Popen([sys.executable, str(ROOT / "mains_watch.py")], env=env,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.wait_for(lambda: self.api().get("setup_needed") is True)
        self.assertIn(b"Find your sensor",
                      urllib.request.urlopen(f"http://127.0.0.1:{self.port}/setup", timeout=2).read())
        time.sleep(1)
        self.assertIsNone(self.daemon.poll())                # no crash loop without a sensor

        def post(path, body):
            rq = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", json.dumps(body).encode(),
                                        {"X-Setup": "1", "Content-Type": "application/json"})
            return json.load(urllib.request.urlopen(rq, timeout=60))
        net = self.d / "net"
        (net / router).write_text("")                  # router (ignored by the scan)
        (net / sensor).write_text("")                 # the sensor
        self.assertIn(sensor, post("/api/setup/scan", {})["found"])
        (net / sensor).unlink()                       # unplug
        self.assertEqual(post("/api/setup/unplugged", {})["gone"], [sensor])
        self.assertEqual(post("/api/setup/save", {"sensor": sensor, "cap": 140,
                                                  "every": 1, "confirm": 3}), {"ok": True})
        self.daemon.wait(timeout=10)                          # exits so systemd restarts it
        self.assertIn(f"SENSOR_IP={sensor}", (self.d / "new.conf").read_text())

    def test_pause_unit(self):
        from dashboard import Pause
        p = Pause(str(self.d / "p.json"))
        self.assertFalse(p.paused())
        self.assertIn("error", p.set("abc"))
        self.assertIn("error", p.set(-1))
        p.set(1)
        self.assertTrue(p.paused())
        self.assertAlmostEqual(p.until() - time.time(), 3600, delta=5)
        p.set(0)
        self.assertEqual(p.until(), float("inf"))
        (self.d / "p.json").write_text(json.dumps({"until": time.time() - 1}))
        self.assertFalse(p.paused())                       # expired -> cleared
        self.assertFalse((self.d / "p.json").exists())
        p.clear(); p.clear()                               # idempotent

    def test_missing_sensor_ip_refuses_to_run(self):
        env = dict(os.environ, MAINS_WATCH_CONF=str(self.d / "none.conf"), SENSOR_IP="", DASHBOARD_PORT="0")
        r = subprocess.run([sys.executable, str(ROOT / "mains_watch.py")], env=env,
                           capture_output=True, text=True, timeout=10)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("SENSOR_IP", r.stderr)


if __name__ == "__main__":
    unittest.main()
