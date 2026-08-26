import unittest

from diagnostic_core import (
    CheckStatus,
    DeviceTarget,
    DiagnosticState,
    DiagnosticStateMachine,
    InvalidStateTransition,
    evaluate_observe_readiness,
    evaluate_readiness,
    identity_is_exact,
    parse_idn,
)


NOMINAL_VALUES = {
    "identity": "KEITHLEY INSTRUMENTS INC.,MODEL 2182A,1340129,C02  /A02",
    "scpi_version": "1991.0",
    "line_frequency_hz": "50",
    "power_on_setup": "SAV0",
    "system_autozero": "1",
    "front_autozero": "1",
    "line_sync": "1",
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
    "operation_condition": "16",
    "measurement_condition": "32",
    "questionable_condition": "0",
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
        self.assertEqual(report.overall, CheckStatus.WARN)
        rendered = report.as_dict()
        self.assertEqual(rendered["layer_status"]["identity"], "PASS")
        self.assertEqual(rendered["summary"]["not_applicable"], 0)
        self.assertFalse(
            any(check.check_id == "safety.operation_idle" for check in report.checks)
        )

    def test_2182a_manual_danger_bits_block_without_requiring_idle(self):
        cases = (
            ("operation_condition", "1", "acquisition.calibrating"),
            ("measurement_condition", "1", "acquisition.reading_overflow"),
            (
                "questionable_condition",
                "256",
                "calibration.condition.invalid_constant",
            ),
            (
                "questionable_condition",
                "512",
                "calibration.condition.invalid_acal",
            ),
        )
        for name, raw, check_id in cases:
            with self.subTest(name=name, raw=raw):
                values = dict(NOMINAL_VALUES, **{name: raw})
                report = evaluate_readiness(
                    values,
                    nominal_transcript(values),
                    tuple(NOMINAL_VALUES),
                    recorder_ready=True,
                )
                check = next(item for item in report.checks if item.check_id == check_id)
                self.assertEqual(check.status, CheckStatus.BLOCKED)
                self.assertFalse(report.can_start_live)

    def test_2182a_autozero_off_warns_and_invalid_condition_bits_block(self):
        values = dict(NOMINAL_VALUES, system_autozero="OFF")
        report = evaluate_readiness(
            values,
            nominal_transcript(values),
            tuple(NOMINAL_VALUES),
            recorder_ready=True,
        )
        autozero = next(
            check
            for check in report.checks
            if check.check_id == "precision.system_autozero"
        )
        self.assertEqual(autozero.status, CheckStatus.WARN)
        self.assertTrue(report.can_start_live)

        invalid = dict(NOMINAL_VALUES, operation_condition="2")
        invalid_report = evaluate_readiness(
            invalid,
            nominal_transcript(invalid),
            tuple(NOMINAL_VALUES),
            recorder_ready=True,
        )
        domain = next(
            check
            for check in invalid_report.checks
            if check.check_id == "status.operation_condition"
        )
        self.assertEqual(domain.status, CheckStatus.BLOCKED)
        self.assertFalse(invalid_report.can_start_live)

    def test_2450_condition_attributes_are_domain_checked_without_bit_inference(self):
        target = DeviceTarget(
            resource="GPIB0::25::INSTR",
            vendor="KEITHLEY INSTRUMENTS",
            model="2450",
            serial="04584128",
            firmware="1.7.12b",
            role="source_measure_unit",
        )
        values = {
            "identity": "KEITHLEY INSTRUMENTS,MODEL 2450,04584128,1.7.12b",
            "status_condition": "1.29000e+02",
            "operation_condition": "32767",
            "questionable_condition": "65535",
        }
        report = evaluate_observe_readiness(
            values,
            nominal_transcript(values),
            tuple(values),
            recorder_ready=True,
            target=target,
            instrument_family="2450",
        )
        status_checks = {
            check.check_id: check for check in report.checks if check.check_id.startswith("status.")
        }
        self.assertEqual(status_checks["status.status_condition"].status, CheckStatus.PASS)
        self.assertEqual(status_checks["status.operation_condition"].status, CheckStatus.PASS)
        self.assertEqual(status_checks["status.questionable_condition"].status, CheckStatus.PASS)
        self.assertFalse(
            any("trigger" in check.check_id for check in status_checks.values())
        )

        invalid = dict(values, status_condition="256")
        invalid_report = evaluate_observe_readiness(
            invalid,
            nominal_transcript(invalid),
            tuple(invalid),
            recorder_ready=True,
            target=target,
            instrument_family="2450",
        )
        invalid_status = next(
            check
            for check in invalid_report.checks
            if check.check_id == "status.status_condition"
        )
        self.assertEqual(invalid_status.status, CheckStatus.BLOCKED)

    def test_valid_nonpreset_settings_remain_live_compatible(self):
        values = dict(
            NOMINAL_VALUES,
            line_frequency_hz="60",
            active_channel="2",
            nplc="10",
            ch1_range_v="0.1",
            ch1_autorange="1",
            ch1_digital_filter="1",
            ch1_analog_filter="1",
            ch2_range_v="0.001",
            ch2_autorange="0",
            ch2_digital_filter="0",
            ch2_analog_filter="1",
            trigger_count="7",
            trigger_delay_s="0.25",
            trigger_source="EXT",
        )
        report = evaluate_readiness(
            values,
            nominal_transcript(values),
            tuple(NOMINAL_VALUES),
            recorder_ready=True,
        )
        self.assertTrue(report.can_start_live)
        self.assertEqual(report.overall, CheckStatus.WARN)
        self.assertFalse(report.as_dict()["blockers"])

    def test_model_valid_state_can_be_live_readout_incompatible(self):
        cases = (
            ("sense_function", '"TEMP"', "live_compatibility.sense_function"),
            ("sample_count", "2", "live_compatibility.sample_count"),
            (
                "continuous_initiation",
                "0",
                "live_compatibility.continuous_initiation",
            ),
            ("data_format", "SRE", "live_compatibility.data_format"),
            (
                "format_elements",
                "READ,UNIT",
                "live_compatibility.format_elements",
            ),
        )
        for name, raw, check_id in cases:
            with self.subTest(name=name):
                values = dict(NOMINAL_VALUES, **{name: raw})
                report = evaluate_readiness(
                    values,
                    nominal_transcript(values),
                    tuple(NOMINAL_VALUES),
                    recorder_ready=True,
                )
                model_check = next(
                    check
                    for check in report.checks
                    if check.check_id == f"configuration.{name}"
                )
                live_check = next(
                    check for check in report.checks if check.check_id == check_id
                )
                self.assertEqual(model_check.status, CheckStatus.PASS)
                self.assertEqual(live_check.status, CheckStatus.BLOCKED)
                self.assertFalse(report.can_start_live)

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
