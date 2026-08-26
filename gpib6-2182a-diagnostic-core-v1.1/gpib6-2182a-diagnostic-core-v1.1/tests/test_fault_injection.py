import unittest

from fault_injection import FAULT_SCENARIO_NAMES, SimulationContext


ALLOWED = {
    "*IDN?",
    "SENS:CHAN?",
    "SENS:VOLT:DC:NPLC?",
    "FETCh?",
}


class FaultInjectionTests(unittest.TestCase):
    def test_every_scenario_constructs_with_full_allowlist(self):
        full = {
            "*IDN?",
            "SENS:CHAN?",
            "SENS:VOLT:DC:NPLC?",
            "FETCh?",
        }
        for scenario in FAULT_SCENARIO_NAMES:
            SimulationContext(scenario, full, sleep=lambda _seconds: None)

    def test_rule_target_must_be_allow_listed(self):
        with self.assertRaises(ValueError):
            SimulationContext("configuration_drift", {"*IDN?"})

    def test_wrong_identity_is_exact_and_deterministic(self):
        context = SimulationContext("wrong_identity", ALLOWED)
        response = context.execute_query(
            "config",
            "*IDN?",
            lambda: "nominal",
        )
        self.assertIn("4510267", response)
        self.assertEqual(context.consumed_rule_ids, ["wrong_identity.config"])

    def test_configuration_timeout_occurs_once(self):
        context = SimulationContext("configuration_timeout", ALLOWED)
        with self.assertRaises(TimeoutError):
            context.execute_query("config", "SENS:VOLT:DC:NPLC?", lambda: "5")
        self.assertEqual(
            context.execute_query("config", "SENS:VOLT:DC:NPLC?", lambda: "5"),
            "5",
        )

    def test_voltage_sequence_repeats_for_same_seed(self):
        first = SimulationContext("nominal", ALLOWED)
        second = SimulationContext("nominal", ALLOWED)
        self.assertEqual(
            [first.next_voltage() for _ in range(5)],
            [second.next_voltage() for _ in range(5)],
        )

    def test_csv_failure_scenarios_do_not_add_queries(self):
        open_failure = SimulationContext("csv_open_failure", ALLOWED)
        write_failure = SimulationContext("csv_write_failure", ALLOWED)
        self.assertTrue(open_failure.should_fail_csv_open())
        self.assertFalse(write_failure.should_fail_csv_write(1))
        self.assertTrue(write_failure.should_fail_csv_write(2))
        self.assertEqual(open_failure.query_history, [])
        self.assertEqual(write_failure.query_history, [])


if __name__ == "__main__":
    unittest.main()
