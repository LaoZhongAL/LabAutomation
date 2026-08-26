import csv
import importlib.util
import json
import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "gpib6_2182a_monitor.py"
SPEC = importlib.util.spec_from_file_location("gpib6_2182a_monitor_diagnostics", MODULE_PATH)
monitor = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(monitor)
LIVE_TARGET = monitor.target_for(monitor.DEFAULT_TARGET_KEY)


class MonitorDiagnosticIntegrationTests(unittest.TestCase):
    @staticmethod
    def _owner(
        kind,
        *,
        target=LIVE_TARGET,
        inventory_snapshot_id=None,
        run_id=None,
        stream_id=None,
    ):
        return monitor.OperationOwner(
            operation_id=f"test-{kind}",
            kind=kind,
            mode="simulate",
            target_key=target.key if target is not None else None,
            inventory_snapshot_id=inventory_snapshot_id,
            run_id=run_id,
            stream_id=stream_id,
        )

    @staticmethod
    def _worker_app():
        app = monitor.MonitorApp.__new__(monitor.MonitorApp)
        app.events = queue.Queue()
        app.stop_event = threading.Event()
        app.poll_interval_s = 0.0
        app.stream_start_monotonic = time.monotonic()
        return app

    def test_manifest_and_event_log_exist_before_configuration_query(self):
        app = self._worker_app()
        original_collect = monitor.collect_configuration

        with tempfile.TemporaryDirectory() as temp_dir:
            output_root = Path(temp_dir)

            def inspect_prequery(*args, **kwargs):
                run_directories = list(output_root.iterdir())
                self.assertEqual(len(run_directories), 1)
                self.assertTrue((run_directories[0] / "run-manifest.json").is_file())
                self.assertTrue((run_directories[0] / "events.jsonl").is_file())
                self.assertTrue((run_directories[0] / "interventions.jsonl").is_file())
                return original_collect(*args, **kwargs)

            with mock.patch.object(
                monitor,
                "collect_configuration",
                side_effect=inspect_prequery,
            ):
                owner = self._owner("diagnostic")
                app._configuration_worker(owner, "simulate", "nominal", output_root)

            queued_owner, event, payload = app.events.get_nowait()
            self.assertIs(queued_owner, owner)
            self.assertEqual(event, "configuration")
            report, run_directory, snapshot, journal, context = payload
            self.assertEqual(journal.state, monitor.DiagnosticState.OBSERVE_READY)
            self.assertTrue(snapshot.is_file())
            self.assertEqual(
                len(context.query_history),
                report["query_plan"]["candidate_count"],
            )
            self.assertTrue(report["diagnostics"]["can_start_live"])

            manifest = json.loads((run_directory / "run-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["state"], "OBSERVE_READY")
            self.assertTrue(manifest["capabilities"]["live_authorized"])
            self.assertEqual(
                set(manifest["safety"]["allowed_queries"]),
                set(monitor.ALLOWED_QUERIES),
            )
            event_records = [
                json.loads(line)
                for line in (run_directory / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            event_types = [record["event_type"] for record in event_records]
            self.assertEqual(
                event_types[:4],
                [
                    "run_created",
                    "state_transition",
                    "configuration_started",
                    "query_plan_committed",
                ],
            )
            started = next(
                record
                for record in event_records
                if record["event_type"] == "configuration_started"
            )
            committed = next(
                record
                for record in event_records
                if record["event_type"] == "query_plan_committed"
            )
            self.assertEqual(
                started["payload"]["candidate_query_count"],
                report["query_plan"]["candidate_count"],
            )
            self.assertEqual(
                [item["command"] for item in committed["payload"]["queries"]],
                [item["command"] for item in context.query_history],
            )
            journal.finalize("test_complete")
            finalized_manifest = json.loads(
                journal.manifest_path.read_text(encoding="utf-8")
            )
            self.assertEqual(
                finalized_manifest["readiness"]["health_axes"]["evidence_complete"],
                "PASS",
            )

    def test_event_write_failure_stops_before_any_simulated_query(self):
        app = self._worker_app()
        with tempfile.TemporaryDirectory() as temp_dir:
            app._configuration_worker(
                self._owner("diagnostic"),
                "simulate",
                "event_write_failure",
                Path(temp_dir),
            )

            _owner, event, payload = app.events.get_nowait()
            self.assertEqual(event, "configuration_error")
            _message, _run_directory, failure_file, journal, context = payload
            self.assertIsNotNone(failure_file)
            self.assertEqual(context.query_history, [])
            self.assertIsNotNone(journal)
            self.assertEqual(journal.state, monitor.DiagnosticState.VERIFYING_IDENTITY)
            manifest = json.loads(journal.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["state"], "VERIFYING_IDENTITY")

    def test_configuration_fault_matrix(self):
        cases = {
            "nominal": (True, "WARN", 51),
            "wrong_identity": (False, "BLOCKED", 1),
            "malformed_identity": (False, "BLOCKED", 1),
            "configuration_missing": (False, "BLOCKED", 51),
            "configuration_drift": (False, "BLOCKED", 51),
            "configuration_timeout": (False, "BLOCKED", 10),
            "configuration_slow": (True, "WARN", 51),
            "configuration_close_failure": (False, "BLOCKED", 51),
        }
        for scenario, (ready, overall, query_count) in cases.items():
            with self.subTest(scenario=scenario):
                context = monitor.SimulationContext(scenario, monitor.ALLOWED_QUERIES)
                report = monitor.collect_configuration("simulate", context)
                self.assertEqual(report["diagnostics"]["can_start_live"], ready)
                self.assertEqual(report["diagnostics"]["overall"], overall)
                self.assertEqual(len(context.query_history), query_count)
                if scenario == "configuration_drift":
                    consistency = next(
                        check
                        for check in report["diagnostics"]["checks"]
                        if check["check_id"]
                        == "communication.sentinel_consistency"
                    )
                    self.assertEqual(consistency["status"], "BLOCKED")
                    self.assertEqual(
                        consistency["observed"]["mismatches"],
                        ["active_channel"],
                    )
                self.assertTrue(
                    all(
                        item["command"] in monitor.ALLOWED_QUERIES
                        for item in context.query_history
                    )
                )

    def test_csv_open_failure_opens_no_live_session(self):
        app = self._worker_app()
        context = monitor.SimulationContext("csv_open_failure", monitor.ALLOWED_QUERIES)
        with tempfile.TemporaryDirectory() as temp_dir:
            csv_path = Path(temp_dir) / "voltage-test.csv"
            app._stream_worker(
                self._owner("live"),
                "simulate",
                csv_path,
                context,
                target=LIVE_TARGET,
            )

            self.assertFalse(csv_path.exists())
            self.assertEqual(context.query_history, [])
            queued_events = []
            while not app.events.empty():
                queued_events.append(app.events.get_nowait()[1])
            self.assertEqual(queued_events, ["stream_error", "stream_stopped"])

    def test_third_fetch_timeout_latches_fault_and_preserves_two_samples(self):
        app = self._worker_app()
        context = monitor.SimulationContext("fetch_timeout", monitor.ALLOWED_QUERIES)

        with tempfile.TemporaryDirectory() as temp_dir:
            run_directory = Path(temp_dir) / "run"
            run_directory.mkdir()
            journal = monitor.RunJournal(
                run_directory,
                mode="simulate",
                allowed_queries=monitor.ALLOWED_QUERIES,
                fault_scenario="fetch_timeout",
            )
            journal.transition(
                monitor.DiagnosticState.VERIFYING_IDENTITY,
                reason_code="TEST_VERIFY",
            )
            journal.transition(
                monitor.DiagnosticState.CHECKING_CONFIG,
                reason_code="TEST_CONFIG",
            )
            journal.transition(
                monitor.DiagnosticState.OBSERVE_READY,
                reason_code="TEST_READY",
            )
            csv_path = run_directory / "voltage-test.csv"
            stream_id = journal.register_stream(csv_path)

            app._stream_worker(
                self._owner(
                    "live",
                    run_id=journal.run_id,
                    stream_id=stream_id,
                ),
                "simulate",
                csv_path,
                context,
                journal,
                stream_id,
                LIVE_TARGET,
            )

            self.assertEqual(journal.state, monitor.DiagnosticState.FAULT_LATCHED)
            with csv_path.open("r", newline="", encoding="utf-8-sig") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(tuple(row) == monitor.CSV_FIELDS for row in rows))
            self.assertTrue(all("record_type" not in row for row in rows))
            self.assertEqual(
                [item["command"] for item in context.query_history],
                ["*IDN?", "FETCh?", "FETCh?", "FETCh?"],
            )
            manifest = json.loads(journal.manifest_path.read_text(encoding="utf-8"))
            stream = manifest["artifacts"]["streams"][0]
            quality_path = run_directory / stream["quality"]
            quality = json.loads(quality_path.read_text(encoding="utf-8"))
            self.assertEqual(quality["source_csv"], csv_path.name)
            self.assertEqual(quality["sample_count"], 2)
            journal.finalize("test_complete")

    def test_fetch_fault_ends_active_intervention_before_stream_finalization(self):
        app = self._worker_app()
        app.poll_interval_s = 0.05
        context = monitor.SimulationContext("fetch_timeout", monitor.ALLOWED_QUERIES)

        with tempfile.TemporaryDirectory() as temp_dir:
            run_directory = Path(temp_dir) / "run"
            run_directory.mkdir()
            journal = monitor.RunJournal(
                run_directory,
                mode="simulate",
                allowed_queries=monitor.ALLOWED_QUERIES,
                fault_scenario="fetch_timeout",
            )
            journal.transition(
                monitor.DiagnosticState.VERIFYING_IDENTITY,
                reason_code="TEST_VERIFY",
            )
            journal.transition(
                monitor.DiagnosticState.CHECKING_CONFIG,
                reason_code="TEST_CONFIG",
            )
            journal.transition(
                monitor.DiagnosticState.OBSERVE_READY,
                reason_code="TEST_READY",
            )
            csv_path = run_directory / "voltage-test.csv"
            stream_id = journal.register_stream(csv_path)
            owner = self._owner(
                "live",
                run_id=journal.run_id,
                stream_id=stream_id,
            )
            worker = threading.Thread(
                target=app._stream_worker,
                args=(
                    owner,
                    "simulate",
                    csv_path,
                    context,
                    journal,
                    stream_id,
                    LIVE_TARGET,
                ),
            )
            worker.start()

            deadline = time.monotonic() + 2.0
            while journal.state != monitor.DiagnosticState.LIVE and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertEqual(journal.state, monitor.DiagnosticState.LIVE)
            journal.start_intervention(
                stream_id,
                elapsed_seconds=app._intervention_elapsed(),
                intervention_type="cable_disturbance",
                location="rear input cable",
            )
            worker.join(timeout=3.0)
            self.assertFalse(worker.is_alive())

            interventions = [
                json.loads(line)
                for line in journal.interventions_path.read_text(encoding="utf-8").splitlines()
            ]
            manifest = json.loads(journal.manifest_path.read_text(encoding="utf-8"))
            stream = manifest["artifacts"]["streams"][0]
            queued = []
            while not app.events.empty():
                queued.append(app.events.get_nowait())

            self.assertEqual([record["phase"] for record in interventions], ["start", "end"])
            self.assertEqual(
                interventions[0]["intervention_id"],
                interventions[1]["intervention_id"],
            )
            self.assertGreaterEqual(
                interventions[1]["elapsed_seconds"],
                interventions[0]["elapsed_seconds"],
            )
            self.assertEqual(stream["intervention_count"], 1)
            self.assertEqual(stream["status"], "closed")
            self.assertEqual(stream["outcome"], "fault")
            self.assertEqual(
                [item["command"] for item in context.query_history],
                ["*IDN?", "FETCh?", "FETCh?", "FETCh?"],
            )
            stream_error_payload = next(
                payload
                for queued_owner, event, payload in queued
                if queued_owner is owner and event == "stream_error"
            )
            self.assertEqual(stream_error_payload["intervention_end"]["phase"], "end")
            journal.finalize("test_complete")

    def test_live_fault_matrix(self):
        cases = {
            "fetch_timeout": (2, 4),
            "fetch_malformed": (1, 3),
            "fetch_nan": (1, 3),
            "fetch_inf": (1, 3),
            "fetch_overrange": (1, 3),
            "disconnect_after_3": (2, 4),
            "csv_open_failure": (0, 0),
            "csv_write_failure": (2, 4),
        }
        for scenario, (expected_samples, expected_queries) in cases.items():
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as temp_dir:
                app = self._worker_app()
                context = monitor.SimulationContext(scenario, monitor.ALLOWED_QUERIES)
                run_directory = Path(temp_dir) / "run"
                run_directory.mkdir()
                journal = monitor.RunJournal(
                    run_directory,
                    mode="simulate",
                    allowed_queries=monitor.ALLOWED_QUERIES,
                    fault_scenario=scenario,
                )
                journal.transition(
                    monitor.DiagnosticState.VERIFYING_IDENTITY,
                    reason_code="TEST_VERIFY",
                )
                journal.transition(
                    monitor.DiagnosticState.CHECKING_CONFIG,
                    reason_code="TEST_CONFIG",
                )
                journal.transition(
                    monitor.DiagnosticState.OBSERVE_READY,
                    reason_code="TEST_READY",
                )
                csv_path = run_directory / "voltage-test.csv"
                stream_id = journal.register_stream(csv_path)

                app._stream_worker(
                    self._owner(
                        "live",
                        run_id=journal.run_id,
                        stream_id=stream_id,
                    ),
                    "simulate",
                    csv_path,
                    context,
                    journal,
                    stream_id,
                    LIVE_TARGET,
                )

                self.assertEqual(journal.state, monitor.DiagnosticState.FAULT_LATCHED)
                self.assertEqual(len(context.query_history), expected_queries)
                self.assertTrue(
                    all(
                        item["command"] in monitor.ALLOWED_QUERIES
                        for item in context.query_history
                    )
                )
                if csv_path.exists():
                    with csv_path.open("r", newline="", encoding="utf-8-sig") as handle:
                        rows = list(csv.DictReader(handle))
                else:
                    rows = []
                self.assertEqual(len(rows), expected_samples)
                manifest = json.loads(journal.manifest_path.read_text(encoding="utf-8"))
                stream = manifest["artifacts"]["streams"][0]
                fault_evidence = stream["fault_injection"]
                self.assertEqual(fault_evidence["scenario"], scenario)
                self.assertEqual(
                    fault_evidence["query_history"],
                    context.query_history,
                )
                self.assertEqual(
                    fault_evidence["consumed_rule_ids"],
                    context.consumed_rule_ids,
                )
                journal.finalize("test_complete")

    def test_slow_fetch_transitions_degraded_then_recovers(self):
        app = self._worker_app()
        app.poll_interval_s = 0.25
        context = monitor.SimulationContext("fetch_slow", monitor.ALLOWED_QUERIES)

        with tempfile.TemporaryDirectory() as temp_dir:
            run_directory = Path(temp_dir) / "run"
            run_directory.mkdir()
            journal = monitor.RunJournal(
                run_directory,
                mode="simulate",
                allowed_queries=monitor.ALLOWED_QUERIES,
                fault_scenario="fetch_slow",
            )
            journal.transition(
                monitor.DiagnosticState.VERIFYING_IDENTITY,
                reason_code="TEST_VERIFY",
            )
            journal.transition(
                monitor.DiagnosticState.CHECKING_CONFIG,
                reason_code="TEST_CONFIG",
            )
            journal.transition(
                monitor.DiagnosticState.OBSERVE_READY,
                reason_code="TEST_READY",
            )
            csv_path = run_directory / "voltage-test.csv"
            stream_id = journal.register_stream(csv_path)
            owner = self._owner(
                "live",
                run_id=journal.run_id,
                stream_id=stream_id,
            )
            worker = threading.Thread(
                target=app._stream_worker,
                args=(
                    owner,
                    "simulate",
                    csv_path,
                    context,
                    journal,
                    stream_id,
                    LIVE_TARGET,
                ),
                daemon=True,
            )
            worker.start()

            observed_samples = 0
            deadline = time.monotonic() + 3.0
            while observed_samples < 3 and time.monotonic() < deadline:
                queued_owner, event, _payload = app.events.get(timeout=1.0)
                self.assertIs(queued_owner, owner)
                if event == "sample":
                    observed_samples += 1
            app.stop_event.set()
            worker.join(timeout=2.0)

            self.assertFalse(worker.is_alive())
            self.assertGreaterEqual(observed_samples, 3)
            self.assertEqual(journal.state, monitor.DiagnosticState.OBSERVE_READY)
            reasons = [
                json.loads(line)["reason_code"]
                for line in journal.events_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertIn("POLL_DEADLINE_MISSED", reasons)
            self.assertIn("POLL_TIMING_RECOVERED", reasons)
            self.assertIn("LIVE_STREAM_PAUSED", reasons)
            journal.finalize("test_complete")

    def test_stream_finalization_failure_is_fault_latched(self):
        app = self._worker_app()

        class OneSampleSession:
            def __enter__(self):
                return self

            def query(self, command):
                if command == "*IDN?":
                    return monitor.SIMULATED_VALUES["*IDN?"]
                if command == monitor.FETCH_QUERY:
                    app.stop_event.set()
                    return "+1.00000000E-07"
                raise AssertionError(command)

            def __exit__(self, exc_type, exc, traceback):
                return None

        with tempfile.TemporaryDirectory() as temp_dir:
            run_directory = Path(temp_dir) / "run"
            run_directory.mkdir()
            journal = monitor.RunJournal(
                run_directory,
                mode="simulate",
                allowed_queries=monitor.ALLOWED_QUERIES,
            )
            journal.transition(
                monitor.DiagnosticState.VERIFYING_IDENTITY,
                reason_code="TEST_VERIFY",
            )
            journal.transition(
                monitor.DiagnosticState.CHECKING_CONFIG,
                reason_code="TEST_CONFIG",
            )
            journal.transition(
                monitor.DiagnosticState.OBSERVE_READY,
                reason_code="TEST_READY",
            )
            csv_path = run_directory / "voltage-test.csv"
            stream_id = journal.register_stream(csv_path)

            with mock.patch.object(monitor, "session_factory", return_value=OneSampleSession()), mock.patch.object(
                journal,
                "finish_stream",
                side_effect=monitor.RecorderError("simulated finish failure"),
            ):
                app._stream_worker(
                    self._owner(
                        "live",
                        run_id=journal.run_id,
                        stream_id=stream_id,
                    ),
                    "simulate",
                    csv_path,
                    None,
                    journal,
                    stream_id,
                    LIVE_TARGET,
                )

            self.assertEqual(journal.state, monitor.DiagnosticState.FAULT_LATCHED)
            queued = []
            while not app.events.empty():
                queued.append(app.events.get_nowait())
            self.assertIn(
                "stream_error",
                [event for _owner, event, _payload in queued],
            )
            stopped = next(
                payload
                for _owner, event, payload in queued
                if event == "stream_stopped"
            )
            self.assertIn("simulated finish failure", stopped["error"])
            with self.assertRaisesRegex(
                monitor.RecorderError,
                "evidence verification failed",
            ):
                journal.finalize("test_complete")
            manifest = json.loads(journal.manifest_path.read_text(encoding="utf-8"))
            self.assertTrue(manifest["final"]["closed"])
            self.assertFalse(manifest["final"]["evidence_complete"])

    def test_stream_identity_change_blocks_fetch_and_closes_session(self):
        app = self._worker_app()
        calls = []
        exits = []

        class WrongIdentitySession:
            def __enter__(self):
                return self

            def query(self, command):
                calls.append(command)
                return "KEITHLEY INSTRUMENTS INC.,MODEL 2182A,4510267,C08/B01"

            def __exit__(self, exc_type, exc, traceback):
                exits.append((exc_type, exc))

        with tempfile.TemporaryDirectory() as temp_dir:
            run_directory = Path(temp_dir) / "run"
            run_directory.mkdir()
            journal = monitor.RunJournal(
                run_directory,
                mode="simulate",
                allowed_queries=monitor.ALLOWED_QUERIES,
            )
            journal.transition(
                monitor.DiagnosticState.VERIFYING_IDENTITY,
                reason_code="TEST_VERIFY",
            )
            journal.transition(
                monitor.DiagnosticState.CHECKING_CONFIG,
                reason_code="TEST_CONFIG",
            )
            journal.transition(
                monitor.DiagnosticState.OBSERVE_READY,
                reason_code="TEST_READY",
            )
            csv_path = run_directory / "voltage-test.csv"
            stream_id = journal.register_stream(csv_path)

            with mock.patch.object(
                monitor,
                "session_factory",
                return_value=WrongIdentitySession(),
            ):
                app._stream_worker(
                    self._owner(
                        "live",
                        run_id=journal.run_id,
                        stream_id=stream_id,
                    ),
                    "simulate",
                    csv_path,
                    None,
                    journal,
                    stream_id,
                    LIVE_TARGET,
                )

            self.assertEqual(calls, ["*IDN?"])
            self.assertEqual(len(exits), 1)
            self.assertEqual(journal.state, monitor.DiagnosticState.FAULT_LATCHED)
            journal.finalize("test_complete")

    def test_stream_session_open_failure_latches_without_query(self):
        app = self._worker_app()
        with tempfile.TemporaryDirectory() as temp_dir:
            run_directory = Path(temp_dir) / "run"
            run_directory.mkdir()
            journal = monitor.RunJournal(
                run_directory,
                mode="simulate",
                allowed_queries=monitor.ALLOWED_QUERIES,
            )
            journal.transition(
                monitor.DiagnosticState.VERIFYING_IDENTITY,
                reason_code="TEST_VERIFY",
            )
            journal.transition(
                monitor.DiagnosticState.CHECKING_CONFIG,
                reason_code="TEST_CONFIG",
            )
            journal.transition(
                monitor.DiagnosticState.OBSERVE_READY,
                reason_code="TEST_READY",
            )
            csv_path = run_directory / "voltage-test.csv"
            stream_id = journal.register_stream(csv_path)

            with mock.patch.object(
                monitor,
                "session_factory",
                side_effect=RuntimeError("simulated session open failure"),
            ):
                app._stream_worker(
                    self._owner(
                        "live",
                        run_id=journal.run_id,
                        stream_id=stream_id,
                    ),
                    "simulate",
                    csv_path,
                    None,
                    journal,
                    stream_id,
                    LIVE_TARGET,
                )

            self.assertEqual(journal.state, monitor.DiagnosticState.FAULT_LATCHED)
            events = []
            while not app.events.empty():
                events.append(app.events.get_nowait())
            self.assertIn(
                "stream_error",
                [event for _owner, event, _payload in events],
            )
            journal.finalize("test_complete")


if __name__ == "__main__":
    unittest.main()
