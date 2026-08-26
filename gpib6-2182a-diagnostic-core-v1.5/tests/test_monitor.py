import csv
import importlib.util
import math
import queue
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "gpib6_2182a_monitor.py"
SPEC = importlib.util.spec_from_file_location("gpib6_2182a_monitor", MODULE_PATH)
monitor = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(monitor)


class MonitorTests(unittest.TestCase):
    def test_primary_diagnostic_issue_prefers_first_blocker(self):
        report = {
            "diagnostics": {
                "checks": [
                    {
                        "check_id": "configuration.warning",
                        "status": "WARN",
                        "message": "warning detail",
                    },
                    {
                        "check_id": "safety.output",
                        "status": "BLOCKED",
                        "message": "output is enabled",
                    },
                ]
            },
            "transcript": [],
        }

        issue = monitor.primary_diagnostic_issue(report)

        self.assertEqual(issue, "BLOCKED safety.output · output is enabled")

    def test_inventory_status_key_changes_with_snapshot(self):
        first = monitor.build_simulated_inventory()
        second = monitor.build_simulated_inventory()
        first_entry = next(
            entry for entry in first.entries if entry.resource == "GPIB0::6::INSTR"
        )
        second_entry = next(
            entry for entry in second.entries if entry.resource == "GPIB0::6::INSTR"
        )

        self.assertNotEqual(
            monitor.instrument_status_key(first, first_entry),
            monitor.instrument_status_key(second, second_entry),
        )

    def test_retained_status_is_display_only_and_stale(self):
        class Var:
            def __init__(self, value=""):
                self.value = value

            def get(self):
                return self.value

            def set(self, value):
                self.value = value

        class Tree:
            def __init__(self):
                self.rows = []

            def get_children(self):
                return tuple(range(len(self.rows)))

            def delete(self, *_items):
                self.rows = []

            def insert(self, _parent, _position, *, values, tags):
                self.rows.append((values, tags))

        class Button:
            def __init__(self):
                self.state = None

            def configure(self, **values):
                self.state = values.get("state", self.state)

        snapshot = monitor.build_simulated_inventory()
        entry = next(
            item for item in snapshot.entries if item.resource == "GPIB0::6::INSTR"
        )
        target = monitor.target_from_inventory_entry(entry)
        report = monitor.collect_configuration("simulate", target=target)
        key = monitor.instrument_status_key(snapshot, entry)
        app = monitor.MonitorApp.__new__(monitor.MonitorApp)
        app.inventory_snapshot = snapshot
        app.inventory_label_to_entry = {"selected": entry}
        app.selected_inventory_label = "selected"
        app.instrument_status_by_key = {
            key: {
                "report": report,
                "checked_at": report["created_at"],
                "evidence": "configuration-snapshot.json",
            }
        }
        app.configuration = None
        app.diagnostic_target = None
        app.summary_tree = Tree()
        app.config_tree = Tree()
        app.diagnostic_state_var = Var()
        app.readiness_var = Var()
        app.health_axes_var = Var()
        app.primary_issue_var = Var()
        app.evidence_var = Var()
        app.status_var = Var()
        app.start_button = Button()
        app.single_button = Button()

        shown = app._show_retained_status()

        self.assertTrue(shown)
        self.assertIsNone(app.configuration)
        self.assertIsNone(app.diagnostic_target)
        self.assertIn("STALE", app.readiness_var.get())
        self.assertEqual(app.start_button.state, "disabled")
        self.assertEqual(app.single_button.state, "disabled")

    def test_inventory_status_overview_marks_noncurrent_result_stale(self):
        class Tree:
            def __init__(self):
                self.rows = {}
                self.selected = None

            def get_children(self):
                return tuple(self.rows)

            def delete(self, *_items):
                self.rows = {}

            def insert(self, _parent, _position, *, iid, values, tags):
                self.rows[iid] = (values, tags)

            def selection_set(self, item_id):
                self.selected = item_id

        snapshot = monitor.build_simulated_inventory()
        entries = {entry.resource: entry for entry in snapshot.entries}
        selected_entry = entries["GPIB0::6::INSTR"]
        selected_target = monitor.target_from_inventory_entry(selected_entry)
        report = monitor.collect_configuration("simulate", target=selected_target)
        app = monitor.MonitorApp.__new__(monitor.MonitorApp)
        app.inventory_snapshot = snapshot
        app.inventory_label_to_entry = {
            entry.resource: entry for entry in snapshot.entries
        }
        app.selected_inventory_label = selected_entry.resource
        app.inventory_tree_label_by_iid = {}
        app.inventory_tree = Tree()
        app.configuration = report
        app.diagnostic_target = selected_target
        app.current_diagnostic_status_key = monitor.instrument_status_key(
            snapshot, selected_entry
        )
        app.instrument_status_by_key = {
            monitor.instrument_status_key(snapshot, selected_entry): {
                "report": report,
                "overall": "PASS",
                "primary_issue": monitor.primary_diagnostic_issue(report),
                "checked_at": report["created_at"],
            }
        }

        app._refresh_inventory_status_tree()

        self.assertEqual(len(app.inventory_tree.rows), 5)
        current_row = next(
            values
            for values, _tags in app.inventory_tree.rows.values()
            if values[0] == "GPIB0::6::INSTR"
        )
        self.assertEqual(current_row[4], "PASS")

        app.configuration = None
        app.diagnostic_target = None
        app.current_diagnostic_status_key = None
        app._refresh_inventory_status_tree()
        stale_row = next(
            values
            for values, _tags in app.inventory_tree.rows.values()
            if values[0] == "GPIB0::6::INSTR"
        )
        self.assertEqual(stale_row[4], "PASS · STALE")

    def test_pending_configuration_result_blocks_instrument_change(self):
        class Var:
            def __init__(self, value):
                self.value = value

            def get(self):
                return self.value

            def set(self, value):
                self.value = value

        snapshot = monitor.build_simulated_inventory()
        entries = {entry.resource: entry for entry in snapshot.entries}
        app = monitor.MonitorApp.__new__(monitor.MonitorApp)
        app.inventory_label_to_entry = entries
        app.selected_inventory_label = "GPIB0::6::INSTR"
        app.target_var = Var("GPIB0::9::INSTR")
        app.operation_pending = True
        app.worker = mock.Mock()
        app.worker.is_alive.return_value = False
        app.messagebox = mock.Mock()
        app._refresh_inventory_status_tree = mock.Mock()
        app._invalidate_current_diagnostics = mock.Mock()

        app._target_changed()

        self.assertEqual(app.target_var.get(), "GPIB0::6::INSTR")
        app._invalidate_current_diagnostics.assert_not_called()
        app.messagebox.showwarning.assert_called_once()

    def test_live_terminal_event_owns_busy_state_until_matching_stop_is_drained(self):
        class Var:
            def __init__(self, value):
                self.value = value

            def get(self):
                return self.value

            def set(self, value):
                self.value = value

        class Widget:
            def __init__(self):
                self.state = None

            def configure(self, **values):
                self.state = values.get("state", self.state)

        class Journal:
            state = monitor.DiagnosticState.OBSERVE_READY

            def __init__(self, run_directory):
                self.register_calls = 0
                self.run_id = "run-1"
                self.manifest_path = run_directory / "manifest.json"
                self.events_path = run_directory / "events.jsonl"
                self.interventions_path = run_directory / "interventions.jsonl"

            def register_stream(self, _csv_path):
                self.register_calls += 1
                return "stream-1"

        class FinishedThread:
            def __init__(self, *, target, args, daemon):
                self.target = target
                self.args = args
                self.daemon = daemon

            def start(self):
                return None

            def is_alive(self):
                return False

        snapshot = monitor.build_simulated_inventory()
        entries = {entry.resource: entry for entry in snapshot.entries}
        target = monitor.target_from_inventory_entry(entries["GPIB0::6::INSTR"])
        app = monitor.MonitorApp.__new__(monitor.MonitorApp)
        app.worker = None
        app.operation_pending = False
        app.active_operation = None
        app.recording_fault_latched = False
        app.stream_had_error = False
        app.configuration = {
            "inventory_snapshot_id": snapshot.snapshot_id,
            "diagnostics": {"can_start_live": True},
            "capabilities": {
                "live_supported": True,
                "live_authorized": True,
            },
        }
        app.diagnostic_target = target
        app.selected_target = target
        app.inventory_usable = True
        app.inventory_label_to_entry = entries
        app.selected_inventory_label = "GPIB0::6::INSTR"
        app.target_var = Var("GPIB0::6::INSTR")
        app.mode_var = Var("simulate")
        app.fault_var = Var("nominal")
        app.selected_fault = "nominal"
        app.simulation_context = None
        app.poll_interval_s = 0.5
        app.stop_event = threading.Event()
        app.events = queue.Queue()
        app.samples = []
        app.interventions = []
        app.active_intervention = None
        app.intervention_ready = False
        app.stream_id = None
        app.live_running = False
        app.stream_stop_fault = None
        app.closing = True
        app.messagebox = mock.Mock()
        app._confirm_real_access = lambda: True
        app._draw_plot = lambda: None
        app._refresh_inventory_status_tree = mock.Mock()
        app._invalidate_current_diagnostics = mock.Mock()
        for name in (
            "start_button",
            "config_button",
            "inventory_button",
            "target_combo",
            "mode_combo",
            "fault_combo",
            "output_button",
            "clear_button",
            "single_button",
            "pause_button",
            "mark_intervention_button",
            "intervention_type_combo",
            "intervention_location_entry",
        ):
            setattr(app, name, Widget())
        for name in (
            "reading_var",
            "raw_var",
            "status_var",
            "evidence_var",
            "diagnostic_state_var",
        ):
            setattr(app, name, Var(""))

        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            monitor.threading,
            "Thread",
            FinishedThread,
        ):
            app.run_directory = Path(temp_dir)
            app.diagnostic_run = Journal(app.run_directory)
            app._start_stream()

            self.assertFalse(app.worker.is_alive())
            self.assertTrue(app.operation_pending)
            self.assertEqual(app.diagnostic_run.register_calls, 1)
            active_owner = app.active_operation
            self.assertIsNotNone(active_owner)
            self.assertEqual(active_owner.kind, "live")
            self.assertEqual(active_owner.stream_id, "stream-1")
            app._start_stream()
            self.assertEqual(app.diagnostic_run.register_calls, 1)

            app.target_var.set("GPIB0::9::INSTR")
            app._target_changed()
            self.assertEqual(app.target_var.get(), "GPIB0::6::INSTR")
            app._invalidate_current_diagnostics.assert_not_called()

            app.events.put(
                (
                    active_owner._replace(target_key="6221-gpib9"),
                    "stream_stopped",
                    {"sample_count": 0, "error": None, "stream_id": "stream-1"},
                )
            )
            app._drain_events()
            self.assertTrue(app.operation_pending)
            self.assertEqual(app.stream_id, "stream-1")
            self.assertEqual(app.target_combo.state, "disabled")

            app.events.put(
                (
                    active_owner,
                    "stream_stopped",
                    {"sample_count": 0, "error": None, "stream_id": "stale-stream"},
                )
            )
            app._drain_events()
            self.assertTrue(app.operation_pending)
            self.assertEqual(app.stream_id, "stream-1")

            app.events.put(
                (
                    active_owner,
                    "stream_stopped",
                    {"sample_count": 0, "error": None, "stream_id": "stream-1"},
                )
            )
            app._drain_events()

        self.assertFalse(app.operation_pending)
        self.assertIsNone(app.stream_id)
        self.assertEqual(app.target_combo.state, "readonly")
        self.assertEqual(app.start_button.state, "normal")

    def test_stale_worker_events_are_rejected_before_any_ui_state_mutation(self):
        app = monitor.MonitorApp.__new__(monitor.MonitorApp)
        active_owner = monitor.OperationOwner(
            "active-operation",
            "diagnostic",
            "simulate",
            target_key="2182a-gpib6",
            inventory_snapshot_id="snapshot-1",
        )
        app.active_operation = active_owner
        app.operation_pending = True
        app.events = queue.Queue()
        app.closing = True

        stale_owner = active_owner._replace(operation_id="stale-operation")
        mismatched_owner = active_owner._replace(target_key="6221-gpib9")
        for owner, event in (
            (stale_owner, "inventory"),
            (stale_owner, "configuration"),
            (stale_owner, "sample"),
            (stale_owner, "stream_error"),
            (stale_owner, "stream_stopped"),
            (stale_owner, "single"),
            (mismatched_owner, "configuration_error"),
        ):
            app.events.put((owner, event, object()))

        app._drain_events()

        self.assertIs(app.active_operation, active_owner)
        self.assertTrue(app.operation_pending)

    def test_success_cache_uses_frozen_status_key_not_current_selection(self):
        snapshot = monitor.build_simulated_inventory()
        entries = {entry.resource: entry for entry in snapshot.entries}
        frozen_entry = entries["GPIB0::6::INSTR"]
        current_entry = entries["GPIB0::9::INSTR"]
        frozen_target = monitor.target_from_inventory_entry(frozen_entry)
        report = monitor.collect_configuration("simulate", target=frozen_target)
        frozen_key = monitor.instrument_status_key(snapshot, frozen_entry)
        current_key = monitor.instrument_status_key(snapshot, current_entry)
        app = monitor.MonitorApp.__new__(monitor.MonitorApp)
        app.inventory_snapshot = snapshot
        app.inventory_label_to_entry = {"current": current_entry}
        app.selected_inventory_label = "current"
        app.instrument_status_by_key = {}
        app.pending_diagnostic_status_key = frozen_key
        app.current_diagnostic_status_key = None
        app.inventory_tree = None

        app._cache_diagnostic_status(report, evidence="snapshot.json")

        self.assertIn(frozen_key, app.instrument_status_by_key)
        self.assertNotIn(current_key, app.instrument_status_by_key)
        self.assertEqual(app.current_diagnostic_status_key, frozen_key)

    def test_current_diagnostic_failure_is_not_marked_stale(self):
        class Tree:
            def __init__(self):
                self.rows = {}

            def get_children(self):
                return tuple(self.rows)

            def delete(self, *_items):
                self.rows = {}

            def insert(self, _parent, _position, *, iid, values, tags):
                self.rows[iid] = (values, tags)

            def selection_set(self, _item_id):
                return None

        snapshot = monitor.build_simulated_inventory()
        entry = next(
            item for item in snapshot.entries if item.resource == "GPIB0::6::INSTR"
        )
        status_key = monitor.instrument_status_key(snapshot, entry)
        app = monitor.MonitorApp.__new__(monitor.MonitorApp)
        app.inventory_snapshot = snapshot
        app.inventory_label_to_entry = {"selected": entry}
        app.selected_inventory_label = "selected"
        app.instrument_status_by_key = {}
        app.pending_diagnostic_status_key = status_key
        app.current_diagnostic_status_key = None
        app.inventory_tree_label_by_iid = {}
        app.inventory_tree = Tree()
        app.configuration = None
        app.diagnostic_target = None

        app._cache_diagnostic_failure("simulated failure", "failure.json")

        row = next(iter(app.inventory_tree.rows.values()))[0]
        self.assertEqual(row[4], "BLOCKED")

    def test_fault_selection_change_invalidates_existing_diagnostics(self):
        class Var:
            def __init__(self, value):
                self.value = value

            def get(self):
                return self.value

            def set(self, value):
                self.value = value

        app = monitor.MonitorApp.__new__(monitor.MonitorApp)
        app.worker = None
        app.selected_fault = "nominal"
        app.fault_var = Var("fetch_timeout")
        app.configuration = {"diagnostics": {"can_start_live": True}}
        app.diagnostic_run = None
        invalidations = []
        statuses = []
        app._invalidate_current_diagnostics = invalidations.append
        app.status_var = mock.Mock(set=statuses.append)

        app._fault_changed()

        self.assertEqual(app.selected_fault, "fetch_timeout")
        self.assertEqual(invalidations, ["fault_scenario_changed"])
        self.assertIn("run Read-Only Diagnostics again", statuses[-1])

    def test_inventory_snapshot_preserves_only_fault_valid_for_selected_target(self):
        class Var:
            def __init__(self, value):
                self.value = value

            def get(self):
                return self.value

            def set(self, value):
                self.value = value

        snapshot = monitor.build_simulated_inventory()
        for selected_fault, expected_fault in (
            ("6221_output_on", "6221_output_on"),
            ("fetch_timeout", "nominal"),
        ):
            with self.subTest(selected_fault=selected_fault):
                app = monitor.MonitorApp.__new__(monitor.MonitorApp)
                app.target_combo = mock.Mock()
                app.target_var = Var("")
                app.inventory_status_var = Var("")
                app.config_button = mock.Mock()
                app.fault_combo = mock.Mock()
                app.mode_var = Var("simulate")
                app.fault_var = Var(selected_fault)
                app.selected_fault = selected_fault
                app._refresh_inventory_status_tree = mock.Mock()
                app._update_target_presentation = mock.Mock()

                app._install_inventory_snapshot(
                    snapshot,
                    preferred_resource="GPIB0::9::INSTR",
                )

                self.assertEqual(app.selected_target.profile_key, "6221")
                self.assertEqual(app.selected_fault, expected_fault)
                self.assertEqual(app.fault_var.get(), expected_fault)

    def test_atomic_json_writer_leaves_only_complete_target(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "snapshot.json"
            monitor.write_json_atomic(target, {"state": "OBSERVE_READY"})

            self.assertEqual(
                target.read_text(encoding="utf-8"),
                '{\n  "state": "OBSERVE_READY"\n}\n',
            )
            self.assertFalse(target.with_suffix(".json.tmp").exists())

    def test_empty_configuration_response_is_not_displayed_as_pass(self):
        class Tree:
            def __init__(self):
                self.rows = []

            def get_children(self):
                return ()

            def delete(self, *_items):
                return None

            def insert(self, _parent, _position, *, values, tags):
                self.rows.append((values, tags))

        app = monitor.MonitorApp.__new__(monitor.MonitorApp)
        app.config_tree = Tree()
        app._show_configuration(
            {
                "transcript": [
                    {
                        "name": "active_channel",
                        "command": "SENS:CHAN?",
                        "ok": True,
                        "response": "",
                    }
                ],
                "diagnostics": {"checks": []},
            }
        )

        values, tags = app.config_tree.rows[0]
        self.assertEqual(values[0], "BLOCKED")
        self.assertEqual(tags, ("BLOCKED",))

    def test_parse_voltage(self):
        self.assertAlmostEqual(monitor.parse_voltage("+1.08673749E-07"), 1.08673749e-7)
        self.assertAlmostEqual(monitor.parse_voltage("-2.5E-6,extra"), -2.5e-6)

    def test_non_numeric_voltage_rejected(self):
        with self.assertRaises(ValueError):
            monitor.parse_voltage("not-a-number")
        with self.assertRaises(ValueError):
            monitor.parse_voltage("+9.9E37")

    def test_poll_interval_uses_real_baseline(self):
        values = {"nplc": "5.00", "line_frequency_hz": "50"}
        self.assertEqual(monitor.derive_poll_interval(values), 0.25)

    def test_allowlist_has_only_queries(self):
        self.assertIn("FETCh?", monitor.ALLOWED_QUERIES)
        self.assertNotIn("READ?", monitor.ALLOWED_QUERIES)
        self.assertNotIn("*RST", monitor.ALLOWED_QUERIES)
        self.assertNotIn("ABOR", monitor.ALLOWED_QUERIES)
        self.assertNotIn("INIT", monitor.ALLOWED_QUERIES)
        self.assertNotIn("TRAC:CLE", monitor.ALLOWED_QUERIES)
        self.assertEqual(
            monitor.ALLOWED_QUERIES,
            frozenset(command for _name, command in monitor.CONFIG_QUERIES)
            | {monitor.FETCH_QUERY},
        )
        self.assertTrue(all(command.endswith("?") for command in monitor.ALLOWED_QUERIES))

    def test_csv_schema_uses_elapsed_scalar_without_host_timestamp(self):
        self.assertEqual(
            monitor.CSV_FIELDS,
            (
                "elapsed_seconds",
                "voltage_v",
                "raw_response",
                "query_elapsed_ms",
            ),
        )
        self.assertNotIn("host_timestamp", monitor.CSV_FIELDS)

    def test_sample_csv_record_preserves_raw_response(self):
        row = monitor.sample_csv_record(1.25, 1.08673749e-7, "+1.08673749E-07", 3.5)
        self.assertEqual(tuple(row), monitor.CSV_FIELDS)
        self.assertNotIn("record_type", row)
        self.assertEqual(row["elapsed_seconds"], "1.250000")
        self.assertEqual(row["raw_response"], "+1.08673749E-07")
        self.assertTrue(math.isfinite(float(row["voltage_v"])))
        self.assertEqual(row["query_elapsed_ms"], "3.500")

    def test_csv_records_reject_invalid_scalar_values(self):
        with self.assertRaises(ValueError):
            monitor.sample_csv_record(-0.1, 1e-7, "+1E-7", 1.0)
        with self.assertRaises(ValueError):
            monitor.sample_csv_record(0.1, math.inf, "+INF", 1.0)
        with self.assertRaises(ValueError):
            monitor.sample_csv_record(0.1, 1e-7, "+1E-7", math.nan)

    def test_csv_filename_contains_one_local_start_datetime(self):
        filename = f"voltage-{monitor.timestamp_name()}.csv"
        self.assertIsNotNone(re.fullmatch(r"voltage-\d{8}-\d{6}-\d{3}\.csv", filename))

    def test_visible_plot_data_keeps_latest_ten_minutes(self):
        samples, intervals = monitor.visible_plot_data(
            [(50.0, 1e-7), (650.0, 2e-7), (700.0, 3e-7)],
            [
                {"start_elapsed_seconds": 10.0, "end_elapsed_seconds": 50.0},
                {"start_elapsed_seconds": 40.0, "end_elapsed_seconds": 650.0},
                {"start_elapsed_seconds": 650.0, "end_elapsed_seconds": 701.0},
            ],
        )
        self.assertEqual(samples, [(650.0, 2e-7), (700.0, 3e-7)])
        self.assertEqual(len(intervals), 2)
        self.assertEqual(intervals[0]["start_elapsed_seconds"], 101.0)
        self.assertEqual(intervals[0]["end_elapsed_seconds"], 650.0)
        self.assertEqual(intervals[1]["start_elapsed_seconds"], 650.0)

    def test_draw_plot_renders_red_intervention_interval_after_redraw(self):
        class FakeCanvas:
            def __init__(self):
                self.lines = []
                self.rectangles = []

            def delete(self, _tag):
                self.lines.clear()
                self.rectangles.clear()

            def winfo_width(self):
                return 800

            def winfo_height(self):
                return 500

            def create_rectangle(self, *args, **kwargs):
                self.rectangles.append((args, kwargs))
                return (args, kwargs)

            def create_text(self, *args, **kwargs):
                return (args, kwargs)

            def create_line(self, *args, **kwargs):
                self.lines.append((args, kwargs))

            def create_oval(self, *args, **kwargs):
                return (args, kwargs)

        app = monitor.MonitorApp.__new__(monitor.MonitorApp)
        app.canvas = FakeCanvas()
        app.samples = [(1.0, 1e-7), (2.0, 2e-7)]
        app.interventions = [
            {
                "number": 1,
                "intervention_id": "i-1",
                "start_elapsed_seconds": 1.25,
                "end_elapsed_seconds": 1.75,
                "intervention_type": "cable_disturbance",
                "location": "rear input cable",
            }
        ]
        app.active_intervention = None

        app._draw_plot()
        app._draw_plot()

        red_lines = [line for line in app.canvas.lines if line[1].get("fill") == "#d62728"]
        self.assertEqual(len(red_lines), 2)
        self.assertTrue(all(line[0][0] == line[0][2] for line in red_lines))
        red_bands = [
            rectangle
            for rectangle in app.canvas.rectangles
            if rectangle[1].get("fill") == "#ffd9dd"
        ]
        self.assertEqual(len(red_bands), 1)

    def test_mark_intervention_start_end_are_host_only_and_use_elapsed_scalar(self):
        class StatusVar:
            def __init__(self):
                self.value = ""

            def set(self, value):
                self.value = value

        class ValueVar:
            def __init__(self, value):
                self.value = value

            def get(self):
                return self.value

        class Widget:
            def __init__(self):
                self.options = {}

            def configure(self, **kwargs):
                self.options.update(kwargs)

        class Journal:
            def __init__(self):
                self.calls = []

            def start_intervention(self, stream_id, **kwargs):
                self.calls.append(("start", stream_id, kwargs))
                return {
                    "intervention_id": "i-1",
                    "elapsed_seconds": kwargs["elapsed_seconds"],
                    "intervention_type": kwargs["intervention_type"],
                    "location": kwargs["location"],
                }

            def end_intervention(self, intervention_id, **kwargs):
                self.calls.append(("end", intervention_id, kwargs))
                return {
                    "intervention_id": intervention_id,
                    "elapsed_seconds": kwargs["elapsed_seconds"],
                }

        app = monitor.MonitorApp.__new__(monitor.MonitorApp)
        app.live_running = True
        app.intervention_ready = True
        app.stop_event = threading.Event()
        app.diagnostic_run = Journal()
        app.stream_id = "stream-1"
        app.stream_start_monotonic = 10.0
        app.interventions = []
        app.active_intervention = None
        app.intervention_type_var = ValueVar("cable_disturbance")
        app.intervention_location_var = ValueVar("rear input cable")
        app.mark_intervention_button = Widget()
        app.intervention_type_combo = Widget()
        app.intervention_location_entry = Widget()
        app.status_var = StatusVar()
        app.messagebox = mock.Mock()
        app.closing = False
        draw_calls = []
        app._draw_plot = lambda: draw_calls.append(True)

        with mock.patch.object(monitor.time, "monotonic", return_value=12.5):
            app._mark_intervention()
        with mock.patch.object(monitor.time, "monotonic", return_value=14.0):
            app._mark_intervention()

        self.assertEqual(
            app.diagnostic_run.calls,
            [
                (
                    "start",
                    "stream-1",
                    {
                        "elapsed_seconds": 2.5,
                        "intervention_type": "cable_disturbance",
                        "location": "rear input cable",
                    },
                ),
                ("end", "i-1", {"elapsed_seconds": 4.0}),
            ],
        )
        self.assertIsNone(app.active_intervention)
        self.assertEqual(len(app.interventions), 1)
        self.assertEqual(app.interventions[0]["start_elapsed_seconds"], 2.5)
        self.assertEqual(app.interventions[0]["end_elapsed_seconds"], 4.0)
        self.assertEqual(len(draw_calls), 2)
        self.assertIn("No instrument message was sent", app.status_var.value)

    def test_mark_intervention_requires_nonempty_location(self):
        app = monitor.MonitorApp.__new__(monitor.MonitorApp)
        app.live_running = True
        app.intervention_ready = True
        app.stop_event = threading.Event()
        app.diagnostic_run = mock.Mock()
        app.stream_id = "stream-1"
        app.active_intervention = None
        app.intervention_type_var = mock.Mock(get=lambda: "cable_disturbance")
        app.intervention_location_var = mock.Mock(get=lambda: "   ")
        app.messagebox = mock.Mock()

        app._mark_intervention()

        app.diagnostic_run.start_intervention.assert_not_called()
        app.messagebox.showwarning.assert_called_once()

    def test_intervention_write_failure_latches_fault_without_opening_session(self):
        class ValueVar:
            def __init__(self, value):
                self.value = value

            def get(self):
                return self.value

        class Widget:
            def configure(self, **_kwargs):
                return None

        class StatusVar:
            def set(self, _value):
                return None

        class Journal:
            def __init__(self):
                self.state = monitor.DiagnosticState.LIVE
                self.errors = []

            def start_intervention(self, *_args, **_kwargs):
                raise monitor.RecorderError("simulated intervention write failure")

            def record_error(self, **kwargs):
                self.errors.append(kwargs)

            def transition(self, target, **_kwargs):
                self.state = target

        app = monitor.MonitorApp.__new__(monitor.MonitorApp)
        app.live_running = True
        app.intervention_ready = True
        app.stop_event = threading.Event()
        app.diagnostic_run = Journal()
        app.stream_id = "stream-1"
        app.stream_start_monotonic = 10.0
        app.stream_had_error = False
        app.recording_fault_latched = False
        app.active_intervention = None
        app.intervention_type_var = ValueVar("cable_disturbance")
        app.intervention_location_var = ValueVar("rear input cable")
        app.mark_intervention_button = Widget()
        app.intervention_type_combo = Widget()
        app.intervention_location_entry = Widget()
        app.status_var = StatusVar()
        app.messagebox = mock.Mock()
        app.closing = False

        with mock.patch.object(
            monitor,
            "session_factory",
            side_effect=AssertionError("intervention must not open a session"),
        ), mock.patch.object(monitor.time, "monotonic", return_value=12.5):
            app._mark_intervention()

        self.assertTrue(app.stop_event.is_set())
        self.assertTrue(app.recording_fault_latched)
        self.assertTrue(app.stream_had_error)
        self.assertFalse(app.live_running)
        self.assertEqual(app.diagnostic_run.state, monitor.DiagnosticState.FAULT_LATCHED)
        self.assertEqual(len(app.diagnostic_run.errors), 1)

    def test_simulated_stream_writes_only_sample_without_extra_query(self):
        calls = []
        app = monitor.MonitorApp.__new__(monitor.MonitorApp)
        app.stop_event = threading.Event()
        app.events = queue.Queue()
        app.poll_interval_s = 0.0
        app.stream_start_monotonic = time.monotonic()
        app.diagnostic_target = monitor.target_for(monitor.DEFAULT_TARGET_KEY)

        class FakeSession:
            def __enter__(self):
                return self

            def query(self, command):
                calls.append(command)
                if command == "*IDN?":
                    return "KEITHLEY INSTRUMENTS INC.,MODEL 2182A,1340129,C02  /A02"
                if command == monitor.FETCH_QUERY:
                    app.stop_event.set()
                    return "+1.08673749E-07"
                raise AssertionError(f"Unexpected query: {command}")

            def __exit__(self, exc_type, exc, traceback):
                return None

        with tempfile.TemporaryDirectory() as temp_dir:
            csv_path = Path(temp_dir) / "voltage-test.csv"
            with mock.patch.object(monitor, "session_factory", return_value=FakeSession()):
                app._stream_worker(
                    monitor.OperationOwner(
                        "test-live",
                        "live",
                        "simulate",
                        target_key=app.diagnostic_target.key,
                    ),
                    "simulate",
                    csv_path,
                    target=app.diagnostic_target,
                )
            with csv_path.open("r", newline="", encoding="utf-8-sig") as handle:
                rows = list(csv.DictReader(handle))

        self.assertEqual(calls, ["*IDN?", "FETCh?"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(tuple(rows[0]), monitor.CSV_FIELDS)
        self.assertNotIn("record_type", rows[0])
        self.assertTrue(all(math.isfinite(float(row["elapsed_seconds"])) for row in rows))
        self.assertEqual(rows[0]["raw_response"], "+1.08673749E-07")

    def test_simulated_configuration_matches_real_target(self):
        report = monitor.collect_configuration("simulate")
        self.assertTrue(report["live_readiness"]["ready"])
        self.assertEqual(report["values"]["active_channel"], "1")
        self.assertEqual(report["values"]["ch1_range_v"], "0.010000")
        self.assertEqual(report["values"]["nplc"], "5.00")
        self.assertEqual(report["values"]["system_autozero"], "1")
        self.assertEqual(report["values"]["snapshot_end.active_channel"], "1")
        self.assertEqual(report["query_plan"]["executed_count"], 38)
        consistency = next(
            check
            for check in report["diagnostics"]["checks"]
            if check["check_id"] == "communication.sentinel_consistency"
        )
        self.assertEqual(consistency["status"], "PASS")
        self.assertEqual(
            report["diagnostics"]["health_axes"],
            {
                "identity_verified": "PASS",
                "transport_healthy": "PASS",
                "snapshot_complete": "PASS",
                "safe_idle": "N/A",
                "configuration_interpretable": "PASS",
                "profile_matched": "WARN",
                "calibration_condition": "PASS",
                "calibration_traceability": "UNKNOWN",
                "performance_validated": "UNKNOWN",
                "evidence_complete": "UNKNOWN",
                "live_authorized": "PASS",
            },
        )
        health_text = monitor.format_health_axes(
            report["diagnostics"]["health_axes"]
        )
        self.assertTrue(health_text.startswith("Health axes: identity verified PASS"))
        self.assertIn("performance validated UNKNOWN", health_text)
        self.assertIn("live authorized PASS", health_text)

    def test_configuration_session_close_failure_blocks_readiness(self):
        class CloseFailureSession:
            def __enter__(self):
                return self

            def query(self, command):
                return monitor.SIMULATED_VALUES[command]

            def __exit__(self, exc_type, exc, traceback):
                raise OSError("close failed")

        with mock.patch.object(
            monitor,
            "session_factory",
            return_value=CloseFailureSession(),
        ):
            report = monitor.collect_configuration("simulate")

        lifecycle_errors = [
            item
            for item in report["transcript"]
            if item["name"] == "session_lifecycle" and not item["ok"]
        ]
        self.assertEqual(len(lifecycle_errors), 1)
        self.assertIn("OSError: close failed", lifecycle_errors[0]["error"])
        self.assertEqual(report["query_plan"]["executed_count"], 38)
        self.assertFalse(report["diagnostics"]["diagnostics_acceptable"])
        self.assertEqual(report["diagnostics"]["overall"], "BLOCKED")
        self.assertFalse(report["live_readiness"]["ready"])

    def test_snapshot_end_condition_is_checked_without_requiring_word_equality(self):
        class EndCalibratingSession:
            def __init__(self):
                self.counts = {}

            def __enter__(self):
                return self

            def query(self, command):
                occurrence = self.counts.get(command, 0) + 1
                self.counts[command] = occurrence
                if command == "STAT:OPER:COND?" and occurrence == 2:
                    return "17"
                return monitor.SIMULATED_VALUES[command]

            def __exit__(self, exc_type, exc, traceback):
                return None

        with mock.patch.object(
            monitor,
            "session_factory",
            return_value=EndCalibratingSession(),
        ):
            report = monitor.collect_configuration("simulate")

        consistency = next(
            check
            for check in report["diagnostics"]["checks"]
            if check["check_id"] == "communication.sentinel_consistency"
        )
        end_calibrating = next(
            check
            for check in report["diagnostics"]["checks"]
            if check["check_id"] == "acquisition.snapshot_end.calibrating"
        )
        self.assertEqual(consistency["status"], "PASS")
        self.assertEqual(end_calibrating["status"], "BLOCKED")
        self.assertFalse(report["live_readiness"]["ready"])

    def test_live_start_requires_ready_state_and_clear_fault_latches(self):
        configuration = {
            "diagnostics": {"can_start_live": True},
            "capabilities": {
                "live_supported": True,
                "live_authorized": True,
            },
        }
        self.assertTrue(
            monitor.live_start_is_safe(
                configuration,
                monitor.DiagnosticState.OBSERVE_READY,
                recording_fault_latched=False,
                stream_had_error=False,
            )
        )
        self.assertFalse(
            monitor.live_start_is_safe(
                configuration,
                monitor.DiagnosticState.OBSERVE_READY,
                recording_fault_latched=True,
                stream_had_error=False,
            )
        )
        self.assertFalse(
            monitor.live_start_is_safe(
                {"diagnostics": {"can_start_live": True}},
                monitor.DiagnosticState.OBSERVE_READY,
                recording_fault_latched=False,
                stream_had_error=False,
            )
        )
        self.assertFalse(
            monitor.live_start_is_safe(
                configuration,
                monitor.DiagnosticState.OBSERVE_READY,
                recording_fault_latched=False,
                stream_had_error=True,
            )
        )
        self.assertFalse(
            monitor.live_start_is_safe(
                configuration,
                monitor.DiagnosticState.FAULT_LATCHED,
                recording_fault_latched=False,
                stream_had_error=False,
            )
        )

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
        self.assertFalse(
            monitor.identity_is_expected(
                "KEITHLEY INSTRUMENTS INC.,MODEL 2182A,1340129,C08/B01"
            )
        )
        self.assertFalse(
            monitor.identity_is_expected(
                "KEITHLEY INSTRUMENTS INC.,MODEL 2182A,X1340129X,C02 /A02"
            )
        )

    def test_wrong_identity_stops_configuration_after_idn(self):
        context = monitor.SimulationContext("wrong_identity", monitor.ALLOWED_QUERIES)
        report = monitor.collect_configuration("simulate", context)

        self.assertFalse(report["diagnostics"]["can_start_live"])
        self.assertEqual(
            context.query_history,
            [{"phase": "config", "command": "*IDN?", "occurrence": 1}],
        )
        self.assertEqual(len(report["transcript"]), 1)
        self.assertFalse(report["transcript"][0]["ok"])

    def test_real_mode_rejects_fault_context_before_pyvisa_import(self):
        for scenario in ("nominal", "wrong_identity"):
            with self.subTest(scenario=scenario):
                context = monitor.SimulationContext(scenario, monitor.ALLOWED_QUERIES)
                with self.assertRaisesRegex(ValueError, "forbidden in real mode"):
                    monitor.session_factory("real", "config", context)
                with self.assertRaisesRegex(ValueError, "forbidden in real mode"):
                    monitor.collect_configuration("real", context)

    def test_real_mode_requires_explicit_inventory_target(self):
        with self.assertRaisesRegex(ValueError, "explicit inventory target"):
            monitor.RealSession()
        with self.assertRaisesRegex(ValueError, "explicit inventory target"):
            monitor.session_factory("real", "config")
        with self.assertRaisesRegex(ValueError, "explicit inventory target"):
            monitor.collect_configuration("real")
        with self.assertRaisesRegex(ValueError, "forbids static target_key"):
            monitor.session_factory(
                "real",
                "config",
                target_key=monitor.DEFAULT_TARGET_KEY,
            )
        with self.assertRaisesRegex(ValueError, "forbids static target_key"):
            monitor.collect_configuration(
                "real",
                target_key=monitor.DEFAULT_TARGET_KEY,
            )

    def test_live_workers_require_explicit_frozen_target_before_session(self):
        app = monitor.MonitorApp.__new__(monitor.MonitorApp)
        app.stop_event = threading.Event()
        app.events = queue.Queue()
        app.poll_interval_s = 0.0
        app.stream_start_monotonic = time.monotonic()

        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            monitor,
            "session_factory",
            side_effect=AssertionError("session must not be opened without target"),
        ):
            csv_path = Path(temp_dir) / "must-not-open.csv"
            stream_owner = monitor.OperationOwner(
                "test-live",
                "live",
                "simulate",
            )
            app._stream_worker(stream_owner, "simulate", csv_path, target=None)
            self.assertFalse(csv_path.exists())
            stream_events = []
            while not app.events.empty():
                stream_events.append(app.events.get_nowait())
            stream_error = next(
                payload
                for owner, event, payload in stream_events
                if owner is stream_owner and event == "stream_error"
            )
            self.assertIn("explicit frozen target", stream_error["message"])

            single_owner = monitor.OperationOwner(
                "test-single",
                "single",
                "simulate",
            )
            app._single_fetch_worker(single_owner, "simulate", target=None)
            queued_owner, event, message = app.events.get_nowait()
            self.assertIs(queued_owner, single_owner)
            self.assertEqual(event, "single_error")
            self.assertIn("explicit frozen target", message)

    def test_single_fetch_event_failure_latches_recorder_before_query(self):
        class Journal:
            def __init__(self):
                self.state = monitor.DiagnosticState.OBSERVE_READY

            def record_event(self, *_args, **_kwargs):
                raise monitor.RecorderError("simulated recorder failure")

            def transition(self, target, **_kwargs):
                self.state = target

        class Widget:
            def __init__(self):
                self.state = None

            def configure(self, **kwargs):
                self.state = kwargs.get("state")

        class Var:
            def __init__(self):
                self.value = ""

            def set(self, value):
                self.value = value

        app = monitor.MonitorApp.__new__(monitor.MonitorApp)
        app.worker = None
        app.configuration = {
            "capabilities": {"live_supported": True},
            "diagnostics": {"can_start_live": True},
        }
        app.diagnostic_run = Journal()
        app.diagnostic_target = monitor.target_for(monitor.DEFAULT_TARGET_KEY)
        app.recording_fault_latched = False
        app.start_button = Widget()
        app.single_button = Widget()
        app.diagnostic_state_var = Var()
        app.messagebox = mock.Mock()
        app._confirm_real_access = lambda: True

        app._single_fetch()

        self.assertTrue(app.recording_fault_latched)
        self.assertEqual(app.diagnostic_run.state, monitor.DiagnosticState.FAULT_LATCHED)
        self.assertEqual(app.start_button.state, "disabled")
        self.assertEqual(app.single_button.state, "disabled")

    def test_pause_records_lifecycle_event_before_stop(self):
        class Journal:
            state = monitor.DiagnosticState.LIVE

            def __init__(self):
                self.events = []

            def end_intervention(self, intervention_id, **kwargs):
                self.events.append(("intervention_end", {"intervention_id": intervention_id, **kwargs}))
                return {
                    "intervention_id": intervention_id,
                    "elapsed_seconds": kwargs["elapsed_seconds"],
                }

            def record_event(self, event_type, **kwargs):
                self.events.append((event_type, kwargs))

        class Widget:
            def configure(self, **_kwargs):
                return None

        class Var:
            def set(self, _value):
                return None

        app = monitor.MonitorApp.__new__(monitor.MonitorApp)
        app.live_running = True
        app.intervention_ready = True
        app.active_intervention = {
            "number": 1,
            "intervention_id": "i-1",
            "start_elapsed_seconds": 1.0,
            "intervention_type": "connector_disturbance",
            "location": "rear input connector",
        }
        app.interventions = []
        app.stream_start_monotonic = 10.0
        app.diagnostic_run = Journal()
        app.stream_id = "stream-1"
        app.recording_fault_latched = False
        app.stream_had_error = False
        app.stop_event = threading.Event()
        app.pause_button = Widget()
        app.mark_intervention_button = Widget()
        app.intervention_type_combo = Widget()
        app.intervention_location_entry = Widget()
        app.status_var = Var()
        app.messagebox = mock.Mock()
        app.closing = False
        app._draw_plot = lambda: None

        with mock.patch.object(monitor.time, "monotonic", return_value=12.5):
            app._pause_stream()

        self.assertTrue(app.stop_event.is_set())
        self.assertEqual(
            [event[0] for event in app.diagnostic_run.events],
            ["intervention_end", "pause_requested"],
        )
        self.assertIsNone(app.active_intervention)
        self.assertEqual(len(app.interventions), 1)
        self.assertEqual(
            app.diagnostic_run.events[1][1]["reason_code"],
            "PAUSE_REQUESTED",
        )


if __name__ == "__main__":
    unittest.main()
