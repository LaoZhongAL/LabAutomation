import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "gpib6_2182a_monitor.py"
SPEC = importlib.util.spec_from_file_location("gpib6_2182a_monitor", MODULE_PATH)
monitor = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(monitor)


class MonitorTests(unittest.TestCase):
    def test_parse_voltage(self):
        self.assertAlmostEqual(monitor.parse_voltage("+1.08673749E-07"), 1.08673749e-7)
        self.assertAlmostEqual(monitor.parse_voltage("-2.5E-6,extra"), -2.5e-6)

    def test_non_numeric_voltage_rejected(self):
        with self.assertRaises(ValueError):
            monitor.parse_voltage("not-a-number")

    def test_poll_interval_uses_real_baseline(self):
        values = {"nplc": "5.00", "line_frequency_hz": "50"}
        self.assertEqual(monitor.derive_poll_interval(values), 0.25)

    def test_allowlist_has_only_queries(self):
        self.assertIn("FETCh?", monitor.ALLOWED_QUERIES)
        self.assertNotIn("READ?", monitor.ALLOWED_QUERIES)
        self.assertNotIn("*RST", monitor.ALLOWED_QUERIES)
        self.assertTrue(all(command.endswith("?") for command in monitor.ALLOWED_QUERIES))

    def test_simulated_configuration_matches_real_target(self):
        report = monitor.collect_configuration("simulate")
        self.assertTrue(report["live_readiness"]["ready"])
        self.assertEqual(report["values"]["active_channel"], "1")
        self.assertEqual(report["values"]["ch1_range_v"], "0.010000")
        self.assertEqual(report["values"]["nplc"], "5.00")

    def test_expected_identity(self):
        self.assertTrue(
            monitor.identity_is_expected(
                "KEITHLEY INSTRUMENTS INC.,MODEL 2182A,1340129,C02  /A02"
            )
        )
        self.assertFalse(
            monitor.identity_is_expected(
                "KEITHLEY INSTRUMENTS INC.,MODEL 2182A,4510267,C08/B01"
            )
        )


if __name__ == "__main__":
    unittest.main()

