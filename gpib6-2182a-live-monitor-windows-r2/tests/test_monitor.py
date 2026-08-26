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
    def test_parse_voltage(self):
        self.assertAlmostEqual(monitor.parse_voltage("+1.08673749E-07"), 1.08673749e-7)
        self.assertAlmostEqual(monitor.parse_voltage("-2.5E-6,extra"), -2.5e-6)

    def test_non_numeric_voltage_rejected(self):
        with self.assertRaises(ValueError):
            monitor.parse_voltage("not-a-number")

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
                "record_type",
                "elapsed_seconds",
                "voltage_v",
                "raw_response",
                "query_elapsed_ms",
            ),
        )
        self.assertNotIn("host_timestamp", monitor.CSV_FIELDS)

    def test_sample_csv_record_preserves_raw_response(self):
        row = monitor.sample_csv_record(1.25, 1.08673749e-7, "+1.08673749E-07", 3.5)
        self.assertEqual(row["record_type"], "sample")
        self.assertEqual(row["elapsed_seconds"], "1.250000")
        self.assertEqual(row["raw_response"], "+1.08673749E-07")
        self.assertTrue(math.isfinite(float(row["voltage_v"])))
        self.assertEqual(row["query_elapsed_ms"], "3.500")

    def test_touch_csv_record_has_no_invented_voltage(self):
        row = monitor.touch_csv_record(8.417392)
        self.assertEqual(row["record_type"], "touch")
        self.assertEqual(row["elapsed_seconds"], "8.417392")
        self.assertEqual(row["voltage_v"], "")
        self.assertEqual(row["raw_response"], "")
        self.assertEqual(row["query_elapsed_ms"], "")

    def test_csv_records_reject_invalid_scalar_values(self):
        with self.assertRaises(ValueError):
            monitor.sample_csv_record(-0.1, 1e-7, "+1E-7", 1.0)
        with self.assertRaises(ValueError):
            monitor.sample_csv_record(0.1, math.inf, "+INF", 1.0)
        with self.assertRaises(ValueError):
            monitor.touch_csv_record(math.nan)

    def test_csv_filename_contains_one_local_start_datetime(self):
        filename = f"voltage-{monitor.timestamp_name()}.csv"
        self.assertIsNotNone(re.fullmatch(r"voltage-\d{8}-\d{6}-\d{3}\.csv", filename))

    def test_visible_plot_data_keeps_latest_ten_minutes(self):
        samples, markers = monitor.visible_plot_data(
            [(50.0, 1e-7), (650.0, 2e-7), (700.0, 3e-7)],
            [40.0, 650.0, 701.0],
        )
        self.assertEqual(samples, [(650.0, 2e-7), (700.0, 3e-7)])
        self.assertEqual(markers, [(2, 650.0), (3, 701.0)])

    def test_draw_plot_renders_vertical_red_touch_line_after_redraw(self):
        class FakeCanvas:
            def __init__(self):
                self.lines = []

            def delete(self, _tag):
                self.lines.clear()

            def winfo_width(self):
                return 800

            def winfo_height(self):
                return 500

            def create_rectangle(self, *args, **kwargs):
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
        app.touch_markers = [1.5]

        app._draw_plot()
        app._draw_plot()

        red_lines = [line for line in app.canvas.lines if line[1].get("fill") == "#d62728"]
        self.assertEqual(len(red_lines), 1)
        coordinates, _style = red_lines[0]
        self.assertEqual(coordinates[0], coordinates[2])
        self.assertNotEqual(coordinates[1], coordinates[3])

    def test_mark_touch_is_host_only_and_uses_elapsed_scalar(self):
        class StatusVar:
            def __init__(self):
                self.value = ""

            def set(self, value):
                self.value = value

        app = monitor.MonitorApp.__new__(monitor.MonitorApp)
        app.live_running = True
        app.stream_start_monotonic = 10.0
        app.touch_markers = []
        app.touch_events = queue.Queue()
        app.status_var = StatusVar()
        draw_calls = []
        app._draw_plot = lambda: draw_calls.append(True)

        with mock.patch.object(monitor.time, "monotonic", return_value=12.5):
            app._mark_touch()

        self.assertEqual(app.touch_markers, [2.5])
        self.assertEqual(app.touch_events.get_nowait(), 2.5)
        self.assertEqual(draw_calls, [True])
        self.assertIn("sends no instrument message", app.status_var.value)

    def test_simulated_stream_writes_touch_and_sample_without_extra_query(self):
        calls = []
        app = monitor.MonitorApp.__new__(monitor.MonitorApp)
        app.stop_event = threading.Event()
        app.touch_events = queue.Queue()
        app.touch_events.put(0.0)
        app.events = queue.Queue()
        app.poll_interval_s = 0.0
        app.stream_start_monotonic = time.monotonic()

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
                app._stream_worker("simulate", csv_path)
            with csv_path.open("r", newline="", encoding="utf-8-sig") as handle:
                rows = list(csv.DictReader(handle))

        self.assertEqual(calls, ["*IDN?", "FETCh?"])
        self.assertEqual([row["record_type"] for row in rows], ["touch", "sample"])
        self.assertTrue(all(math.isfinite(float(row["elapsed_seconds"])) for row in rows))
        self.assertEqual(rows[0]["voltage_v"], "")
        self.assertEqual(rows[1]["raw_response"], "+1.08673749E-07")

    def test_simulated_configuration_matches_real_target(self):
        report = monitor.collect_configuration("simulate")
        self.assertTrue(report["live_readiness"]["ready"])
        self.assertEqual(report["values"]["active_channel"], "1")
        self.assertEqual(report["values"]["ch1_range_v"], "0.010000")
        self.assertEqual(report["values"]["nplc"], "5.00")

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


if __name__ == "__main__":
    unittest.main()
