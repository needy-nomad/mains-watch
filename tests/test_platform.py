"""Platform helpers and the restart-proof self-check (system calls are faked)."""
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import netutil  # noqa: E402
import selfcheck  # noqa: E402

LINUX_ROUTE = "default via 10.1.2.1 dev wlan0 proto dhcp src 10.1.2.7 metric 600\n"
WIN_ROUTE = """
IPv4 Route Table
===========================================================================
Active Routes:
Network Destination        Netmask          Gateway       Interface  Metric
          0.0.0.0          0.0.0.0      10.9.0.1     10.9.0.50     50
          0.0.0.0          0.0.0.0      10.8.0.1     10.8.0.20     25
        127.0.0.0        255.0.0.0         On-link         127.0.0.1    331
"""


class NetutilTests(unittest.TestCase):
    def test_linux_gateway(self):
        self.assertEqual(netutil.parse_gateway(LINUX_ROUTE, windows=False), "10.1.2.1")
        self.assertIsNone(netutil.parse_gateway("", windows=False))

    def test_windows_gateway_picks_lowest_metric(self):
        self.assertEqual(netutil.parse_gateway(WIN_ROUTE, windows=True), "10.8.0.1")
        self.assertIsNone(netutil.parse_gateway("no routes", windows=True))

    def test_windows_ping_needs_a_real_reply(self):
        # Windows exits 0 even for 'Destination host unreachable' from the local gateway.
        unreachable = mock.Mock(returncode=0, stdout="Reply from 10.0.0.5: Destination host unreachable.")
        reply = mock.Mock(returncode=0, stdout="Reply from 10.0.0.9: bytes=32 time=2ms TTL=64")
        with mock.patch.object(netutil, "IS_WINDOWS", True), \
                mock.patch.object(netutil.subprocess, "run", return_value=unreachable):
            self.assertFalse(netutil.ping("10.0.0.9"))
        with mock.patch.object(netutil, "IS_WINDOWS", True), \
                mock.patch.object(netutil.subprocess, "run", return_value=reply):
            self.assertTrue(netutil.ping("10.0.0.9"))

    def test_ping_survives_missing_binary(self):
        with mock.patch.object(netutil.subprocess, "run", side_effect=OSError):
            self.assertFalse(netutil.ping("10.0.0.9"))


def fake_run(table):
    def run(cmd, timeout=10):
        for key, value in table.items():
            if key in " ".join(cmd):
                return value
        return 127, ""
    return run


GOOD_LINUX = {
    "is-enabled": (0, "enabled"),
    "show -p Restart": (0, "always"),
    "systemctl cat": (0, "[Service]\nExecStart=/usr/bin/python3 " + __file__ + "\n"),
    "nmcli -t": (0, "Home:802-11-wireless"),
    "connection.autoconnect": (0, "yes\n"),
    "nvidia-smi": (0, "GPU, 30 W"),
}


class SelfcheckTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.cfg = dict(conf="x", sensor="10.0.0.9", sensor_check="ping", router="10.0.0.1",
                        pause_file=f"{d}/p.json", events_file=f"{d}/e.jsonl", state_file=f"{d}/s.json")

    def go(self, table, sensor_up=True, router_up=True, windows=False):
        with mock.patch.object(selfcheck, "IS_WINDOWS", windows), \
                mock.patch.object(selfcheck, "_run", fake_run(table)), \
                mock.patch.object(selfcheck.shutil, "which", return_value="/usr/bin/x"):
            return selfcheck.verdict(selfcheck.run(self.cfg, lambda ip, m: sensor_up, lambda ip: router_up))

    def test_all_good_is_restart_proof(self):
        v = self.go(GOOD_LINUX)
        self.assertTrue(v["restart_proof"], v)
        self.assertEqual(v["failed"], [])

    def test_not_enabled_at_boot_fails(self):
        v = self.go({**GOOD_LINUX, "is-enabled": (1, "disabled")})
        self.assertFalse(v["restart_proof"])
        self.assertIn("Starts at boot", v["failed"])

    def test_not_installed_fails(self):
        self.assertFalse(self.go({})["restart_proof"])

    def test_no_auto_restart_fails(self):
        v = self.go({**GOOD_LINUX, "show -p Restart": (0, "no")})
        self.assertIn("Restarts if it crashes", v["failed"])

    def test_wifi_limited_to_one_user_fails(self):
        v = self.go({**GOOD_LINUX, "connection.autoconnect": (0, "yes\nuser:someone")})
        self.assertIn("Network connects before login", v["failed"])

    def test_wifi_not_autoconnecting_fails(self):
        v = self.go({**GOOD_LINUX, "connection.autoconnect": (0, "no\n")})
        self.assertFalse(v["restart_proof"])

    def test_sensor_or_router_down_is_only_a_warning(self):
        v = self.go(GOOD_LINUX, sensor_up=False, router_up=False)
        self.assertTrue(v["restart_proof"])
        self.assertIn("Sensor answers now", v["warnings"])
        self.assertIn("Router answers now", v["warnings"])

    def test_missing_sensor_setting_fails(self):
        self.cfg["sensor"] = ""
        self.assertIn("Settings saved", self.go(GOOD_LINUX)["failed"])

    def test_paused_protection_is_flagged(self):
        from dashboard import Pause
        Pause(self.cfg["pause_file"]).set(1)
        v = self.go(GOOD_LINUX)
        self.assertIn("Protection not paused", v["warnings"])

    def test_windows_task_checks(self):
        good = ("<Task><Triggers><BootTrigger><Enabled>true</Enabled></BootTrigger></Triggers>"
                "<Principals><Principal><UserId>S-1-5-18</UserId></Principal></Principals>"
                "<Settings><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>"
                "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries></Settings>"
                f"<Actions><Exec><Command>{sys.executable}</Command></Exec></Actions></Task>")
        table = {"schtasks": (0, good), "nvidia-smi": (0, "GPU, 30 W")}
        if "\\users\\" in sys.executable.lower():
            self.skipTest("interpreter lives under a user profile")
        self.assertTrue(self.go(table, windows=True)["restart_proof"])
        bad = good.replace("<BootTrigger>", "<LogonTrigger>").replace("</BootTrigger>", "</LogonTrigger>")
        self.assertIn("Starts at boot", self.go({**table, "schtasks": (0, bad)}, windows=True)["failed"])
        user_py = good.replace(sys.executable, "C:\\Users\\bob\\AppData\\python.exe")
        self.assertIn("Python reachable by SYSTEM",
                      self.go({**table, "schtasks": (0, user_py)}, windows=True)["failed"])
        self.assertFalse(self.go({"schtasks": (1, "")}, windows=True)["restart_proof"])


if __name__ == "__main__":
    unittest.main()
