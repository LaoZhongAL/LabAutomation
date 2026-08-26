"""Query-only live monitor for the Keithley 2182A at GPIB0::6::INSTR.

The instrument protocol is intentionally small and auditable:

* configuration: exact allow-listed SCPI queries only;
* live data: FETCh? only;
* no generic write API and no configuration controls.

The graph uses Tkinter Canvas, so the only external runtime dependency is
PyVISA (plus the NI-VISA installation already present on the laboratory PC).
"""

from __future__ import annotations

import csv
import json
import math
import queue
import random
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path


RESOURCE = "GPIB0::6::INSTR"
EXPECTED_MODEL = "2182A"
EXPECTED_SERIAL = "1340129"

CONFIG_QUERIES: tuple[tuple[str, str], ...] = (
    ("identity", "*IDN?"),
    ("scpi_version", "SYST:VERS?"),
    ("line_frequency_hz", "SYST:LFREQUENCY?"),
    ("power_on_setup", "SYST:POSETUP?"),
    ("sense_function", "SENS:FUNC?"),
    ("active_channel", "SENS:CHAN?"),
    ("nplc", "SENS:VOLT:DC:NPLC?"),
    ("ch1_range_v", "SENS:VOLT:DC:CHAN1:RANG?"),
    ("ch1_autorange", "SENS:VOLT:DC:CHAN1:RANG:AUTO?"),
    ("ch2_range_v", "SENS:VOLT:DC:CHAN2:RANG?"),
    ("ch2_autorange", "SENS:VOLT:DC:CHAN2:RANG:AUTO?"),
    ("ch1_digital_filter", "SENS:VOLT:DC:CHAN1:DFILTER?"),
    ("ch1_analog_filter", "SENS:VOLT:DC:CHAN1:LPASS?"),
    ("ch2_digital_filter", "SENS:VOLT:DC:CHAN2:DFILTER?"),
    ("ch2_analog_filter", "SENS:VOLT:DC:CHAN2:LPASS?"),
    ("trigger_count", "TRIG:COUNT?"),
    ("trigger_delay_s", "TRIG:DELAY?"),
    ("trigger_source", "TRIG:SOURCE?"),
    ("sample_count", "SAMP:COUN?"),
    ("continuous_initiation", "INIT:CONT?"),
    ("data_format", "FORM:DATA?"),
    ("format_elements", "FORM:ELEM?"),
)

FETCH_QUERY = "FETCh?"
ALLOWED_QUERIES = frozenset(command for _, command in CONFIG_QUERIES) | {FETCH_QUERY}

CSV_FIELDS = (
    "record_type",
    "elapsed_seconds",
    "voltage_v",
    "raw_response",
    "query_elapsed_ms",
)
SAMPLE_RECORD = "sample"
TOUCH_RECORD = "touch"
PLOT_WINDOW_SECONDS = 600.0

SIMULATED_VALUES = {
    "*IDN?": "KEITHLEY INSTRUMENTS INC.,MODEL 2182A,1340129,C02  /A02",
    "SYST:VERS?": "1991.0",
    "SYST:LFREQUENCY?": "50",
    "SYST:POSETUP?": "SAV0",
    "SENS:FUNC?": '"VOLT:DC"',
    "SENS:CHAN?": "1",
    "SENS:VOLT:DC:NPLC?": "5.00",
    "SENS:VOLT:DC:CHAN1:RANG?": "0.010000",
    "SENS:VOLT:DC:CHAN1:RANG:AUTO?": "0",
    "SENS:VOLT:DC:CHAN2:RANG?": "10.000000",
    "SENS:VOLT:DC:CHAN2:RANG:AUTO?": "1",
    "SENS:VOLT:DC:CHAN1:DFILTER?": "0",
    "SENS:VOLT:DC:CHAN1:LPASS?": "0",
    "SENS:VOLT:DC:CHAN2:DFILTER?": "1",
    "SENS:VOLT:DC:CHAN2:LPASS?": "0",
    "TRIG:COUNT?": "+9.9e37",
    "TRIG:DELAY?": "0.000",
    "TRIG:SOURCE?": "IMM",
    "SAMP:COUN?": "1",
    "INIT:CONT?": "1",
    "FORM:DATA?": "ASC",
    "FORM:ELEM?": "READ",
}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def timestamp_name() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]


def parse_voltage(raw: str) -> float:
    """Parse the first ASCII numeric field returned by FETCh?."""
    text = raw.strip()
    if not text:
        raise ValueError("empty response")
    value = float(text.split(",", 1)[0].strip())
    if not math.isfinite(value):
        raise ValueError(f"non-finite voltage: {text!r}")
    return value


def derive_poll_interval(values: dict[str, str]) -> float:
    """Choose a conservative host polling interval from NPLC and line frequency."""
    try:
        nplc = float(values["nplc"])
        line_hz = float(values["line_frequency_hz"])
        measurement_seconds = nplc / line_hz
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return 0.5
    return min(5.0, max(0.25, measurement_seconds * 1.5))


def format_voltage(voltage_v: float) -> str:
    magnitude = abs(voltage_v)
    if magnitude < 1e-6:
        return f"{voltage_v * 1e9:+.6f} nV"
    if magnitude < 1e-3:
        return f"{voltage_v * 1e6:+.6f} µV"
    if magnitude < 1:
        return f"{voltage_v * 1e3:+.6f} mV"
    return f"{voltage_v:+.9g} V"


def identity_is_expected(identity: str) -> bool:
    upper = identity.upper()
    return "MODEL 2182A" in upper and EXPECTED_SERIAL in identity


def sample_csv_record(
    elapsed_seconds: float,
    voltage_v: float,
    raw_response: str,
    query_elapsed_ms: float,
) -> dict[str, str]:
    """Build one voltage record using elapsed scalar time only."""
    if not math.isfinite(elapsed_seconds) or elapsed_seconds < 0:
        raise ValueError(f"invalid elapsed time: {elapsed_seconds!r}")
    if not math.isfinite(voltage_v):
        raise ValueError(f"non-finite voltage: {voltage_v!r}")
    if not math.isfinite(query_elapsed_ms) or query_elapsed_ms < 0:
        raise ValueError(f"invalid query duration: {query_elapsed_ms!r}")
    return {
        "record_type": SAMPLE_RECORD,
        "elapsed_seconds": f"{elapsed_seconds:.6f}",
        "voltage_v": f"{voltage_v:.12g}",
        "raw_response": raw_response,
        "query_elapsed_ms": f"{query_elapsed_ms:.3f}",
    }


def touch_csv_record(elapsed_seconds: float) -> dict[str, str]:
    """Build one host-side touch event without inventing a voltage sample."""
    if not math.isfinite(elapsed_seconds) or elapsed_seconds < 0:
        raise ValueError(f"invalid elapsed time: {elapsed_seconds!r}")
    return {
        "record_type": TOUCH_RECORD,
        "elapsed_seconds": f"{elapsed_seconds:.6f}",
        "voltage_v": "",
        "raw_response": "",
        "query_elapsed_ms": "",
    }


def visible_plot_data(
    samples,
    touch_markers,
    window_seconds: float = PLOT_WINDOW_SECONDS,
) -> tuple[list[tuple[float, float]], list[tuple[int, float]]]:
    """Return samples and numbered touch markers inside the latest plot window."""
    sample_values = list(samples)
    numbered_markers = list(enumerate(touch_markers, start=1))
    times = [elapsed for elapsed, _voltage in sample_values]
    times.extend(elapsed for _number, elapsed in numbered_markers)
    if not times:
        return [], []
    cutoff = max(0.0, max(times) - window_seconds)
    return (
        [point for point in sample_values if point[0] >= cutoff],
        [marker for marker in numbered_markers if marker[1] >= cutoff],
    )


class RealSession:
    """Minimal PyVISA session exposing exact query messages only."""

    def __init__(self, timeout_ms: int = 3000) -> None:
        try:
            import pyvisa
        except ImportError as exc:
            raise RuntimeError(
                "PyVISA is missing from C:\\LabAutomation\\.venv. "
                "Install pyvisa in that shared environment."
            ) from exc
        self.pyvisa = pyvisa
        self.timeout_ms = timeout_ms
        self.manager = None
        self.instrument = None

    def __enter__(self):
        self.manager = self.pyvisa.ResourceManager()
        self.instrument = self.manager.open_resource(RESOURCE)
        self.instrument.timeout = self.timeout_ms
        self.instrument.write_termination = "\n"
        self.instrument.read_termination = "\n"
        return self

    def query(self, command: str) -> str:
        if command not in ALLOWED_QUERIES:
            raise ValueError(f"Blocked non-allow-listed message: {command!r}")
        return str(self.instrument.query(command)).strip()

    def __exit__(self, exc_type, exc, traceback) -> None:
        try:
            if self.instrument is not None:
                self.instrument.close()
        finally:
            if self.manager is not None:
                self.manager.close()


class SimulatedSession:
    def __init__(self) -> None:
        self.started = time.monotonic()
        self.random = random.Random(2182)

    def __enter__(self):
        return self

    def query(self, command: str) -> str:
        if command not in ALLOWED_QUERIES:
            raise ValueError(f"Blocked non-allow-listed message: {command!r}")
        if command == FETCH_QUERY:
            elapsed = time.monotonic() - self.started
            voltage = 1.2e-7 + 4e-8 * math.sin(elapsed / 3.0) + self.random.gauss(0.0, 5e-9)
            return f"{voltage:+.8E}"
        return SIMULATED_VALUES[command]

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None


def session_factory(mode: str):
    if mode == "real":
        return RealSession()
    if mode == "simulate":
        return SimulatedSession()
    raise ValueError(f"Unsupported mode: {mode}")


def collect_configuration(mode: str) -> dict[str, object]:
    transcript: list[dict[str, object]] = []
    values: dict[str, str] = {}
    with session_factory(mode) as session:
        for name, command in CONFIG_QUERIES:
            started = time.perf_counter()
            try:
                response = session.query(command)
                elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
                transcript.append(
                    {
                        "name": name,
                        "command": command,
                        "ok": True,
                        "response": response,
                        "elapsed_ms": elapsed_ms,
                    }
                )
                values[name] = response
                if name == "identity" and not identity_is_expected(response):
                    raise RuntimeError(
                        f"Identity mismatch. Expected MODEL 2182A, serial {EXPECTED_SERIAL}; got {response!r}"
                    )
            except Exception as exc:
                elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
                transcript.append(
                    {
                        "name": name,
                        "command": command,
                        "ok": False,
                        "error": f"{type(exc).__name__}: {exc}",
                        "elapsed_ms": elapsed_ms,
                    }
                )
                if name == "identity":
                    raise

    format_ok = values.get("data_format", "").upper().startswith("ASC")
    elements_ok = values.get("format_elements", "").upper().strip() == "READ"
    function_ok = "VOLT:DC" in values.get("sense_function", "").upper()
    return {
        "created_at": now_iso(),
        "operation": "query-only configuration snapshot",
        "mode": mode,
        "resource": RESOURCE,
        "expected_model": EXPECTED_MODEL,
        "expected_serial": EXPECTED_SERIAL,
        "safety": {
            "query_only": True,
            "exact_allowlist": True,
            "generic_write_api_exposed": False,
            "configuration_controls_exposed": False,
            "live_query": FETCH_QUERY,
        },
        "values": values,
        "transcript": transcript,
        "live_readiness": {
            "identity_matches": identity_is_expected(values.get("identity", "")),
            "dc_voltage_function": function_ok,
            "ascii_data": format_ok,
            "read_only_element": elements_ok,
            "ready": function_ok and format_ok and elements_ok,
        },
        "derived_poll_interval_s": derive_poll_interval(values),
    }


class MonitorApp:
    def __init__(self, root) -> None:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk

        self.tk = tk
        self.ttk = ttk
        self.filedialog = filedialog
        self.messagebox = messagebox
        self.root = root
        self.root.title("Keithley 2182A GPIB6 Query-Only Live Monitor")
        self.root.geometry("1380x880")
        self.root.minsize(1080, 720)

        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.touch_events: queue.Queue[float] = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.output_root = Path.cwd() / "monitor_runs"
        self.run_directory: Path | None = None
        self.configuration: dict[str, object] | None = None
        self.samples: deque[tuple[float, float]] = deque(maxlen=20000)
        self.touch_markers: list[float] = []
        self.stream_start_monotonic = 0.0
        self.poll_interval_s = 0.5
        self.real_access_confirmed = False
        self.live_running = False

        self._configure_style()
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.root.after(100, self._drain_events)

    def _configure_style(self) -> None:
        style = self.ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Title.TLabel", font=("Segoe UI", 18, "bold"))
        style.configure("Safe.TLabel", foreground="#176b3a", font=("Segoe UI", 10, "bold"))
        style.configure("Warn.TLabel", foreground="#8a5a00", font=("Segoe UI", 9, "bold"))
        style.configure("Reading.TLabel", font=("Segoe UI", 24, "bold"), foreground="#123e73")
        style.configure("Action.TButton", font=("Segoe UI", 10, "bold"), padding=(12, 8))
        style.configure("Treeview", rowheight=25, font=("Segoe UI", 9))

    def _build_ui(self) -> None:
        tk, ttk = self.tk, self.ttk
        outer = ttk.Frame(self.root, padding=14)
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text="Keithley 2182A · GPIB6 Live Voltage Monitor", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            outer,
            text="Fixed target: GPIB0::6::INSTR · MODEL 2182A · expected S/N 1340129",
        ).pack(anchor="w", pady=(2, 0))
        ttk.Label(
            outer,
            text="QUERY ONLY · CONFIGURATION SNAPSHOT + FETCh? · NO RESET, ABORT, INIT, TRIGGER, OR CONFIGURATION WRITES",
            style="Safe.TLabel",
        ).pack(anchor="w", pady=(6, 0))
        ttk.Label(
            outer,
            text="Close LabVIEW and NI MAX test panels before Real mode. Do not run two controllers on GPIB6.",
            style="Warn.TLabel",
        ).pack(anchor="w", pady=(2, 8))

        controls = ttk.Frame(outer)
        controls.pack(fill="x")
        ttk.Label(controls, text="Mode:").pack(side="left")
        self.mode_var = tk.StringVar(value="simulate")
        self.mode_combo = ttk.Combobox(
            controls,
            state="readonly",
            width=12,
            values=("simulate", "real"),
            textvariable=self.mode_var,
        )
        self.mode_combo.pack(side="left", padx=(5, 12))
        self.mode_combo.bind("<<ComboboxSelected>>", self._mode_changed)

        self.config_button = ttk.Button(
            controls,
            text="1. Read Configuration",
            style="Action.TButton",
            command=self._read_configuration,
        )
        self.config_button.pack(side="left")
        self.start_button = ttk.Button(
            controls,
            text="2. Start Live Plot",
            style="Action.TButton",
            state="disabled",
            command=self._start_stream,
        )
        self.start_button.pack(side="left", padx=(7, 0))
        self.pause_button = ttk.Button(controls, text="Pause", state="disabled", command=self._pause_stream)
        self.pause_button.pack(side="left", padx=(7, 0))
        self.mark_touch_button = ttk.Button(
            controls,
            text="Mark Touch",
            state="disabled",
            command=self._mark_touch,
        )
        self.mark_touch_button.pack(side="left", padx=(7, 0))
        self.single_button = ttk.Button(
            controls, text="Single FETCh?", state="disabled", command=self._single_fetch
        )
        self.single_button.pack(side="left", padx=(7, 0))
        ttk.Button(controls, text="Clear Plot", command=self._clear_plot).pack(side="left", padx=(7, 0))
        ttk.Button(controls, text="Output Folder", command=self._choose_output).pack(side="left", padx=(7, 0))

        self.mode_status_var = tk.StringVar(value="Simulation selected; no VISA communication.")
        ttk.Label(outer, textvariable=self.mode_status_var).pack(anchor="w", pady=(6, 6))

        body = ttk.Panedwindow(outer, orient="horizontal")
        body.pack(fill="both", expand=True)
        left = ttk.Frame(body, padding=(0, 0, 8, 0))
        right = ttk.Frame(body)
        body.add(left, weight=2)
        body.add(right, weight=3)

        config_box = ttk.LabelFrame(left, text="Current instrument configuration (read only)", padding=8)
        config_box.pack(fill="both", expand=True)
        self.config_tree = ttk.Treeview(
            config_box,
            columns=("parameter", "value", "query"),
            show="headings",
            selectmode="browse",
        )
        self.config_tree.heading("parameter", text="Parameter")
        self.config_tree.heading("value", text="Instrument response")
        self.config_tree.heading("query", text="Exact query")
        self.config_tree.column("parameter", width=150, stretch=False)
        self.config_tree.column("value", width=225, stretch=True)
        self.config_tree.column("query", width=250, stretch=True)
        self.config_tree.pack(side="left", fill="both", expand=True)
        config_scroll = ttk.Scrollbar(config_box, orient="vertical", command=self.config_tree.yview)
        config_scroll.pack(side="right", fill="y")
        self.config_tree.configure(yscrollcommand=config_scroll.set)

        reading_box = ttk.LabelFrame(right, text="Latest voltage", padding=10)
        reading_box.pack(fill="x")
        self.reading_var = tk.StringVar(value="No sample")
        ttk.Label(reading_box, textvariable=self.reading_var, style="Reading.TLabel").pack(side="left")
        self.raw_var = tk.StringVar(value="Raw: —")
        ttk.Label(reading_box, textvariable=self.raw_var).pack(side="right")

        plot_box = ttk.LabelFrame(right, text="Voltage versus elapsed host time · latest 10 minutes", padding=6)
        plot_box.pack(fill="both", expand=True, pady=(8, 0))
        self.canvas = tk.Canvas(plot_box, background="#ffffff", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda _event: self._draw_plot())

        footer = ttk.Frame(outer)
        footer.pack(fill="x", pady=(8, 0))
        self.status_var = tk.StringVar(value="Ready. Read Configuration before starting the plot.")
        ttk.Label(footer, textvariable=self.status_var).pack(anchor="w")
        self.evidence_var = tk.StringVar(value=f"Output root: {self.output_root}")
        ttk.Label(footer, textvariable=self.evidence_var).pack(anchor="w", pady=(2, 0))

    def _mode_changed(self, _event=None) -> None:
        if self.worker and self.worker.is_alive():
            self.messagebox.showwarning("Monitor running", "Pause the live plot before changing mode.")
            return
        self.configuration = None
        self.run_directory = None
        self.real_access_confirmed = False
        self.live_running = False
        self.start_button.configure(state="disabled")
        self.single_button.configure(state="disabled")
        self.mark_touch_button.configure(state="disabled")
        self.mode_status_var.set(
            "Real VISA selected; configuration has not been read."
            if self.mode_var.get() == "real"
            else "Simulation selected; no VISA communication."
        )

    def _confirm_real_access(self) -> bool:
        if self.mode_var.get() != "real" or self.real_access_confirmed:
            return True
        confirmed = self.messagebox.askyesno(
            "Confirm exclusive GPIB access",
            "Confirm all of the following:\n\n"
            "• LabVIEW is stopped and has released GPIB0::6::INSTR.\n"
            "• NI MAX test panels for GPIB6 are closed.\n"
            "• You want exact query-only VISA communication now.\n\n"
            "The program never sends reset, abort, init, trigger, or configuration commands.",
        )
        self.real_access_confirmed = confirmed
        return confirmed

    def _choose_output(self) -> None:
        selected = self.filedialog.askdirectory(initialdir=str(self.output_root.parent))
        if selected:
            self.output_root = Path(selected)
            self.evidence_var.set(f"Output root: {self.output_root}")

    def _read_configuration(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        if not self._confirm_real_access():
            return
        self._set_busy(True)
        self.status_var.set("Reading the exact allow-listed configuration queries...")
        mode = self.mode_var.get()
        self.worker = threading.Thread(target=self._configuration_worker, args=(mode,), daemon=True)
        self.worker.start()

    def _configuration_worker(self, mode: str) -> None:
        try:
            report = collect_configuration(mode)
            run_directory = self.output_root / f"{timestamp_name()}-{mode}-gpib6-monitor"
            run_directory.mkdir(parents=True, exist_ok=False)
            evidence_file = run_directory / "configuration-snapshot.json"
            evidence_file.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
            self.events.put(("configuration", (report, run_directory, evidence_file)))
        except Exception as exc:
            self.events.put(("error", f"{type(exc).__name__}: {exc}"))

    def _start_stream(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        if not self.configuration or not self.run_directory:
            self.messagebox.showwarning("Configuration required", "Read Configuration first.")
            return
        readiness = self.configuration.get("live_readiness", {})
        if not isinstance(readiness, dict) or not readiness.get("ready"):
            self.messagebox.showerror(
                "Unsupported current format",
                "Live plotting requires VOLT:DC, FORM:DATA ASC, and FORM:ELEM READ. "
                "This program will not change the instrument to force those settings.",
            )
            return
        if not self._confirm_real_access():
            return
        self.stop_event.clear()
        self.samples.clear()
        self.touch_markers.clear()
        self._drain_touch_events()
        self.reading_var.set("No sample")
        self.raw_var.set("Raw: —")
        self.stream_start_monotonic = time.monotonic()
        mode = self.mode_var.get()
        csv_path = self.run_directory / f"voltage-{timestamp_name()}.csv"
        self.live_running = True
        self.start_button.configure(state="disabled")
        self.config_button.configure(state="disabled")
        self.mode_combo.configure(state="disabled")
        self.single_button.configure(state="disabled")
        self.pause_button.configure(state="normal")
        self.mark_touch_button.configure(state="disabled")
        self._draw_plot()
        self.status_var.set(f"Live FETCh? polling started every {self.poll_interval_s:.3f} s.")
        self.evidence_var.set(f"CSV: {csv_path}")
        self.worker = threading.Thread(target=self._stream_worker, args=(mode, csv_path), daemon=True)
        self.worker.start()

    def _stream_worker(self, mode: str, csv_path: Path) -> None:
        sample_count = 0
        try:
            with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=CSV_FIELDS,
                )
                writer.writeheader()
                handle.flush()
                try:
                    with session_factory(mode) as session:
                        identity = session.query("*IDN?")
                        if not identity_is_expected(identity):
                            raise RuntimeError(f"Identity changed before streaming: {identity!r}")
                        while True:
                            pending_markers = self._drain_touch_events()
                            for marker_elapsed in pending_markers:
                                writer.writerow(touch_csv_record(marker_elapsed))
                            if pending_markers:
                                handle.flush()
                            if self.stop_event.is_set():
                                break

                            loop_started = time.monotonic()
                            query_started = time.perf_counter()
                            raw = session.query(FETCH_QUERY)
                            query_elapsed_ms = round((time.perf_counter() - query_started) * 1000, 3)
                            voltage = parse_voltage(raw)
                            elapsed = time.monotonic() - self.stream_start_monotonic

                            records = [sample_csv_record(elapsed, voltage, raw, query_elapsed_ms)]
                            records.extend(
                                touch_csv_record(marker_elapsed)
                                for marker_elapsed in self._drain_touch_events()
                            )
                            records.sort(key=lambda record: float(record["elapsed_seconds"]))
                            writer.writerows(records)
                            sample_count += 1
                            if sample_count % 5 == 0 or len(records) > 1:
                                handle.flush()
                            self.events.put(("sample", (elapsed, voltage, raw, sample_count)))
                            remaining = self.poll_interval_s - (time.monotonic() - loop_started)
                            if remaining > 0:
                                self.stop_event.wait(remaining)
                finally:
                    pending_markers = self._drain_touch_events()
                    for marker_elapsed in pending_markers:
                        writer.writerow(touch_csv_record(marker_elapsed))
                    handle.flush()
        except Exception as exc:
            self.events.put(("stream_error", f"{type(exc).__name__}: {exc}"))
        finally:
            self.events.put(("stream_stopped", sample_count))

    def _pause_stream(self) -> None:
        self.live_running = False
        self.stop_event.set()
        self.pause_button.configure(state="disabled")
        self.mark_touch_button.configure(state="disabled")
        self.status_var.set("Stopping after the current VISA query completes...")

    def _mark_touch(self) -> None:
        if not self.live_running:
            return
        elapsed = max(0.0, time.monotonic() - self.stream_start_monotonic)
        self.touch_markers.append(elapsed)
        self.touch_events.put(elapsed)
        self._draw_plot()
        self.status_var.set(
            f"Touch {len(self.touch_markers)} marked at elapsed {elapsed:.3f} s; "
            "the marker is a host-only event and sends no instrument message."
        )

    def _drain_touch_events(self) -> list[float]:
        pending: list[float] = []
        while True:
            try:
                pending.append(self.touch_events.get_nowait())
            except queue.Empty:
                return pending

    def _single_fetch(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        if not self._confirm_real_access():
            return
        self._set_busy(True)
        mode = self.mode_var.get()
        self.worker = threading.Thread(target=self._single_fetch_worker, args=(mode,), daemon=True)
        self.worker.start()

    def _single_fetch_worker(self, mode: str) -> None:
        try:
            started = time.perf_counter()
            with session_factory(mode) as session:
                identity = session.query("*IDN?")
                if not identity_is_expected(identity):
                    raise RuntimeError(f"Identity mismatch: {identity!r}")
                raw = session.query(FETCH_QUERY)
            query_ms = round((time.perf_counter() - started) * 1000, 3)
            voltage = parse_voltage(raw)
            elapsed = self.samples[-1][0] if self.samples else 0.0
            self.events.put(("single", (elapsed, voltage, raw, query_ms)))
        except Exception as exc:
            self.events.put(("error", f"{type(exc).__name__}: {exc}"))

    def _clear_plot(self) -> None:
        self.samples.clear()
        self.touch_markers.clear()
        self.reading_var.set("No sample")
        self.raw_var.set("Raw: —")
        self._draw_plot()
        self.status_var.set("Plot memory cleared. Existing CSV evidence was not deleted.")

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        self.config_button.configure(state=state)
        self.mode_combo.configure(state="disabled" if busy else "readonly")
        if busy:
            self.start_button.configure(state="disabled")
            self.single_button.configure(state="disabled")
            self.mark_touch_button.configure(state="disabled")
        elif self.configuration:
            self.start_button.configure(state="normal")
            self.single_button.configure(state="normal")

    def _show_configuration(self, report: dict[str, object]) -> None:
        self.config_tree.delete(*self.config_tree.get_children())
        transcript = report.get("transcript", [])
        for item in transcript if isinstance(transcript, list) else []:
            if not isinstance(item, dict):
                continue
            value = item.get("response") if item.get("ok") else item.get("error", "ERROR")
            self.config_tree.insert(
                "", "end", values=(item.get("name", ""), value, item.get("command", ""))
            )

    def _drain_events(self) -> None:
        try:
            while True:
                event, payload = self.events.get_nowait()
                if event == "configuration":
                    report, run_directory, evidence_file = payload
                    self.configuration = report
                    self.run_directory = run_directory
                    self.poll_interval_s = float(report["derived_poll_interval_s"])
                    self._show_configuration(report)
                    ready = bool(report.get("live_readiness", {}).get("ready"))
                    self._set_busy(False)
                    self.start_button.configure(state="normal" if ready else "disabled")
                    self.single_button.configure(state="normal" if ready else "disabled")
                    self.status_var.set(
                        f"Configuration complete: {len(report['values'])}/{len(CONFIG_QUERIES)} values; "
                        f"derived poll interval {self.poll_interval_s:.3f} s."
                    )
                    self.evidence_var.set(f"Configuration evidence: {evidence_file}")
                    self.mode_status_var.set(
                        "Real query-only VISA access confirmed." if report["mode"] == "real"
                        else "Simulation configuration loaded; no VISA communication."
                    )
                elif event == "sample":
                    elapsed, voltage, raw, count = payload
                    self.samples.append((elapsed, voltage))
                    if self.live_running:
                        self.mark_touch_button.configure(state="normal")
                    self.reading_var.set(format_voltage(voltage))
                    self.raw_var.set(f"Raw: {raw}")
                    self.status_var.set(
                        f"Running · samples: {count} · elapsed: {elapsed:.1f} s · "
                        f"poll interval: {self.poll_interval_s:.3f} s"
                    )
                    self._draw_plot()
                elif event == "single":
                    _elapsed, voltage, raw, query_ms = payload
                    self.reading_var.set(format_voltage(voltage))
                    self.raw_var.set(f"Raw: {raw}")
                    self.status_var.set(f"Single FETCh? completed in {query_ms:.3f} ms; plot was not changed.")
                    self._set_busy(False)
                elif event == "error":
                    self._set_busy(False)
                    self.status_var.set(f"Error: {payload}")
                    self.messagebox.showerror("GPIB6 monitor error", str(payload))
                elif event == "stream_error":
                    self.live_running = False
                    self.mark_touch_button.configure(state="disabled")
                    self.status_var.set(f"Live stream error: {payload}")
                    self.messagebox.showerror("Live stream stopped", str(payload))
                elif event == "stream_stopped":
                    self.live_running = False
                    self.pause_button.configure(state="disabled")
                    self.mark_touch_button.configure(state="disabled")
                    self.mode_combo.configure(state="readonly")
                    self.config_button.configure(state="normal")
                    self.start_button.configure(state="normal" if self.configuration else "disabled")
                    self.single_button.configure(state="normal" if self.configuration else "disabled")
                    if not self.status_var.get().startswith("Live stream error"):
                        self.status_var.set(f"Paused safely after {payload} samples. VISA session closed.")
        except queue.Empty:
            pass
        self.root.after(100, self._drain_events)

    def _draw_plot(self) -> None:
        canvas = self.canvas
        canvas.delete("all")
        width = max(canvas.winfo_width(), 300)
        height = max(canvas.winfo_height(), 220)
        left, right, top, bottom = 78, 18, 20, 48
        plot_w = width - left - right
        plot_h = height - top - bottom
        canvas.create_rectangle(left, top, left + plot_w, top + plot_h, outline="#9aa5b1")

        visible, visible_markers = visible_plot_data(self.samples, self.touch_markers)
        if not visible:
            canvas.create_text(width / 2, height / 2, text="No voltage samples", fill="#687481")
            canvas.create_text(width / 2, height - 15, text="Elapsed host time (s)", fill="#33404d")
            canvas.create_text(18, height / 2, text="Voltage", angle=90, fill="#33404d")
            return

        xs = [point[0] for point in visible]
        ys = [point[1] for point in visible]
        x_values = xs + [elapsed for _number, elapsed in visible_markers]
        x_min, x_max = min(x_values), max(x_values)
        if x_max <= x_min:
            x_max = x_min + 1.0
        y_min, y_max = min(ys), max(ys)
        if y_max <= y_min:
            padding = max(abs(y_min) * 0.05, 1e-12)
        else:
            padding = (y_max - y_min) * 0.08
        y_min -= padding
        y_max += padding

        for index in range(6):
            fraction = index / 5
            x = left + fraction * plot_w
            y = top + fraction * plot_h
            canvas.create_line(x, top, x, top + plot_h, fill="#edf0f3")
            canvas.create_line(left, y, left + plot_w, y, fill="#edf0f3")
            x_value = x_min + fraction * (x_max - x_min)
            y_value = y_max - fraction * (y_max - y_min)
            canvas.create_text(x, top + plot_h + 17, text=f"{x_value:.1f}", fill="#4b5966")
            canvas.create_text(left - 8, y, text=f"{y_value:.3e}", anchor="e", fill="#4b5966")

        coordinates: list[float] = []
        for x_value, y_value in visible:
            x = left + (x_value - x_min) / (x_max - x_min) * plot_w
            y = top + (y_max - y_value) / (y_max - y_min) * plot_h
            coordinates.extend((x, y))
        if len(coordinates) >= 4:
            canvas.create_line(*coordinates, fill="#1769aa", width=2)
        else:
            canvas.create_oval(
                coordinates[0] - 2,
                coordinates[1] - 2,
                coordinates[0] + 2,
                coordinates[1] + 2,
                fill="#1769aa",
                outline="",
            )
        for marker_number, marker_elapsed in visible_markers:
            x = left + (marker_elapsed - x_min) / (x_max - x_min) * plot_w
            canvas.create_line(x, top, x, top + plot_h, fill="#d62728", width=2)
            canvas.create_text(
                x + 4,
                top + 4,
                text=f"Touch {marker_number}",
                anchor="nw",
                fill="#b01f2e",
            )
        canvas.create_text(width / 2, height - 12, text="Elapsed host time (s)", fill="#33404d")
        canvas.create_text(15, height / 2, text="Voltage (V)", angle=90, fill="#33404d")

    def _close(self) -> None:
        self.live_running = False
        self.stop_event.set()
        self.root.after(150, self.root.destroy)


def main() -> int:
    import tkinter as tk

    root = tk.Tk()
    MonitorApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
