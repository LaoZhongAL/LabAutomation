import csv
import json
import tempfile
import unittest
from pathlib import Path

from diagnostic_core import DIAGNOSTIC_SCHEMA_VERSION, DiagnosticState
from gpib6_2182a_monitor import profile_id_for
from run_evidence import APP_RELEASE_TAG, APP_VERSION, RecorderError, RunJournal, query_policy_hash
from stream_quality import CSV_FIELDS, analyze_stream_csv


class RunEvidenceTests(unittest.TestCase):
    def test_release_identity_is_written_consistently(self):
        self.assertEqual(APP_RELEASE_TAG, "v1.5")
        self.assertEqual(APP_VERSION, "1.5.0-query-only-snapshot-ownership")
        self.assertEqual(DIAGNOSTIC_SCHEMA_VERSION, 3)
        self.assertTrue(
            all(
                profile_id_for(target_key).endswith("-v1.5")
                for target_key in ("2182a-gpib6", "6221-gpib9", "2450-gpib25")
            )
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "run"
            run_dir.mkdir()
            RunJournal(run_dir, mode="simulate", allowed_queries={"*IDN?"})
            manifest = json.loads(
                (run_dir / "run-manifest.json").read_text(encoding="utf-8")
            )
        self.assertEqual(manifest["app"]["version"], APP_VERSION)

    def test_policy_hash_is_order_independent(self):
        self.assertEqual(query_policy_hash(["B?", "A?"]), query_policy_hash(["A?", "B?"]))

    def test_manifest_and_jsonl_lifecycle(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "run"
            run_dir.mkdir()
            journal = RunJournal(
                run_dir,
                mode="simulate",
                allowed_queries={"*IDN?", "FETCh?"},
            )
            journal.transition(
                DiagnosticState.VERIFYING_IDENTITY,
                reason_code="CONFIGURATION_REQUESTED",
            )
            journal.transition(
                DiagnosticState.CHECKING_CONFIG,
                reason_code="IDENTITY_VERIFIED",
            )
            journal.set_diagnostics(
                observed_identity={"serial": "1340129"},
                readiness={
                    "overall": "PASS",
                    "diagnostics_acceptable": True,
                    "can_start_live": True,
                    "blockers": [],
                    "warnings": [],
                },
            )
            journal.transition(
                DiagnosticState.OBSERVE_READY,
                reason_code="READINESS_PASSED",
            )
            csv_path = run_dir / "voltage-test.csv"
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
            journal.transition(DiagnosticState.LIVE, reason_code="FIRST_SAMPLE_PERSISTED")
            journal.stream_started(stream_id)
            journal.record_sample(stream_id, 0.25)
            started = journal.start_intervention(
                stream_id,
                elapsed_seconds=0.5,
                intervention_type="connector_disturbance",
                location="rear input connector",
            )
            journal.end_intervention(
                str(started["intervention_id"]),
                elapsed_seconds=0.75,
            )
            quality_path = csv_path.with_suffix(".quality.json")
            quality_path.write_text(
                json.dumps(analyze_stream_csv(csv_path)),
                encoding="utf-8",
            )
            journal.set_stream_quality(stream_id, quality_path.name)
            journal.finish_stream(stream_id, outcome="paused")
            journal.transition(DiagnosticState.OBSERVE_READY, reason_code="PAUSED")
            journal.finalize("test_complete")

            manifest = json.loads((run_dir / "run-manifest.json").read_text(encoding="utf-8"))
            events = [
                json.loads(line)
                for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            interventions = [
                json.loads(line)
                for line in (run_dir / "interventions.jsonl").read_text(encoding="utf-8").splitlines()
            ]

        self.assertTrue(manifest["final"]["closed"])
        self.assertEqual(manifest["final"]["termination_reason"], "test_complete")
        self.assertEqual(manifest["statistics"]["sample_count"], 1)
        self.assertEqual(manifest["statistics"]["intervention_count"], 1)
        self.assertNotIn("touch_count", manifest["statistics"])
        self.assertEqual(manifest["artifacts"]["interventions"], "interventions.jsonl")
        self.assertEqual([event["seq"] for event in events], list(range(1, len(events) + 1)))
        elapsed = [event["elapsed_seconds"] for event in events]
        self.assertEqual(elapsed, sorted(elapsed))
        self.assertTrue(all("created_at" not in event for event in events))
        expected_keys = {
            "schema_version",
            "run_id",
            "seq",
            "stream_id",
            "intervention_id",
            "phase",
            "elapsed_seconds",
            "intervention_type",
            "location",
        }
        self.assertEqual(len(interventions), 2)
        self.assertTrue(all(set(record) == expected_keys for record in interventions))
        self.assertEqual([record["seq"] for record in interventions], [1, 2])
        self.assertEqual([record["phase"] for record in interventions], ["start", "end"])
        self.assertEqual([record["elapsed_seconds"] for record in interventions], [0.5, 0.75])
        self.assertEqual(
            interventions[0]["intervention_id"],
            interventions[1]["intervention_id"],
        )
        self.assertTrue(
            all(record["intervention_type"] == "connector_disturbance" for record in interventions)
        )
        self.assertTrue(all(record["location"] == "rear input connector" for record in interventions))
        self.assertTrue(all("created_at" not in record for record in interventions))
        self.assertFalse(any(event["event_type"].startswith("intervention") for event in events))

    def test_diagnostic_only_ready_event_uses_diagnostics_acceptable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "run"
            run_dir.mkdir()
            journal = RunJournal(
                run_dir,
                mode="simulate",
                allowed_queries={"*IDN?"},
            )

            journal.set_diagnostics(
                observed_identity={"serial": "4533811"},
                readiness={
                    "overall": "WARN",
                    "diagnostics_acceptable": True,
                    "can_start_live": False,
                    "blockers": [],
                    "warnings": ["sample profile not selected"],
                },
            )
            events = [
                json.loads(line)
                for line in journal.events_path.read_text(encoding="utf-8").splitlines()
            ]

        event = events[-1]
        self.assertEqual(event["event_type"], "readiness_evaluated")
        self.assertEqual(event["severity"], "INFO")
        self.assertEqual(event["reason_code"], "READINESS_OBSERVE_READY")
        self.assertTrue(event["payload"]["diagnostics_acceptable"])
        self.assertFalse(event["payload"]["can_start_live"])

    def test_intervention_validation_and_pairing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "run"
            run_dir.mkdir()
            journal = RunJournal(
                run_dir,
                mode="simulate",
                allowed_queries={"*IDN?", "FETCh?"},
            )
            stream_id = journal.register_stream(run_dir / "voltage-test.csv")
            journal.transition(
                DiagnosticState.VERIFYING_IDENTITY,
                reason_code="TEST_VERIFY",
            )
            journal.transition(
                DiagnosticState.CHECKING_CONFIG,
                reason_code="TEST_CONFIG",
            )
            journal.transition(
                DiagnosticState.OBSERVE_READY,
                reason_code="TEST_READY",
            )
            journal.transition(DiagnosticState.LIVE, reason_code="TEST_LIVE")
            journal.stream_started(stream_id)

            for intervention_type, location, elapsed in (
                ("unknown", "rear", 1.0),
                ("cable_disturbance", "  ", 1.0),
                ("cable_disturbance", "rear\nconnector", 1.0),
                ("cable_disturbance", "rear", -1.0),
            ):
                with self.subTest(
                    intervention_type=intervention_type,
                    location=location,
                    elapsed=elapsed,
                ), self.assertRaises(RecorderError):
                    journal.start_intervention(
                        stream_id,
                        elapsed_seconds=elapsed,
                        intervention_type=intervention_type,
                        location=location,
                    )

            started = journal.start_intervention(
                stream_id,
                elapsed_seconds=1.0,
                intervention_type="cable_disturbance",
                location="cryostat connector",
            )
            with self.assertRaises(RecorderError):
                journal.finish_stream(stream_id, outcome="paused")
            with self.assertRaises(RecorderError):
                journal.start_intervention(
                    stream_id,
                    elapsed_seconds=1.1,
                    intervention_type="other",
                    location="sample holder",
                )
            with self.assertRaises(RecorderError):
                journal.end_intervention(
                    str(started["intervention_id"]),
                    elapsed_seconds=0.9,
                )
            journal.end_intervention(
                str(started["intervention_id"]),
                elapsed_seconds=1.2,
            )
            with self.assertRaises(RecorderError):
                journal.end_intervention(
                    str(started["intervention_id"]),
                    elapsed_seconds=1.3,
                )

    def test_intervention_write_failure_leaves_unmatched_start_and_zero_completed_count(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "run"
            run_dir.mkdir()
            journal = RunJournal(
                run_dir,
                mode="simulate",
                allowed_queries={"*IDN?", "FETCh?"},
                fail_intervention_write_after=1,
            )
            stream_id = journal.register_stream(run_dir / "voltage-test.csv")
            journal.transition(
                DiagnosticState.VERIFYING_IDENTITY,
                reason_code="TEST_VERIFY",
            )
            journal.transition(
                DiagnosticState.CHECKING_CONFIG,
                reason_code="TEST_CONFIG",
            )
            journal.transition(
                DiagnosticState.OBSERVE_READY,
                reason_code="TEST_READY",
            )
            journal.transition(DiagnosticState.LIVE, reason_code="TEST_LIVE")
            journal.stream_started(stream_id)
            started = journal.start_intervention(
                stream_id,
                elapsed_seconds=1.0,
                intervention_type="other",
                location="sample space",
            )

            with self.assertRaises(RecorderError):
                journal.end_intervention(
                    str(started["intervention_id"]),
                    elapsed_seconds=2.0,
                )

            records = journal.interventions_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(records), 1)
            self.assertEqual(json.loads(records[0])["phase"], "start")
            self.assertEqual(journal.manifest["statistics"]["intervention_count"], 0)
            journal.finish_stream(
                stream_id,
                outcome="fault",
                error="intervention journal write failure",
            )
            stream = journal.manifest["artifacts"]["streams"][0]
            self.assertEqual(stream["status"], "closed")
            self.assertEqual(stream["outcome"], "fault")
            self.assertEqual(stream["intervention_count"], 0)

    def test_fault_stop_gate_atomically_rejects_late_intervention_start(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "run"
            run_dir.mkdir()
            journal = RunJournal(
                run_dir,
                mode="simulate",
                allowed_queries={"*IDN?", "FETCh?"},
            )
            stream_id = journal.register_stream(run_dir / "voltage-test.csv")
            journal.transition(
                DiagnosticState.VERIFYING_IDENTITY,
                reason_code="TEST_VERIFY",
            )
            journal.transition(
                DiagnosticState.CHECKING_CONFIG,
                reason_code="TEST_CONFIG",
            )
            journal.transition(
                DiagnosticState.OBSERVE_READY,
                reason_code="TEST_READY",
            )
            journal.transition(DiagnosticState.LIVE, reason_code="TEST_LIVE")
            journal.stream_started(stream_id)

            self.assertIsNone(
                journal.stop_interventions_for_stream(
                    stream_id,
                    elapsed_seconds=1.0,
                )
            )
            with self.assertRaisesRegex(RecorderError, "live stream"):
                journal.start_intervention(
                    stream_id,
                    elapsed_seconds=1.1,
                    intervention_type="other",
                    location="sample space",
                )
            stream = journal.manifest["artifacts"]["streams"][0]
            self.assertEqual(stream["status"], "stopping")
            journal.finish_stream(
                stream_id,
                outcome="fault",
                error="simulated stream fault",
            )

    def test_event_write_failure_is_injectable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "run"
            run_dir.mkdir()
            journal = RunJournal(
                run_dir,
                mode="simulate",
                allowed_queries={"*IDN?"},
                fail_event_write_after=1,
            )
            with self.assertRaises(RecorderError):
                journal.record_event("next", reason_code="SIMULATED_FAILURE")

    def test_failed_transition_keeps_memory_manifest_and_events_consistent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "run"
            run_dir.mkdir()
            journal = RunJournal(
                run_dir,
                mode="simulate",
                allowed_queries={"*IDN?"},
                fail_event_write_after=1,
            )

            with self.assertRaises(RecorderError):
                journal.transition(
                    DiagnosticState.VERIFYING_IDENTITY,
                    reason_code="SIMULATED_TRANSITION_FAILURE",
                )

            manifest = json.loads(journal.manifest_path.read_text(encoding="utf-8"))
            events = [
                json.loads(line)
                for line in journal.events_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(journal.state, DiagnosticState.DISCONNECTED)
            self.assertEqual(manifest["state"], "DISCONNECTED")
            self.assertEqual(manifest["final"]["state"], "DISCONNECTED")
            self.assertEqual([event["event_type"] for event in events], ["run_created"])

    def test_manifest_refuses_false_query_only_or_real_fault_claims(self):
        cases = (
            {"mode": "simulate", "allowed_queries": {"*RST"}},
            {"mode": "simulate", "allowed_queries": {"FETCh?;*RST"}},
            {
                "mode": "real",
                "allowed_queries": {"*IDN?"},
                "fault_scenario": "fetch_timeout",
            },
        )
        for index, arguments in enumerate(cases):
            with self.subTest(arguments=arguments), tempfile.TemporaryDirectory() as temp_dir:
                run_dir = Path(temp_dir) / f"run-{index}"
                run_dir.mkdir()
                with self.assertRaises(RecorderError):
                    RunJournal(run_dir, **arguments)

    def test_tsp_manifest_accepts_only_simple_exact_attribute_prints(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "valid"
            run_dir.mkdir()
            journal = RunJournal(
                run_dir,
                mode="simulate",
                allowed_queries={"*IDN?", "print(localnode.model)"},
                command_set="TSP",
                profile_id="2450-gpib25-read-only-test",
                target={"resource": "GPIB0::25::INSTR", "serial": "04584128"},
                live_supported=False,
            )
            manifest = json.loads(journal.manifest_path.read_text(encoding="utf-8"))
            journal.finalize("test_complete")

        self.assertEqual(manifest["safety"]["command_set"], "TSP")
        self.assertFalse(manifest["capabilities"]["live_supported"])

        for index, command in enumerate(
            (
                "smu.source.output=1",
                "print(smu.measure.read())",
                "print(localnode.model);reset()",
                "print(trigger.model.state())",
            )
        ):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as temp_dir:
                run_dir = Path(temp_dir) / f"invalid-{index}"
                run_dir.mkdir()
                with self.assertRaises(RecorderError):
                    RunJournal(
                        run_dir,
                        mode="simulate",
                        allowed_queries={"*IDN?", command},
                        command_set="TSP",
                    )


if __name__ == "__main__":
    unittest.main()
