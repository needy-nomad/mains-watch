"""Onboarding wizard tests: a fake network whose devices drop off when 'unplugged'."""
import ipaddress
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import onboard  # noqa: E402

NET = ipaddress.ip_network("10.0.0.0/24")
GATEWAY, OWN = "10.0.0.1", "10.0.0.2"


class Wizard:
    """Runs onboard.main against a fake LAN. `unplug` = devices that vanish when asked."""

    def __init__(self, unplug, answers, devices=("10.0.0.1", "10.0.0.2", "10.0.0.50",
                                                  "10.0.0.60", "10.0.0.77")):
        self.alive = set(devices)
        self.unplug, self.answers, self.out = set(unplug), list(answers), []
        self.conf = str(pathlib.Path(tempfile.mkdtemp()) / "c.conf")

    def input_fn(self, prompt):
        if "unplugged" in prompt:
            self.alive -= self.unplug
            return ""
        elif "plug the sensor back" in prompt:
            self.alive |= self.unplug
            return ""
        return self.answers.pop(0) if self.answers else ""

    def run(self):
        self.rc = onboard.main(self.conf, self.input_fn, lambda ip: ip in self.alive,
                               self.out.append, network=(GATEWAY, OWN, NET))
        self.text = "\n".join(self.out)
        p = pathlib.Path(self.conf)
        self.cfg = dict(l.split("=") for l in p.read_text().split()) if p.exists() else {}
        return self


class OnboardTests(unittest.TestCase):
    def test_scan_finds_alive_hosts(self):
        self.assertEqual(onboard.scan(NET, lambda ip: ip in {"10.0.0.9", "10.0.0.8"}),
                         {"10.0.0.9", "10.0.0.8"})

    def test_lost_packet_does_not_hide_a_device(self):
        calls = {}
        def flaky(ip):
            calls[ip] = calls.get(ip, 0) + 1
            return ip == "10.0.0.9" and calls[ip] == 2    # only the second probe answers
        self.assertEqual(onboard.scan(NET, flaky), {"10.0.0.9"})

    def test_vanished_ignores_router_and_self_and_sorts(self):
        self.assertEqual(onboard.vanished({"10.0.0.9", "10.0.0.10", "10.0.0.1", "10.0.0.2"},
                                          {"10.0.0.2"}, {"10.0.0.1", "10.0.0.2"}),
                         ["10.0.0.9", "10.0.0.10"])

    def test_one_device_vanishes_defaults_accepted(self):
        w = Wizard({"10.0.0.50"}, []).run()          # every prompt answered with Enter
        self.assertEqual(w.rc, 0)
        self.assertEqual(w.cfg, {"SENSOR_IP": "10.0.0.50", "GPU_CAP_WATTS": "150",
                                 "CHECK_EVERY": "1", "OUTAGE_CONFIRM": "3"})
        self.assertIn("is back online", w.text)

    def test_several_vanish_user_picks_second(self):
        w = Wizard({"10.0.0.50", "10.0.0.60"}, ["2"]).run()
        self.assertEqual(w.cfg["SENSOR_IP"], "10.0.0.60")

    def test_declining_suggestion_allows_manual_ip(self):
        w = Wizard({"10.0.0.50"}, ["n", "10.0.0.77"]).run()
        self.assertEqual(w.cfg["SENSOR_IP"], "10.0.0.77")

    def test_nothing_vanishes_manual_ip_or_quit(self):
        w = Wizard(set(), ["10.0.0.77"]).run()
        self.assertIn("No device disappeared", w.text)
        self.assertEqual(w.cfg["SENSOR_IP"], "10.0.0.77")
        q = Wizard(set(), [""]).run()                # Enter = quit
        self.assertEqual(q.rc, 1)
        self.assertEqual(q.cfg, {})

    def test_custom_settings_written(self):
        w = Wizard({"10.0.0.50"}, ["y", "120", "2", "4"]).run()
        self.assertEqual((w.cfg["GPU_CAP_WATTS"], w.cfg["CHECK_EVERY"], w.cfg["OUTAGE_CONFIRM"]),
                         ("120", "2", "4"))

    def test_empty_network(self):
        w = Wizard(set(), ["10.0.0.5"], devices=()).run()
        self.assertEqual(w.cfg["SENSOR_IP"], "10.0.0.5")

    def test_set_key_preserves_other_lines(self):
        p = pathlib.Path(tempfile.mkdtemp()) / "c.conf"
        p.write_text("# note\nSENSOR_IP=1.1.1.1\nHIBERNATE_AFTER=600\n")
        onboard.set_key(str(p), "SENSOR_IP", "2.2.2.2")
        onboard.set_key(str(p), "GPU_CAP_WATTS", "150")
        self.assertEqual(p.read_text(), "# note\nSENSOR_IP=2.2.2.2\nHIBERNATE_AFTER=600\nGPU_CAP_WATTS=150\n")


if __name__ == "__main__":
    unittest.main()
