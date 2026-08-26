import unittest
import importlib.metadata
from types import SimpleNamespace
from unittest.mock import patch

from instrument_probe.catalog import PROFILES, queries_for
from instrument_probe import __version__
from instrument_probe.collector import collect
from instrument_probe.lab_setup import (
    LAB_INSTRUMENTS,
    detect_model,
    validate_confirmed_target,
)
from instrument_probe.production import evaluate_production_host
from instrument_probe.safety import UnsafeCommandError, validate_query
from instrument_probe.transports import DryRunTransport, SimulatedTransport, query_identity_only


class SafetyTests(unittest.TestCase):
    def test_source_and_installed_metadata_versions_match(self):
        self.assertEqual(
            __version__, importlib.metadata.version("readonly-instrument-probe")
        )

    def test_every_catalog_entry_passes_safety_gate(self):
        for profile in PROFILES.values():
            for item in profile.queries:
                validate_query(profile, item.command)

    def test_2182a_line_frequency_uses_manual_spelling(self):
        commands = {item.command for item in PROFILES["2182a"].queries}
        self.assertIn("SYST:LFREQUENCY?", commands)
        self.assertNotIn("SYST:LFREQ?", commands)

    def test_no_profile_uses_invalid_lfreq_abbreviation(self):
        for profile in PROFILES.values():
            commands = {item.command for item in profile.queries}
            self.assertNotIn("SYST:LFREQ?", commands)

    def test_arbitrary_or_dangerous_messages_are_rejected(self):
        profile = PROFILES["6221"]
        bad = (
            "OUTP ON", "*RST", "*CLS", "READ?", "MEAS?", "FETC?",
            "SYST:ERR?", "STAT:OPER:EVENT?", "SENS:DATA:FRESH?",
            "SOUR:CURR?;OUTP ON", "SOUR:CURR?\nOUTP ON", "SOUR:CURR 1",
        )
        for command in bad:
            with self.subTest(command=command):
                with self.assertRaises(UnsafeCommandError):
                    validate_query(profile, command)

    def test_tsp_assignment_is_rejected(self):
        profile = PROFILES["2450-tsp"]
        for command in ("smu.source.output = smu.ON", "print(os.execute('x'))", "reset()"):
            with self.subTest(command=command):
                with self.assertRaises(UnsafeCommandError):
                    validate_query(profile, command)

    def test_simulated_core_collection(self):
        for profile in PROFILES.values():
            report = collect(profile, SimulatedTransport(profile), "core")
            self.assertFalse(report["safety"]["stopped_after_first_io_error"])
            self.assertEqual(len(report["transcript"]), len(queries_for(profile, "core")))
            self.assertTrue(all(row["ok"] for row in report["transcript"]))

    def test_identity_scope_sends_only_idn(self):
        for profile in PROFILES.values():
            report = collect(profile, SimulatedTransport(profile), "identity")
            self.assertEqual([row["command"] for row in report["transcript"]], ["*IDN?"])

    def test_dry_run_skips_identity_verification(self):
        profile = PROFILES["6221"]
        report = collect(profile, DryRunTransport(profile), "core")
        self.assertFalse(report["safety"]["stopped_after_identity_mismatch"])
        self.assertIsNone(report["transcript"][0]["identity_matches_profile"])

    def test_identity_mismatch_stops_before_other_queries(self):
        class WrongInstrument:
            backend_name = "test"

            def query(self, command):
                self.last_command = command
                return "KEITHLEY INSTRUMENTS,MODEL 2450,WRONG,1.0"

            def close(self):
                pass

        report = collect(PROFILES["6221"], WrongInstrument(), "core")
        self.assertTrue(report["safety"]["stopped_after_identity_mismatch"])
        self.assertEqual(len(report["transcript"]), 1)

    def test_confirmed_lab_assignments_and_model_detection(self):
        resources = {assignment.resource for assignment in LAB_INSTRUMENTS}
        self.assertEqual(resources, {
            "GPIB0::6::INSTR", "GPIB0::7::INSTR", "GPIB0::9::INSTR",
            "GPIB0::10::INSTR", "GPIB0::25::INSTR", "GPIB0::26::INSTR",
        })
        self.assertEqual(detect_model("KEITHLEY,MODEL 2182A,1,1"), "2182A")
        self.assertEqual(detect_model("KEITHLEY,MODEL 6221,1,1"), "6221")
        self.assertEqual(detect_model("KEITHLEY,MODEL 2450,1,1"), "2450")

    def test_confirmed_target_gate(self):
        validate_confirmed_target("2182a", "GPIB0::6::INSTR", "core")
        validate_confirmed_target("6221", "gpib0::9::instr", "identity")
        validate_confirmed_target("2450-scpi", "GPIB0::25::INSTR", "identity")
        validate_confirmed_target("2450-tsp", "GPIB0::26::INSTR", "core")
        with self.assertRaises(ValueError):
            validate_confirmed_target("6221", "GPIB0::6::INSTR", "identity")
        with self.assertRaises(ValueError):
            validate_confirmed_target("6221", "GPIB0::8::INSTR", "identity")

    def test_identity_helper_sends_exactly_one_idn_query(self):
        calls = []

        class FakeResource:
            def query(self, command):
                calls.append(("query", command))
                return "KEITHLEY,MODEL 6221,SERIAL,1.0\n"

            def close(self):
                calls.append(("resource_close", None))

        class FakeManager:
            def open_resource(self, resource):
                calls.append(("open", resource))
                return FakeResource()

            def close(self):
                calls.append(("manager_close", None))

        fake_pyvisa = SimpleNamespace(ResourceManager=lambda *args: FakeManager())
        with patch.dict("sys.modules", {"pyvisa": fake_pyvisa}):
            identity = query_identity_only("GPIB0::10::INSTR")

        self.assertEqual(identity, "KEITHLEY,MODEL 6221,SERIAL,1.0")
        self.assertEqual([item for item in calls if item[0] == "query"], [("query", "*IDN?")])
        self.assertIn(("resource_close", None), calls)
        self.assertIn(("manager_close", None), calls)

    def test_windows_x64_python_39_or_newer_passes_host_gate(self):
        python_39_32_bit = evaluate_production_host(
            system="Windows",
            machine="AMD64",
            python_bits=32,
            python_version=(3, 9),
        )
        self.assertTrue(python_39_32_bit["host_gate_passed"])
        self.assertFalse(
            python_39_32_bit["python_bitness_assessment"]["is_hard_gate"]
        )
        self.assertEqual(python_39_32_bit["blockers"], [])

        python_38 = evaluate_production_host(
            system="Windows",
            machine="AMD64",
            python_bits=64,
            python_version=(3, 8),
        )
        self.assertFalse(python_38["host_gate_passed"])
        self.assertFalse(python_38["checks"]["python_3_9_or_newer"])

        mac = evaluate_production_host(
            system="Darwin",
            machine="arm64",
            python_bits=64,
            python_version=(3, 11),
        )
        self.assertFalse(mac["host_gate_passed"])
        self.assertFalse(mac["checks"]["windows"])
        self.assertFalse(mac["checks"]["x86_64_machine"])


if __name__ == "__main__":
    unittest.main()
