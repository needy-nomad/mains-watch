"""Browser setup backend: Setup logic on a fake LAN, plus the HTTP endpoints' guards."""
import http.client
import ipaddress
import json
import pathlib
import socket
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import dashboard  # noqa: E402
import onboard  # noqa: E402

NET = ipaddress.ip_network("10.0.0.0/24")
INFO = ("10.0.0.1", "10.0.0.2", NET)                      # router, this PC, network


def make_setup(alive, restarted=None):
    conf = str(pathlib.Path(tempfile.mkdtemp()) / "c.conf")
    s = onboard.Setup(conf, lambda: restarted.append(1) if restarted is not None else None,
                      ping_fn=lambda ip: ip in alive, network=INFO, settle=0)
    return s, conf


class ScanTests(unittest.TestCase):
    def test_scan_finds_alive_hosts(self):
        self.assertEqual(onboard.scan(NET, lambda ip: ip in {"10.0.0.9", "10.0.0.8"}),
                         {"10.0.0.9", "10.0.0.8"})

    def test_lost_packet_does_not_hide_a_device(self):
        calls = {}
        def flaky(ip):
            calls[ip] = calls.get(ip, 0) + 1
            return ip == "10.0.0.9" and calls[ip] == 2
        self.assertEqual(onboard.scan(NET, flaky), {"10.0.0.9"})

    def test_vanished_ignores_router_and_self_and_sorts(self):
        self.assertEqual(onboard.vanished({"10.0.0.9", "10.0.0.10", "10.0.0.1", "10.0.0.2"},
                                          {"10.0.0.2"}, {"10.0.0.1", "10.0.0.2"}),
                         ["10.0.0.9", "10.0.0.10"])

    def test_set_key_preserves_other_lines(self):
        p = pathlib.Path(tempfile.mkdtemp()) / "c.conf"
        p.write_text("# note\nSENSOR_IP=1.1.1.1\nHIBERNATE_AFTER=600\n")
        onboard.set_key(str(p), "SENSOR_IP", "2.2.2.2")
        onboard.set_key(str(p), "GPU_CAP_WATTS", "150")
        self.assertEqual(p.read_text(), "# note\nSENSOR_IP=2.2.2.2\nHIBERNATE_AFTER=600\nGPU_CAP_WATTS=150\n")


class SetupTests(unittest.TestCase):
    def test_full_flow_unplug_detects_sensor_and_saves(self):
        alive = {"10.0.0.1", "10.0.0.2", "10.0.0.50", "10.0.0.60"}
        restarted = []
        s, conf = make_setup(alive, restarted)
        r = s.scan()
        self.assertEqual(r["found"], ["10.0.0.50", "10.0.0.60"])
        alive.discard("10.0.0.50")                       # user unplugs the sensor
        self.assertEqual(s.unplugged(), {"gone": ["10.0.0.50"]})
        self.assertEqual(s.check("10.0.0.50"), {"online": False})
        alive.add("10.0.0.50")                           # plugged back in
        self.assertEqual(s.check("10.0.0.50"), {"online": True})
        self.assertEqual(s.save({"sensor": "10.0.0.50", "cap": "120", "every": "2", "confirm": "4"}),
                         {"ok": True})
        self.assertEqual(s.config(), {"SENSOR_IP": "10.0.0.50", "GPU_CAP_WATTS": "120.0",
                                      "CHECK_EVERY": "2.0", "OUTAGE_CONFIRM": "4"})
        import time; time.sleep(1.3)
        self.assertEqual(restarted, [1])                 # service asked to restart

    def test_nothing_vanishes(self):
        s, _ = make_setup({"10.0.0.1", "10.0.0.2", "10.0.0.50"})
        s.scan()
        self.assertEqual(s.unplugged(), {"gone": []})

    def test_unplugged_before_scan_is_an_error(self):
        s, _ = make_setup(set())
        self.assertIn("error", s.unplugged())

    def test_save_rejects_bad_input_and_writes_nothing(self):
        restarted = []
        s, conf = make_setup(set(), restarted)
        good = {"sensor": "10.0.0.50", "cap": "150", "every": "1", "confirm": "3"}
        for bad in ({**good, "sensor": "not-an-ip"}, {**good, "sensor": "10.0.0.1"},   # router
                    {**good, "sensor": "10.0.0.2"}, {**good, "sensor": "10.0.0.5; rm -rf /"},
                    {**good, "sensor": "::1"}, {**good, "cap": "5"}, {**good, "cap": "abc"},
                    {**good, "every": "0"}, {**good, "confirm": "0"}, {"sensor": "10.0.0.50"}):
            self.assertIn("error", s.save(bad), bad)
        self.assertFalse(pathlib.Path(conf).exists())
        self.assertEqual(restarted, [])

    def test_no_network_is_reported(self):
        s = onboard.Setup("/nonexistent", lambda: None, ping_fn=lambda ip: False, network=None)
        s.net_info = None
        self.assertIn("error", s.scan())


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with socket.socket() as sk:
            sk.bind(("127.0.0.1", 0))
            cls.port = sk.getsockname()[1]
        cls.restarted = []
        cls.setup, cls.conf = make_setup({"10.0.0.1", "10.0.0.2", "10.0.0.50"}, cls.restarted)
        cls.events = str(pathlib.Path(tempfile.mkdtemp()) / "e.jsonl")
        dashboard.serve(cls.port, cls.events, {"on_battery": False}, cls.setup)

    def req(self, method, path, body=None, headers=None, host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": host or f"127.0.0.1:{self.port}", **(headers or {})}
        c.request(method, path, json.dumps(body) if body is not None else None, h)
        r = c.getresponse()
        return r.status, r.read()

    OK = {"X-Setup": "1", "Content-Type": "application/json"}

    def test_pages_served(self):
        for path, text in (("/", b"Mains watch"), ("/setup", b"Find your sensor")):
            st, body = self.req("GET", path)
            self.assertEqual(st, 200)
            self.assertIn(text, body)

    def test_post_works_with_header(self):
        st, body = self.req("POST", "/api/setup/scan", {}, self.OK)
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(body)["found"], ["10.0.0.50"])

    def test_post_without_header_is_refused(self):          # what a cross-site form/fetch looks like
        st, _ = self.req("POST", "/api/setup/save",
                         {"sensor": "10.0.0.50", "cap": 150, "every": 1, "confirm": 3},
                         {"Content-Type": "text/plain"})
        self.assertEqual(st, 403)
        self.assertFalse(pathlib.Path(self.conf).exists())

    def test_post_with_foreign_host_is_refused(self):       # DNS-rebinding
        st, _ = self.req("POST", "/api/setup/scan", {}, self.OK, host="evil.example.com")
        self.assertEqual(st, 403)

    def test_unknown_post_and_bad_json(self):
        self.assertEqual(self.req("POST", "/api/setup/nope", {}, self.OK)[0], 404)
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("POST", "/api/setup/check", "{not json", {**self.OK, "Host": f"127.0.0.1:{self.port}"})
        self.assertEqual(c.getresponse().status, 400)


if __name__ == "__main__":
    unittest.main()
