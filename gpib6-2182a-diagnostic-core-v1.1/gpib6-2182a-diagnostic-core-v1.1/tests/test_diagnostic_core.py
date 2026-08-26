import unittest

from diagnostic_core import (
    CheckStatus,
    DiagnosticState,
    DiagnosticStateMachine,
    InvalidStateTransition,
    evaluate_readiness,
    identity_is_exact,
    parse_idn,
)


NOMINAL_VALUES = {
    "identity": "KEITHLEY INSTRUMENTS INC.,MODEL 2182A,1340129,C02  /A02",
    "scpi_version": "1991.0",
    "line_frequency_hz": "50",
    "power_on_setup": "SAV0",
    "sense_function": '"VOLT:DC"',
    "active_channel": "1",
    "nplc": "5.00",
    "ch1_range_v": "0.010000",
    "ch1_autorange": "0",
    "ch2_range_v": "10.000000",
    "ch2_autorange": "1",
    "ch1_digital_filter": "0",
    "ch1_analog_filter": "0",
    "ch2_digital_filter": "1",
    "ch2_analog_filter": "0",
    "trigger_count": "+9.9e37",
    "trigger_delay_s": "0.000",
    "trigger_source": "IMM",
    "sample_count": "1",
    "continuous_initiation": "1",
    "data_format": "ASC",
    "format_elements": "READ",
}


def nominal_transcript(values=None):
    selected = dict(NOMINAL_VALUES if values is None else values)
    return [
        {
            "name": name,
            "command": f"{name}?",
            "ok": True,
            "response": value,
            "elapsed_ms": 1.0,
        }
        for name, value in selected.items()
    ]


class DiagnosticCoreTests(unittest.TestCase):
    def test_exact_idn_parser(self):
        parsed = parse_idn(NOMINAL_VALUES["identity"])
        self.assertEqual(parsed.vendor, "KEITHLEY INSTRUMENTS INC.")
        self.assertEqual(parsed.model, "2182A")
        self.assertEqual(parsed.serial, "1340129")
        self.assertEqual(parsed.firmware, "C02 /A02")
        self.assertTrue(identity_is_exact(NOMINAL_VALUES["identity"]))

    def test_serial_substring_and_wrong_device_are_rejected(self):
        self.assertFalse(
            identity_is_exact(
                "KEITHLEY INSTRUMENTS INC.,MODEL 2182A,X1340129X,C02 /A02"
            )
        )
        self.assertFalse(
            identity_is_exact(
                "KEITHLEY INSTRUMENTS INC.,MODEL 2182A,4510267,C08/B01"
            )
        )

    def test_malformed_identity_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_idn("MODEL 2182A,1340129")

    def test_nominal_readiness_allows_live(self):
        report = evaluate_readiness(
            NOMINAL_VALUES,
            nominal_transcript(),
            tuple(NOMINAL_VALUES),
            recorder_ready=True,
        )
        self.assertTrue(report.can_start_live)
        self.assertEqual(report.overall, CheckStatus.PASS)
        rendered = report.as_dict()
        self.assertEqual(rendered["layer_status"]["identity"], "PASS")
        self.assertEqual(rendered["summary"]["not_applicable"], 4)

    def test_key_configuration_drift_blocks_live(self):
        values = dict(NOMINAL_VALUES, active_channel="2")
        report = evaluate_readiness(
            values,
            nominal_transcript(values),
            tuple(NOMINAL_VALUES),
            recorder_ready=True,
        )
        self.assertFalse(report.can_start_live)
        self.assertEqual(report.overall, CheckStatus.BLOCKED)
        self.assertTrue(any("active_channel" in message for message in report.as_dict()["blockers"]))

    def test_ascii_format_requires_exact_normalized_token(self):
        values = dict(NOMINAL_VALUES, data_format="ASC-corrupt")
        report = evaluate_readiness(
            values,
            nominal_transcript(values),
            tuple(NOMINAL_VALUES),
            recorder_ready=True,
        )

        self.assertFalse(report.can_start_live)
        data_format = next(
            check
            for check in report.checks
            if check.check_id == "configuration.data_format"
        )
        self.assertEqual(data_format.status, CheckStatus.BLOCKED)

    def test_missing_query_is_unknown_and_blocks_live(self):
        transcript = [
            item for item in nominal_transcript() if item["name"] != "sample_count"
        ]
        values = dict(NOMINAL_VALUES)
        values.pop("sample_count")
        report = evaluate_readiness(
            values,
            transcript,
            tuple(NOMINAL_VALUES),
            recorder_ready=True,
        )
        self.assertFalse(report.can_start_live)
        self.assertTrue(
            any(
                check.status == CheckStatus.UNKNOWN and "sample_count" in check.check_id
                for check in report.checks
            )
        )

    def test_slow_configuration_is_warning_not_blocker(self):
        transcript = nominal_transcript()
        transcript[3]["elapsed_ms"] = 650.0
        report = evaluate_readiness(
            NOMINAL_VALUES,
            transcript,
            tuple(NOMINAL_VALUES),
            recorder_ready=True,
        )
        self.assertTrue(report.can_start_live)
        self.assertEqual(report.overall, CheckStatus.WARN)

    def test_invalid_ch2_record_only_value_warns_without_blocking_ch1(self):
        values = dict(NOMINAL_VALUES, ch2_range_v="garbage")
        report = evaluate_readiness(
            values,
            nominal_transcript(values),
            tuple(NOMINAL_VALUES),
            recorder_ready=True,
        )

        self.assertTrue(report.can_start_live)
        self.assertEqual(report.overall, CheckStatus.WARN)
        ch2_range = next(
            check
            for check in report.checks
            if check.check_id == "configuration.ch2_range_v"
        )
        self.assertEqual(ch2_range.status, CheckStatus.WARN)
        self.assertFalse(ch2_range.blocks_live)

    def test_state_machine_allows_only_explicit_transitions(self):
        machine = DiagnosticStateMachine()
        machine.transition(DiagnosticState.VERIFYING_IDENTITY)
        machine.transition(DiagnosticState.CHECKING_CONFIG)
        machine.transition(DiagnosticState.OBSERVE_READY)
        machine.transition(DiagnosticState.LIVE)
        machine.transition(DiagnosticState.DEGRADED)
        machine.transition(DiagnosticState.FAULT_LATCHED)
        self.assertEqual(machine.state, DiagnosticState.FAULT_LATCHED)

        fresh = DiagnosticStateMachine()
        with self.assertRaises(InvalidStateTransition):
            fresh.transition(DiagnosticState.LIVE)


if __name__ == "__main__":
    unittest.main()
