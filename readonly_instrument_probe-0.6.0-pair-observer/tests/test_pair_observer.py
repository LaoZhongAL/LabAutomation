import tempfile
import unittest
from pathlib import Path

from instrument_probe.pair_observer import (
    CURRENT_SOURCE_RESOURCES,
    NANOVOLTMETER_RESOURCES,
    PAIR_PROFILES,
    observe_pair,
)
from instrument_probe.safety import validate_query


class PairObserverTests(unittest.TestCase):
    def test_every_pair_message_passes_the_exact_query_gate(self):
        for operation_profiles in PAIR_PROFILES.values():
            for profile in operation_profiles.values():
                self.assertEqual(profile.queries[0].command, "*IDN?")
                for item in profile.queries:
                    validate_query(profile, item.command)
                    self.assertNotIn(";", item.command)
                    self.assertNotIn("\n", item.command)

    def test_simulation_supports_all_four_free_pair_combinations(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_root = Path(temporary_directory)
            for meter in NANOVOLTMETER_RESOURCES:
                for source in CURRENT_SOURCE_RESOURCES:
                    result = observe_pair(
                        output_root,
                        nanovoltmeter_resource=meter,
                        current_source_resource=source,
                        operation="configuration",
                        mode="simulate",
                    )
                    self.assertEqual(result["pair"]["2182a_resource"], meter)
                    self.assertEqual(result["pair"]["6221_resource"], source)
                    self.assertTrue(Path(result["evidence_file"]).is_file())
                    self.assertTrue(result["safety"]["manual_refresh_only"])
                    self.assertFalse(result["safety"]["automatic_polling"])
                    self.assertFalse(
                        result["safety"]["reset_clear_trigger_acquisition_or_output_control"]
                    )

    def test_measurement_snapshot_calculates_v_over_i_locally(self):
        class FakeTransport:
            backend_name = "test"

            def __init__(self, profile, resource, timeout_ms=3000):
                self.profile = profile
                self.resource_name = resource

            def query(self, command):
                values = dict(self.profile.simulated)
                if self.profile.key == "6221":
                    values.update({
                        "OUTP?": "1",
                        "SOUR:CURR?": "1.000000E-03",
                        "SOUR:DELTA:ARM?": "0",
                    })
                else:
                    values["SENS:DATA:LATEST?"] = "1.000000E-01"
                return values[command]

            def close(self):
                return None

        with tempfile.TemporaryDirectory() as temporary_directory:
            result = observe_pair(
                Path(temporary_directory),
                nanovoltmeter_resource="GPIB0::6::INSTR",
                current_source_resource="GPIB0::9::INSTR",
                operation="measurement",
                mode="real",
                local_metadata={"nominal_resistance_ohm": "100"},
                real_transport_factory=FakeTransport,
                host_gate=lambda: None,
                readiness_provider=lambda: {"host_gate_passed": True, "blockers": []},
            )
        calculation = result["summary"]["calculation"]
        self.assertAlmostEqual(calculation["resistance_ohm"], 100.0)
        self.assertAlmostEqual(calculation["absolute_error_ohm"], 0.0)
        self.assertAlmostEqual(calculation["relative_error_percent"], 0.0)

    def test_output_off_never_produces_a_resistance_estimate(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            result = observe_pair(
                Path(temporary_directory),
                nanovoltmeter_resource="GPIB0::7::INSTR",
                current_source_resource="GPIB0::10::INSTR",
                operation="measurement",
                mode="simulate",
            )
        calculation = result["summary"]["calculation"]
        self.assertIsNone(calculation["resistance_ohm"])
        self.assertIn("output is OFF", calculation["status"])

    def test_invalid_resource_is_rejected_before_host_or_visa(self):
        calls = []

        def forbidden_host():
            calls.append("host")

        def forbidden_transport(*args, **kwargs):
            calls.append("visa")
            raise AssertionError("VISA must not be opened")

        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaisesRegex(ValueError, "not selectable"):
                observe_pair(
                    Path(temporary_directory),
                    nanovoltmeter_resource="GPIB0::8::INSTR",
                    current_source_resource="GPIB0::9::INSTR",
                    operation="configuration",
                    mode="real",
                    host_gate=forbidden_host,
                    real_transport_factory=forbidden_transport,
                )
        self.assertEqual(calls, [])

    def test_delta_armed_disables_simple_v_over_i_estimate(self):
        class DeltaTransport:
            backend_name = "test"

            def __init__(self, profile, resource, timeout_ms=3000):
                self.profile = profile
                self.resource_name = resource

            def query(self, command):
                values = dict(self.profile.simulated)
                if self.profile.key == "6221":
                    values.update({
                        "OUTP?": "1",
                        "SOUR:CURR?": "1.000000E-03",
                        "SOUR:DELTA:ARM?": "1",
                    })
                else:
                    values["SENS:DATA:LATEST?"] = "1.000000E-01"
                return values[command]

            def close(self):
                return None

        with tempfile.TemporaryDirectory() as temporary_directory:
            result = observe_pair(
                Path(temporary_directory),
                nanovoltmeter_resource="GPIB0::6::INSTR",
                current_source_resource="GPIB0::9::INSTR",
                operation="measurement",
                mode="real",
                real_transport_factory=DeltaTransport,
                host_gate=lambda: None,
                readiness_provider=lambda: {"host_gate_passed": True, "blockers": []},
            )
        calculation = result["summary"]["calculation"]
        self.assertIsNone(calculation["resistance_ohm"])
        self.assertIn("Delta mode is armed", calculation["status"])

    def test_visa_open_failure_is_saved_as_partial_evidence(self):
        class PartlyUnavailableTransport:
            backend_name = "test"

            def __init__(self, profile, resource, timeout_ms=3000):
                if profile.key == "2182a":
                    raise RuntimeError("simulated 2182A session-open failure")
                self.profile = profile
                self.resource_name = resource

            def query(self, command):
                return self.profile.simulated[command]

            def close(self):
                return None

        with tempfile.TemporaryDirectory() as temporary_directory:
            result = observe_pair(
                Path(temporary_directory),
                nanovoltmeter_resource="GPIB0::6::INSTR",
                current_source_resource="GPIB0::9::INSTR",
                operation="configuration",
                mode="real",
                real_transport_factory=PartlyUnavailableTransport,
                host_gate=lambda: None,
                readiness_provider=lambda: {"host_gate_passed": True, "blockers": []},
            )
            self.assertTrue(Path(result["evidence_file"]).is_file())

        meter = result["instruments"]["2182a"]
        self.assertEqual(result["summary"]["status"], "warning")
        self.assertTrue(meter["safety"]["stopped_after_first_io_error"])
        self.assertIn("session-open failure", meter["transcript"][0]["error"])


if __name__ == "__main__":
    unittest.main()
