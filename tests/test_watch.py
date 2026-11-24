"""State-machine tests for mains_watch. Run with: python3 -m unittest discover -s tests"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import mains_watch as m  # noqa: E402


def run_seq(seq, hibernate_after=600):
    m.HIBERNATE_AFTER = hibernate_after
    w = m.Watcher()
    out, t = [], 0.0
    for sensor, router in seq:
        out.append(w.step(sensor, router, t))
        t += 3.0
    return out


OK = (True, True)


class WatcherTests(unittest.TestCase):
    def test_steady_state_no_actions(self):
        self.assertTrue(all(a is None for a in run_seq([OK] * 20)))

    def test_router_also_silent_never_switches(self):
        self.assertTrue(all(a is None for a in run_seq([(False, False)] * 20)))

    def test_brief_flicker_no_action(self):
        self.assertTrue(all(a is None for a in run_seq([(False, True)] * 2 + [OK] * 5)))

    def test_power_saver_on_third_miss(self):
        r = run_seq([OK] * 2 + [(False, True)] * 3)
        self.assertEqual(r[-1], "POWER_SAVER")
        self.assertIsNone(r[-2])
        self.assertIsNone(r[-3])

    def test_hibernate_once_after_timeout(self):
        r = run_seq([OK] * 2 + [(False, True)] * 260)
        self.assertEqual(r.count("HIBERNATE"), 1)
        self.assertEqual(r.count("POWER_SAVER"), 1)

    def test_restore_needs_two_consecutive_hits(self):
        seq = ([OK] * 2 + [(False, True)] * 3 + [OK]
               + [(False, True)] + [OK] * 2)
        r = run_seq(seq)
        self.assertEqual(r.count("NORMAL"), 1)
        self.assertEqual(r[-1], "NORMAL")

    def test_hibernate_disabled(self):
        self.assertNotIn("HIBERNATE",
                         run_seq([OK] * 2 + [(False, True)] * 260, hibernate_after=None))


if __name__ == "__main__":
    unittest.main()
