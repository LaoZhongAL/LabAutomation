import csv
import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

import gpib6_2182a_monitor as monitor
from diagnostic_core import parse_idn
from evidence_verifier import verify_run_directory
from run_evidence import RunJournal
from stream_quality import CSV_FIELDS, analyze_stream_csv


class EvidenceVerifierTests(unittest.TestCase):
    def _completed_run(
        self,
        root: Path,
        *,
        runtime_error: bool = False,
    ) -> tuple[RunJournal, Path, Path]:
        target = monitor.resolve_target("2182a-gpib6")
        report = monitor.collect_configuration("simulate", target=target)
        journal = RunJournal(
            root,
            mode="simulate",
            allowed_queries=monitor.allowed_queries_for_target(target),
            command_set=report["command_set"],
            profile_id=report["profile_id"],
            target=asdict(target),
            live_supported=target.live_supported,
            live_authorized=report["capabilities"]["live_authorized"],
        )
        configuration_path = root / "configuration-snapshot.json"
        configuration_path.write_text(
            json.dumps(report, indent=2),
            encoding="utf-8",
        )
        journal.set_configuration_snapshot(configuration_path.name)
        journal.set_diagnostics(
            observed_identity=asdict(parse_idn(report["values"]["identity"])),
            readiness=report["diagnostics"],
        )
        csv_path = root / "voltage-test.csv"
        stream_id = journal.register_stream(csv_path)
        with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
            writer.writeheader()
            writer.writerow(
                {
                    "elapsed_seconds": "0.250000",
                    "voltage_v": "1e-7",
                    "raw_response": "+1.00000000E-07",
                    "query_elapsed_ms": "2.000",
                }
            )
        journal.record_sample(stream_id, 0.25)
        quality_path = csv_path.with_suffix(".quality.json")
        quality_path.write_text(
            json.dumps(analyze_stream_csv(csv_path)),
            encoding="utf-8",
        )
        journal.set_stream_quality(stream_id, quality_path.name)
        journal.finish_stream(stream_id, outcome="paused")
        if runtime_error:
            journal.record_error(
                reason_code="SIMULATED_RUNTIME_FAILURE",
                message="TimeoutError: simulated timeout",
            )
        journal.finalize("test_complete")
        return journal, csv_path, configuration_path

    def test_finalize_writes_passing_verification_and_manifest_reference(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            journal, _csv_path, _configuration_path = self._completed_run(Path(temp_dir))
            manifest = json.loads(journal.manifest_path.read_text(encoding="utf-8"))
            verification_path = Path(temp_dir) / manifest["artifacts"]["evidence_verification"]
            verification = json.loads(verification_path.read_text(encoding="utf-8"))

        self.assertTrue(manifest["final"]["closed"])
        self.assertTrue(manifest["final"]["evidence_complete"])
        self.assertTrue(verification["passed"])
        self.assertEqual(verification["errors"], [])
        self.assertTrue(
            all(
                set(check) == {"check_id", "passed", "detail"}
                for check in verification["checks"]
            )
        )
        self.assertTrue(
            any(
                check["check_id"] == "manifest.readable"
                for check in verification["checks"]
            )
        )

    def test_tampered_csv_fails_direct_verification(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            journal, csv_path, _configuration_path = self._completed_run(Path(temp_dir))
            with csv_path.open("a", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
                writer.writerow(
                    {
                        "elapsed_seconds": "0.500000",
                        "voltage_v": "2e-7",
                        "raw_response": "+2.00000000E-07",
                        "query_elapsed_ms": "2.000",
                    }
                )

            verification = verify_run_directory(journal.run_directory)

        self.assertFalse(verification["passed"])
        self.assertTrue(any("sample_count" in error for error in verification["errors"]))

    def test_nonempty_error_journal_matches_error_events(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            journal, _csv_path, _configuration_path = self._completed_run(
                Path(temp_dir),
                runtime_error=True,
            )
            manifest = json.loads(journal.manifest_path.read_text(encoding="utf-8"))
            errors = [
                json.loads(line)
                for line in journal.errors_path.read_text(encoding="utf-8").splitlines()
            ]
            verification = verify_run_directory(journal.run_directory)

        self.assertTrue(verification["passed"])
        self.assertEqual(len(errors), 1)
        self.assertEqual(manifest["statistics"]["error_count"], 1)
        self.assertEqual(errors[0]["reason_code"], "SIMULATED_RUNTIME_FAILURE")

    def test_tampered_error_journal_fails_direct_verification(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            journal, _csv_path, _configuration_path = self._completed_run(Path(temp_dir))
            journal.errors_path.write_text(
                json.dumps(
                    {
                        "run_id": journal.run_id,
                        "severity": "ERROR",
                        "reason_code": "TAMPERED",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            verification = verify_run_directory(journal.run_directory)

        self.assertFalse(verification["passed"])
        self.assertTrue(any("errors.match_events" in error for error in verification["errors"]))

    def test_tampered_configuration_identity_fails_direct_verification(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            journal, _csv_path, configuration_path = self._completed_run(Path(temp_dir))
            configuration = json.loads(configuration_path.read_text(encoding="utf-8"))
            configuration["expected_identity"]["serial"] = "TAMPERED"
            configuration_path.write_text(json.dumps(configuration), encoding="utf-8")

            verification = verify_run_directory(journal.run_directory)

        self.assertFalse(verification["passed"])
        self.assertTrue(any("target" in error for error in verification["errors"]))

    def test_tampered_configuration_command_fails_direct_verification(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            journal, _csv_path, configuration_path = self._completed_run(Path(temp_dir))
            configuration = json.loads(configuration_path.read_text(encoding="utf-8"))
            configuration["transcript"][1]["command"] = "*OPT?"
            configuration_path.write_text(json.dumps(configuration), encoding="utf-8")

            verification = verify_run_directory(journal.run_directory)

        self.assertFalse(verification["passed"])
        self.assertTrue(any("allowlist" in error for error in verification["errors"]))

    def test_tampered_configuration_value_fails_direct_verification(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            journal, _csv_path, configuration_path = self._completed_run(Path(temp_dir))
            configuration = json.loads(configuration_path.read_text(encoding="utf-8"))
            configuration["values"]["line_frequency_hz"] = "60"
            configuration_path.write_text(json.dumps(configuration), encoding="utf-8")

            verification = verify_run_directory(journal.run_directory)

        self.assertFalse(verification["passed"])
        self.assertTrue(any("responses" in error for error in verification["errors"]))

    def test_tampered_configuration_readiness_fails_direct_verification(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            journal, _csv_path, configuration_path = self._completed_run(Path(temp_dir))
            configuration = json.loads(configuration_path.read_text(encoding="utf-8"))
            configuration["diagnostics"]["overall"] = "BLOCKED"
            configuration_path.write_text(json.dumps(configuration), encoding="utf-8")

            verification = verify_run_directory(journal.run_directory)

        self.assertFalse(verification["passed"])
        self.assertTrue(any("readiness" in error for error in verification["errors"]))

    def test_tampered_operation_observation_fails_direct_verification(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            journal, _csv_path, configuration_path = self._completed_run(Path(temp_dir))
            configuration = json.loads(configuration_path.read_text(encoding="utf-8"))
            configuration["operation_condition_observation"][
                "classification"
            ] = "set_for_entire_window"
            configuration_path.write_text(json.dumps(configuration), encoding="utf-8")

            verification = verify_run_directory(journal.run_directory)

        self.assertFalse(verification["passed"])
        self.assertTrue(
            any(
                "configuration_snapshot.operation_observation" in error
                for error in verification["errors"]
            )
        )

    def test_tampered_manifest_policy_hash_fails_direct_verification(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            journal, _csv_path, _configuration_path = self._completed_run(Path(temp_dir))
            manifest = json.loads(journal.manifest_path.read_text(encoding="utf-8"))
            manifest["safety"]["policy_sha256"] = "0" * 64
            journal.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            verification = verify_run_directory(journal.run_directory)

        self.assertFalse(verification["passed"])
        self.assertTrue(any("policy_sha256" in error for error in verification["errors"]))

    def test_malformed_manifest_allowlist_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            journal, _csv_path, _configuration_path = self._completed_run(Path(temp_dir))
            manifest = json.loads(journal.manifest_path.read_text(encoding="utf-8"))
            manifest["safety"]["allowed_queries"] = None
            journal.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            verification = verify_run_directory(journal.run_directory)

        self.assertFalse(verification["passed"])
        self.assertTrue(any("allowed_queries" in error for error in verification["errors"]))

    def test_tampered_configuration_safety_or_capability_fails(self):
        for field_group, field_name in (
            ("safety", "query_only"),
            ("capabilities", "live_authorized"),
        ):
            with self.subTest(field_group=field_group, field_name=field_name):
                with tempfile.TemporaryDirectory() as temp_dir:
                    journal, _csv_path, configuration_path = self._completed_run(
                        Path(temp_dir)
                    )
                    configuration = json.loads(
                        configuration_path.read_text(encoding="utf-8")
                    )
                    configuration[field_group][field_name] = False
                    configuration_path.write_text(
                        json.dumps(configuration), encoding="utf-8"
                    )

                    verification = verify_run_directory(journal.run_directory)

                self.assertFalse(verification["passed"])
                self.assertTrue(
                    any(field_group in error for error in verification["errors"])
                )

    def test_tampered_manifest_observed_identity_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            journal, _csv_path, _configuration_path = self._completed_run(Path(temp_dir))
            manifest = json.loads(journal.manifest_path.read_text(encoding="utf-8"))
            manifest["observed_identity"]["serial"] = "TAMPERED"
            journal.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            verification = verify_run_directory(journal.run_directory)

        self.assertFalse(verification["passed"])
        self.assertTrue(any("observed_identity" in error for error in verification["errors"]))

    def test_empty_diagnostic_checks_fail(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            journal, _csv_path, configuration_path = self._completed_run(Path(temp_dir))
            configuration = json.loads(configuration_path.read_text(encoding="utf-8"))
            configuration["diagnostics"]["checks"] = []
            configuration_path.write_text(json.dumps(configuration), encoding="utf-8")
            manifest = json.loads(journal.manifest_path.read_text(encoding="utf-8"))
            manifest["readiness"]["checks"] = []
            journal.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            verification = verify_run_directory(journal.run_directory)

        self.assertFalse(verification["passed"])
        self.assertTrue(any("readiness_derived" in error for error in verification["errors"]))


if __name__ == "__main__":
    unittest.main()
