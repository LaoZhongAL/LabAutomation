import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class CliTests(unittest.TestCase):
    def test_simulation_cli(self):
        completed = subprocess.run(
            [sys.executable, "-m", "instrument_probe", "--model", "2182a", "--mode", "simulate"],
            check=True,
            capture_output=True,
            text=True,
        )
        data = json.loads(completed.stdout)
        self.assertEqual(data["profile"], "2182a")
        self.assertTrue(data["safety"]["query_only"])

    def test_real_mode_needs_explicit_ack(self):
        completed = subprocess.run(
            [sys.executable, "-m", "instrument_probe", "--model", "6221", "--mode", "real", "--resource", "GPIB0::12::INSTR"],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("QUERY_ONLY", completed.stderr)

    def test_list_lab_addresses_needs_no_model(self):
        completed = subprocess.run(
            [sys.executable, "-m", "instrument_probe", "--list-lab-addresses"],
            check=True,
            capture_output=True,
            text=True,
        )
        data = json.loads(completed.stdout)
        self.assertEqual(data["status"], "confirmed_in_ni_max")
        resources = {row["resource"] for row in data["assignments"]}
        self.assertIn("GPIB0::6::INSTR", resources)
        self.assertIn("GPIB0::9::INSTR", resources)
        self.assertIn("GPIB0::25::INSTR", resources)
        self.assertIn("GPIB0::26::INSTR", resources)

    def test_identify_lab_needs_real_ack(self):
        completed = subprocess.run(
            [sys.executable, "-m", "instrument_probe", "--identify-lab", "--mode", "real"],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("QUERY_ONLY", completed.stderr)

    def test_real_mode_requires_evidence_file_after_ack(self):
        completed = subprocess.run(
            [
                sys.executable, "-m", "instrument_probe", "--model", "6221",
                "--mode", "real", "--resource", "GPIB0::10::INSTR",
                "--real-device-ack", "QUERY_ONLY",
            ],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("requires --output", completed.stderr)

    def test_real_mode_rejects_wrong_profile_for_confirmed_address(self):
        completed = subprocess.run(
            [
                sys.executable, "-m", "instrument_probe", "--model", "6221",
                "--mode", "real", "--scope", "identity",
                "--resource", "GPIB0::6::INSTR",
                "--real-device-ack", "QUERY_ONLY", "--output", "unused.json",
            ],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("is not allowed", completed.stderr)

    def test_real_mode_rejects_unconfirmed_address(self):
        completed = subprocess.run(
            [
                sys.executable, "-m", "instrument_probe", "--model", "6221",
                "--mode", "real", "--scope", "identity",
                "--resource", "GPIB0::8::INSTR",
                "--real-device-ack", "QUERY_ONLY", "--output", "unused.json",
            ],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("not in the NI MAX confirmed", completed.stderr)

    def test_2450_core_requires_matching_command_set_ack(self):
        completed = subprocess.run(
            [
                sys.executable, "-m", "instrument_probe", "--model", "2450-scpi",
                "--mode", "real", "--scope", "core",
                "--resource", "GPIB0::25::INSTR",
                "--real-device-ack", "QUERY_ONLY", "--output", "unused.json",
            ],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("--2450-command-set-ack SCPI", completed.stderr)

        wrong_ack = subprocess.run(
            [
                sys.executable, "-m", "instrument_probe", "--model", "2450-scpi",
                "--mode", "real", "--scope", "core",
                "--resource", "GPIB0::25::INSTR",
                "--real-device-ack", "QUERY_ONLY",
                "--2450-command-set-ack", "TSP", "--output", "unused.json",
            ],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(wrong_ack.returncode, 0)
        self.assertIn("--2450-command-set-ack SCPI", wrong_ack.stderr)

    def test_existing_real_output_is_rejected_before_host_or_visa(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "existing.json"
            output.write_text("original", encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable, "-m", "instrument_probe", "--model", "6221",
                    "--mode", "real", "--scope", "identity",
                    "--resource", "GPIB0::10::INSTR",
                    "--real-device-ack", "QUERY_ONLY", "--output", str(output),
                ],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("was not started", completed.stderr)
            self.assertEqual(output.read_text(encoding="utf-8"), "original")

    def test_offline_output_is_not_overwritten_by_default(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "report.json"
            command = [
                sys.executable, "-m", "instrument_probe", "--model", "2182a",
                "--mode", "simulate", "--output", str(output),
            ]
            subprocess.run(command, check=True, capture_output=True, text=True)
            original = output.read_text(encoding="utf-8")
            repeated = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(repeated.returncode, 0)
            self.assertIn("already exists", repeated.stderr)
            self.assertEqual(output.read_text(encoding="utf-8"), original)

    def test_host_audit_writes_report_even_when_host_is_not_ready(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "host.json"
            completed = subprocess.run(
                [sys.executable, "-m", "instrument_probe", "--audit-host", "--output", str(output)],
                capture_output=True,
                text=True,
            )
            self.assertIn(completed.returncode, (0, 2))
            data = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(data["operation"], "production host audit")
            self.assertIn("host_gate_passed", data["production_readiness"])


if __name__ == "__main__":
    unittest.main()
