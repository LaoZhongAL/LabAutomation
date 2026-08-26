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
import threading
import time
from collections import deque
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from diagnostic_core import (
    GPIB6_TARGET,
    DiagnosticState,
    evaluate_readiness,
    identity_is_exact,
    parse_idn,
)
from fault_injection import FAULT_SCENARIO_NAMES, SimulationContext
from run_evidence import INTERVENTION_TYPES, RecorderError, RunJournal


RESOURCE = GPIB6_TARGET.resource
EXPECTED_MODEL = GPIB6_TARGET.model
EXPECTED_SERIAL = GPIB6_TARGET.serial

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
    "elapsed_seconds",
    "voltage_v",
    "raw_response",
    "query_elapsed_ms",
)
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


def write_json_atomic(path: Path, payload: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    rendered = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    try:
        temporary.write_text(rendered, encoding="utf-8")
        temporary.replace(path)
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def parse_voltage(raw: str) -> float:
    """Parse the first ASCII numeric field returned by FETCh?."""
    text = raw.strip()
    if not text:
        raise ValueError("empty response")
    value = float(text.split(",", 1)[0].strip())
    if not math.isfinite(value):
        raise ValueError(f"non-finite voltage: {text!r}")
    if abs(value) >= 1e37:
        raise ValueError(f"instrument overrange sentinel: {text!r}")
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
    return identity_is_exact(identity)


def live_start_is_safe(
    configuration: dict[str, object] | None,
    state: DiagnosticState | None,
    *,
    recording_fault_latched: bool,
    stream_had_error: bool,
) -> bool:
    diagnostics = configuration.get("diagnostics") if configuration else None
    return bool(
        isinstance(diagnostics, dict)
        and diagnostics.get("can_start_live")
        and state == DiagnosticState.OBSERVE_READY
        and not recording_fault_latched
        and not stream_had_error
    )


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
        "elapsed_seconds": f"{elapsed_seconds:.6f}",
        "voltage_v": f"{voltage_v:.12g}",
        "raw_response": raw_response,
        "query_elapsed_ms": f"{query_elapsed_ms:.3f}",
    }


def visible_plot_data(
    samples,
    interventions,
    window_seconds: float = PLOT_WINDOW_SECONDS,
) -> tuple[list[tuple[float, float]], list[dict[str, object]]]:
    """Return samples and intervention intervals inside the latest plot window."""
    sample_values = list(samples)
    interval_values = [dict(item) for item in interventions]
    times = [elapsed for elapsed, _voltage in sample_values]
    for interval in interval_values:
        times.extend(
            (
                float(interval["start_elapsed_seconds"]),
                float(interval["end_elapsed_seconds"]),
            )
        )
    if not times:
        return [], []
    cutoff = max(0.0, max(times) - window_seconds)
    visible_intervals: list[dict[str, object]] = []
    for interval in interval_values:
        start = float(interval["start_elapsed_seconds"])
        end = float(interval["end_elapsed_seconds"])
        if end < cutoff:
            continue
        clipped = dict(interval)
        clipped["start_elapsed_seconds"] = max(start, cutoff)
        visible_intervals.append(clipped)
    return (
        [point for point in sample_values if point[0] >= cutoff],
        visible_intervals,
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
    def __init__(
        self,
        context: SimulationContext | None = None,
        phase: str = "stream",
    ) -> None:
        self.context = context or SimulationContext("nominal", ALLOWED_QUERIES)
        self.phase = phase

    def __enter__(self):
        return self

    def query(self, command: str) -> str:
        if command not in ALLOWED_QUERIES:
            raise ValueError(f"Blocked non-allow-listed message: {command!r}")
        return self.context.execute_query(
            self.phase,
            command,
            lambda: (
                self.context.next_voltage()
                if command == FETCH_QUERY
                else SIMULATED_VALUES[command]
            ),
        )

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None


def session_factory(
    mode: str,
    phase: str = "config",
    simulation_context: SimulationContext | None = None,
):
    if mode == "real":
        if simulation_context is not None:
            raise ValueError("fault injection is forbidden in real mode")
        return RealSession()
    if mode == "simulate":
        return SimulatedSession(simulation_context, phase)
    raise ValueError(f"Unsupported mode: {mode}")


def collect_configuration(
    mode: str,
    simulation_context: SimulationContext | None = None,
    *,
    recorder_ready: bool = True,
) -> dict[str, object]:
    if mode == "real" and simulation_context is not None:
        raise ValueError("fault injection is forbidden in real mode")
    transcript: list[dict[str, object]] = []
    values: dict[str, str] = {}
    try:
        with session_factory(mode, "config", simulation_context) as session:
            for name, command in CONFIG_QUERIES:
                started = time.perf_counter()
                item: dict[str, object] = {
                    "name": name,
                    "command": command,
                }
                try:
                    response = session.query(command)
                    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
                    values[name] = response
                    item.update(
                        {
                            "ok": True,
                            "response": response,
                            "elapsed_ms": elapsed_ms,
                        }
                    )
                    if name == "identity" and not identity_is_expected(response):
                        item.update(
                            {
                                "ok": False,
                                "error": (
                                    "Exact identity mismatch. Expected "
                                    f"{GPIB6_TARGET.vendor}, MODEL {EXPECTED_MODEL}, "
                                    f"serial {EXPECTED_SERIAL}, firmware {GPIB6_TARGET.firmware}; "
                                    f"got {response!r}"
                                ),
                            }
                        )
                        transcript.append(item)
                        break
                    transcript.append(item)
                except Exception as exc:
                    item.update(
                        {
                            "ok": False,
                            "error": f"{type(exc).__name__}: {exc}",
                            "elapsed_ms": round(
                                (time.perf_counter() - started) * 1000,
                                3,
                            ),
                        }
                    )
                    transcript.append(item)
                    break
    except Exception as exc:
        if not transcript:
            transcript.append(
                {
                    "name": "identity",
                    "command": "*IDN?",
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "elapsed_ms": 0.0,
                }
            )

    readiness = evaluate_readiness(
        values,
        transcript,
        [name for name, _command in CONFIG_QUERIES],
        recorder_ready=recorder_ready,
    )
    readiness_dict = readiness.as_dict()
    return {
        "created_at": now_iso(),
        "operation": "query-only single-instrument diagnostic snapshot",
        "mode": mode,
        "resource": RESOURCE,
        "expected_identity": {
            "vendor": GPIB6_TARGET.vendor,
            "model": EXPECTED_MODEL,
            "serial": EXPECTED_SERIAL,
            "firmware": GPIB6_TARGET.firmware,
        },
        "safety": {
            "query_only": True,
            "exact_allowlist": True,
            "generic_write_api_exposed": False,
            "configuration_controls_exposed": False,
            "live_query": FETCH_QUERY,
        },
        "values": values,
        "transcript": transcript,
        "diagnostics": readiness_dict,
        "live_readiness": {
            "identity_matches": identity_is_expected(values.get("identity", "")),
            "ready": readiness.can_start_live,
            "overall": readiness.overall.value,
            "blockers": readiness_dict["blockers"],
            "warnings": readiness_dict["warnings"],
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
        self.root.title("Keithley 2182A GPIB6 Query-Only Diagnostic Core v1.1")
        self.root.geometry("1380x880")
        self.root.minsize(1080, 720)

        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.output_root = Path.cwd() / "monitor_runs"
        self.run_directory: Path | None = None
        self.configuration: dict[str, object] | None = None
        self.diagnostic_run: RunJournal | None = None
        self.simulation_context: SimulationContext | None = None
        self.stream_id: str | None = None
        self.samples: deque[tuple[float, float]] = deque(maxlen=20000)
        self.interventions: list[dict[str, object]] = []
        self.active_intervention: dict[str, object] | None = None
        self.intervention_ready = False
        self.stream_start_monotonic = 0.0
        self.poll_interval_s = 0.5
        self.real_access_confirmed = False
        self.live_running = False
        self.stream_had_error = False
        self.stream_stop_fault: str | None = None
        self.recording_fault_latched = False
        self.selected_mode = "simulate"
        self.selected_fault = "nominal"
        self.incomplete_run_paths: list[Path] = []
        self.closing = False

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
        style.configure("Fault.TLabel", foreground="#a51d2d", font=("Segoe UI", 10, "bold"))
        style.configure("Reading.TLabel", font=("Segoe UI", 24, "bold"), foreground="#123e73")
        style.configure("Action.TButton", font=("Segoe UI", 10, "bold"), padding=(12, 8))
        style.configure("Treeview", rowheight=25, font=("Segoe UI", 9))

    def _build_ui(self) -> None:
        tk, ttk = self.tk, self.ttk
        outer = ttk.Frame(self.root, padding=14)
        outer.pack(fill="both", expand=True)

        ttk.Label(
            outer,
            text="Keithley 2182A · GPIB6 Single-Instrument Diagnostic Core v1.1",
            style="Title.TLabel",
        ).pack(anchor="w")
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
            text="1. Run Read-Only Diagnostics",
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
        self.single_button = ttk.Button(
            controls, text="Single FETCh?", state="disabled", command=self._single_fetch
        )
        self.single_button.pack(side="left", padx=(7, 0))
        self.clear_button = ttk.Button(controls, text="Clear Plot", command=self._clear_plot)
        self.clear_button.pack(side="left", padx=(7, 0))
        self.output_button = ttk.Button(controls, text="Output Folder", command=self._choose_output)
        self.output_button.pack(side="left", padx=(7, 0))

        diagnostic_controls = ttk.Frame(outer)
        diagnostic_controls.pack(fill="x", pady=(6, 0))
        ttk.Label(diagnostic_controls, text="Simulation fault:").pack(side="left")
        self.fault_var = tk.StringVar(value="nominal")
        self.fault_combo = ttk.Combobox(
            diagnostic_controls,
            state="readonly",
            width=28,
            values=FAULT_SCENARIO_NAMES,
            textvariable=self.fault_var,
        )
        self.fault_combo.pack(side="left", padx=(5, 14))
        self.fault_combo.bind("<<ComboboxSelected>>", self._fault_changed)
        ttk.Label(
            diagnostic_controls,
            text="Simulation only · deterministic · never adds an instrument command",
            style="Safe.TLabel",
        ).pack(side="left")

        intervention_controls = ttk.Frame(outer)
        intervention_controls.pack(fill="x", pady=(6, 0))
        ttk.Label(intervention_controls, text="Intervention type:").pack(side="left")
        self.intervention_type_var = tk.StringVar(value=INTERVENTION_TYPES[0])
        self.intervention_type_combo = ttk.Combobox(
            intervention_controls,
            state="readonly",
            width=29,
            values=INTERVENTION_TYPES,
            textvariable=self.intervention_type_var,
        )
        self.intervention_type_combo.pack(side="left", padx=(5, 12))
        ttk.Label(intervention_controls, text="Location:").pack(side="left")
        self.intervention_location_var = tk.StringVar(value="")
        self.intervention_location_entry = ttk.Entry(
            intervention_controls,
            width=34,
            textvariable=self.intervention_location_var,
        )
        self.intervention_location_entry.pack(side="left", padx=(5, 12))
        self.mark_intervention_button = ttk.Button(
            intervention_controls,
            text="Mark Intervention: Start",
            state="disabled",
            command=self._mark_intervention,
        )
        self.mark_intervention_button.pack(side="left")
        ttk.Label(
            intervention_controls,
            text="Host-side label only · no instrument message",
            style="Safe.TLabel",
        ).pack(side="left", padx=(12, 0))

        self.mode_status_var = tk.StringVar(value="Simulation selected; no VISA communication.")
        ttk.Label(outer, textvariable=self.mode_status_var).pack(anchor="w", pady=(6, 2))
        self.diagnostic_state_var = tk.StringVar(value="State: DISCONNECTED")
        ttk.Label(outer, textvariable=self.diagnostic_state_var, style="Fault.TLabel").pack(anchor="w")
        self.readiness_var = tk.StringVar(value="Readiness: not evaluated")
        ttk.Label(outer, textvariable=self.readiness_var).pack(anchor="w", pady=(1, 6))

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
            columns=("status", "parameter", "value", "query"),
            show="headings",
            selectmode="browse",
        )
        self.config_tree.heading("status", text="Status")
        self.config_tree.heading("parameter", text="Parameter")
        self.config_tree.heading("value", text="Instrument response")
        self.config_tree.heading("query", text="Exact query / expected")
        self.config_tree.column("status", width=82, stretch=False)
        self.config_tree.column("parameter", width=150, stretch=False)
        self.config_tree.column("value", width=225, stretch=True)
        self.config_tree.column("query", width=250, stretch=True)
        self.config_tree.pack(side="left", fill="both", expand=True)
        config_scroll = ttk.Scrollbar(config_box, orient="vertical", command=self.config_tree.yview)
        config_scroll.pack(side="right", fill="y")
        self.config_tree.configure(yscrollcommand=config_scroll.set)
        self.config_tree.tag_configure("PASS", foreground="#176b3a")
        self.config_tree.tag_configure("WARN", foreground="#8a5a00")
        self.config_tree.tag_configure("BLOCKED", foreground="#a51d2d")
        self.config_tree.tag_configure("UNKNOWN", foreground="#5f6872")
        self.config_tree.tag_configure("N/A", foreground="#5f6872")

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
        self.status_var = tk.StringVar(
            value="Ready. Run Read-Only Diagnostics before starting the plot."
        )
        ttk.Label(footer, textvariable=self.status_var).pack(anchor="w")
        self.evidence_var = tk.StringVar(value=f"Output root: {self.output_root}")
        ttk.Label(footer, textvariable=self.evidence_var).pack(anchor="w", pady=(2, 0))

    def _mode_changed(self, _event=None) -> None:
        if self.worker and self.worker.is_alive():
            self.mode_var.set(self.selected_mode)
            self.messagebox.showwarning("Monitor running", "Pause the live plot before changing mode.")
            return
        self._finalize_diagnostic_run("mode_changed")
        self.selected_mode = self.mode_var.get()
        self.configuration = None
        self.run_directory = None
        self.simulation_context = None
        self.stream_id = None
        self.real_access_confirmed = False
        self.live_running = False
        self.active_intervention = None
        self.intervention_ready = False
        self.stream_had_error = False
        self.recording_fault_latched = False
        self.start_button.configure(state="disabled")
        self.single_button.configure(state="disabled")
        self.mark_intervention_button.configure(
            text="Mark Intervention: Start",
            state="disabled",
        )
        self.diagnostic_state_var.set("State: DISCONNECTED")
        self.readiness_var.set("Readiness: not evaluated")
        if self.selected_mode == "real":
            self.fault_var.set("nominal")
            self.selected_fault = "nominal"
            self.fault_combo.configure(state="disabled")
        else:
            self.fault_combo.configure(state="readonly")
        self.mode_status_var.set(
            "Real VISA selected; configuration has not been read."
            if self.selected_mode == "real"
            else "Simulation selected; no VISA communication."
        )

    def _fault_changed(self, _event=None) -> None:
        if self.worker and self.worker.is_alive():
            self.fault_var.set(self.selected_fault)
            self.messagebox.showwarning(
                "Diagnostic run active",
                "Pause or wait for the current operation before changing the fault scenario.",
            )
            return
        selected = self.fault_var.get()
        if selected == self.selected_fault:
            return
        self.selected_fault = selected
        if self.configuration is not None or self.diagnostic_run is not None:
            self._invalidate_current_diagnostics("fault_scenario_changed")
            self.status_var.set(
                f"Fault scenario changed to {selected}; run Read-Only Diagnostics again."
            )

    def _finalize_diagnostic_run(self, reason: str) -> bool:
        journal = self.diagnostic_run
        if journal is None:
            return True
        errors: list[str] = []
        try:
            if journal.state != DiagnosticState.DISCONNECTED:
                journal.transition(
                    DiagnosticState.DISCONNECTED,
                    reason_code="RUN_DISCONNECTED",
                    payload={"reason": reason},
                )
        except Exception as exc:
            errors.append(f"disconnect transition: {type(exc).__name__}: {exc}")
        try:
            journal.finalize(reason)
        except Exception as exc:
            errors.append(f"manifest finalization: {type(exc).__name__}: {exc}")
        if errors:
            self.recording_fault_latched = True
            self.incomplete_run_paths.append(journal.manifest_path)
            warning = (
                f"Run evidence may be incomplete: {journal.manifest_path} · "
                + " · ".join(errors)
            )
            print(f"WARNING: {warning}")
            if not self.closing:
                self.messagebox.showwarning("Run evidence incomplete", warning)
        self.diagnostic_run = None
        self.stream_id = None
        return not errors

    def _invalidate_current_diagnostics(self, reason: str) -> None:
        self._finalize_diagnostic_run(reason)
        self.configuration = None
        self.run_directory = None
        self.simulation_context = None
        self.stream_id = None
        self.start_button.configure(state="disabled")
        self.single_button.configure(state="disabled")
        self.active_intervention = None
        self.intervention_ready = False
        self.mark_intervention_button.configure(
            text="Mark Intervention: Start",
            state="disabled",
        )
        self.diagnostic_state_var.set("State: DISCONNECTED")
        self.readiness_var.set("Readiness: not evaluated")

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
            if self.configuration is not None or self.diagnostic_run is not None:
                self._invalidate_current_diagnostics("output_root_changed")
            self.output_root = Path(selected)
            self.evidence_var.set(f"Output root: {self.output_root}")
            self.status_var.set("Output folder changed; run Read-Only Diagnostics again.")

    def _read_configuration(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        if not self._confirm_real_access():
            return
        self._finalize_diagnostic_run("new_diagnostic_requested")
        self.configuration = None
        self.run_directory = None
        self.simulation_context = None
        self.stream_id = None
        self.stream_had_error = False
        self.recording_fault_latched = False
        self.start_button.configure(state="disabled")
        self.single_button.configure(state="disabled")
        self.active_intervention = None
        self.intervention_ready = False
        self.mark_intervention_button.configure(
            text="Mark Intervention: Start",
            state="disabled",
        )
        self.readiness_var.set("Readiness: evaluating")
        self.diagnostic_state_var.set("State: VERIFYING_IDENTITY (pending recorder preflight)")
        self._set_busy(True)
        self.status_var.set("Creating run evidence before the first allow-listed query...")
        mode = self.mode_var.get()
        fault_scenario = self.fault_var.get() if mode == "simulate" else "nominal"
        self.selected_fault = fault_scenario
        output_root = self.output_root
        self.worker = threading.Thread(
            target=self._configuration_worker,
            args=(mode, fault_scenario, output_root),
            daemon=True,
        )
        self.worker.start()

    def _configuration_worker(
        self,
        mode: str,
        fault_scenario: str,
        output_root: Path,
    ) -> None:
        run_directory: Path | None = None
        evidence_file: Path | None = None
        journal: RunJournal | None = None
        simulation_context: SimulationContext | None = None
        try:
            if mode == "real" and fault_scenario != "nominal":
                raise ValueError("fault injection is forbidden in real mode")
            run_directory = output_root / f"{timestamp_name()}-{mode}-gpib6-diagnostic-v1.1"
            run_directory.mkdir(parents=True, exist_ok=False)
            if mode == "simulate":
                simulation_context = SimulationContext(fault_scenario, ALLOWED_QUERIES)
            journal = RunJournal(
                run_directory,
                mode=mode,
                allowed_queries=ALLOWED_QUERIES,
                fault_scenario=fault_scenario,
                fail_event_write_after=(
                    simulation_context.event_write_fail_after
                    if simulation_context is not None
                    else None
                ),
            )
            journal.transition(
                DiagnosticState.VERIFYING_IDENTITY,
                reason_code="IDENTITY_VERIFICATION_STARTED",
            )
            journal.record_event(
                "configuration_started",
                reason_code="CONFIGURATION_DIAGNOSTIC_STARTED",
                payload={"query_count": len(CONFIG_QUERIES)},
            )
            journal.record_event(
                "query_plan_committed",
                reason_code="QUERY_PLAN_COMMITTED",
                payload={
                    "queries": [command for _name, command in CONFIG_QUERIES],
                    "query_only": True,
                },
            )
            report = collect_configuration(
                mode,
                simulation_context,
                recorder_ready=True,
            )
            report["fault_injection"] = {
                "scenario": fault_scenario if mode == "simulate" else "nominal",
                "consumed_rule_ids": (
                    list(simulation_context.consumed_rule_ids)
                    if simulation_context is not None
                    else []
                ),
                "query_history": (
                    list(simulation_context.query_history)
                    if simulation_context is not None
                    else []
                ),
            }
            identity_exact = identity_is_expected(
                str(report.get("values", {}).get("identity", ""))
            )
            if identity_exact:
                journal.transition(
                    DiagnosticState.CHECKING_CONFIG,
                    reason_code="EXACT_IDENTITY_VERIFIED",
                )
            evidence_file = run_directory / "configuration-snapshot.json"
            write_json_atomic(evidence_file, report)
            journal.set_configuration_snapshot(evidence_file.name)

            observed_identity = None
            try:
                observed_identity = asdict(
                    parse_idn(str(report.get("values", {}).get("identity", "")))
                )
            except Exception:
                pass
            diagnostics = report["diagnostics"]
            journal.set_diagnostics(
                observed_identity=observed_identity,
                readiness=diagnostics,
            )
            if bool(diagnostics.get("can_start_live")):
                journal.transition(
                    DiagnosticState.OBSERVE_READY,
                    reason_code="READINESS_OBSERVE_READY",
                )
            else:
                journal.transition(
                    DiagnosticState.FAULT_LATCHED,
                    reason_code=(
                        "IDENTITY_MISMATCH" if not identity_exact else "READINESS_BLOCKED"
                    ),
                    severity="ERROR",
                )
            self.events.put(
                (
                    "configuration",
                    (
                        report,
                        run_directory,
                        evidence_file,
                        journal,
                        simulation_context,
                    ),
                )
            )
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            if journal is not None:
                try:
                    journal.record_error(
                        reason_code="CONFIGURATION_DIAGNOSTIC_FAILED",
                        message=message,
                    )
                except Exception:
                    pass
                try:
                    if journal.state in {
                        DiagnosticState.VERIFYING_IDENTITY,
                        DiagnosticState.CHECKING_CONFIG,
                    }:
                        journal.transition(
                            DiagnosticState.FAULT_LATCHED,
                            reason_code="CONFIGURATION_DIAGNOSTIC_FAILED",
                            severity="ERROR",
                        )
                except Exception:
                    pass
            failure_file = None
            if run_directory is not None:
                try:
                    failure_file = run_directory / "configuration-failure.json"
                    failure_file.write_text(
                        json.dumps(
                            {
                                "created_at": now_iso(),
                                "mode": mode,
                                "fault_scenario": fault_scenario,
                                "error": message,
                            },
                            indent=2,
                            ensure_ascii=False,
                        ),
                        encoding="utf-8",
                    )
                except Exception:
                    failure_file = None
            self.events.put(
                (
                    "configuration_error",
                    (message, run_directory, failure_file, journal, simulation_context),
                )
            )

    def _start_stream(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        if self.recording_fault_latched or self.stream_had_error:
            self.messagebox.showerror(
                "Recorder fault latched",
                "The previous live/recorder failure is latched. Run read-only diagnostics again.",
            )
            return
        if not self.configuration or not self.run_directory or not self.diagnostic_run:
            self.messagebox.showwarning(
                "Diagnostics required",
                "Run Read-Only Diagnostics first.",
            )
            return
        diagnostics = self.configuration.get("diagnostics", {})
        if not isinstance(diagnostics, dict) or not diagnostics.get("can_start_live"):
            self.messagebox.showerror(
                "Readiness blocked",
                "One or more identity, communication, CH1 acquisition, or recorder checks block live observation. "
                "The program will not change the instrument to correct them.",
            )
            return
        if self.diagnostic_run.state != DiagnosticState.OBSERVE_READY:
            self.messagebox.showerror(
                "State blocks live observation",
                f"Current diagnostic state is {self.diagnostic_run.state.value}; rerun diagnostics.",
            )
            return
        if not self._confirm_real_access():
            return
        self.stop_event.clear()
        self.samples.clear()
        self.interventions.clear()
        self.active_intervention = None
        self.intervention_ready = False
        self.stream_stop_fault = None
        self.reading_var.set("No sample")
        self.raw_var.set("Raw: —")
        self.stream_start_monotonic = time.monotonic()
        mode = self.mode_var.get()
        csv_path = self.run_directory / f"voltage-{timestamp_name()}.csv"
        try:
            stream_id = self.diagnostic_run.register_stream(csv_path)
        except Exception as exc:
            self.recording_fault_latched = True
            try:
                if self.diagnostic_run.state == DiagnosticState.OBSERVE_READY:
                    self.diagnostic_run.transition(
                        DiagnosticState.FAULT_LATCHED,
                        reason_code="STREAM_REGISTRATION_FAILED",
                        severity="ERROR",
                    )
            except Exception:
                pass
            self.diagnostic_state_var.set(
                f"State: {self.diagnostic_run.state.value} · RECORDER FAULT LATCHED"
            )
            self.status_var.set(f"Recorder preflight failed: {type(exc).__name__}: {exc}")
            self.messagebox.showerror("Live start blocked", self.status_var.get())
            return
        self.stream_id = stream_id
        stream_context = None
        if mode == "simulate":
            scenario = (
                self.simulation_context.scenario_id
                if self.simulation_context is not None
                else self.selected_fault
            )
            stream_context = SimulationContext(scenario, ALLOWED_QUERIES)
        self.live_running = True
        self.stream_had_error = False
        self.start_button.configure(state="disabled")
        self.config_button.configure(state="disabled")
        self.mode_combo.configure(state="disabled")
        self.fault_combo.configure(state="disabled")
        self.output_button.configure(state="disabled")
        self.clear_button.configure(state="disabled")
        self.single_button.configure(state="disabled")
        self.pause_button.configure(state="normal")
        self.mark_intervention_button.configure(
            text="Mark Intervention: Start",
            state="disabled",
        )
        self.intervention_type_combo.configure(state="readonly")
        self.intervention_location_entry.configure(state="normal")
        self._draw_plot()
        self.status_var.set(f"Live FETCh? polling started every {self.poll_interval_s:.3f} s.")
        self.evidence_var.set(
            f"Readout CSV: {csv_path} · Interventions: "
            f"{self.diagnostic_run.interventions_path}"
        )
        self.worker = threading.Thread(
            target=self._stream_worker,
            args=(
                mode,
                csv_path,
                stream_context,
                self.diagnostic_run,
                stream_id,
            ),
            daemon=True,
        )
        self.worker.start()

    def _stream_worker(
        self,
        mode: str,
        csv_path: Path,
        simulation_context: SimulationContext | None = None,
        journal: RunJournal | None = None,
        stream_id: str | None = None,
    ) -> None:
        sample_count = 0
        stream_started = False
        stream_error: str | None = None
        fault_intervention_end: dict[str, object] | None = None

        try:
            if mode == "real" and simulation_context is not None:
                raise ValueError("fault injection is forbidden in real mode")
            if simulation_context is not None and simulation_context.should_fail_csv_open():
                raise OSError("simulated CSV open failure")
            with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=CSV_FIELDS,
                )
                writer.writeheader()
                handle.flush()
                try:
                    with session_factory(mode, "stream", simulation_context) as session:
                        identity = session.query("*IDN?")
                        if not identity_is_expected(identity):
                            raise RuntimeError(f"Identity changed before streaming: {identity!r}")
                        while True:
                            if self.stop_event.is_set():
                                break

                            loop_started = time.monotonic()
                            query_started = time.perf_counter()
                            raw = session.query(FETCH_QUERY)
                            query_elapsed_ms = round((time.perf_counter() - query_started) * 1000, 3)
                            voltage = parse_voltage(raw)
                            elapsed = time.monotonic() - self.stream_start_monotonic
                            if (
                                simulation_context is not None
                                and simulation_context.should_fail_csv_write(sample_count)
                            ):
                                raise OSError("simulated CSV sample write failure")
                            writer.writerow(
                                sample_csv_record(
                                    elapsed,
                                    voltage,
                                    raw,
                                    query_elapsed_ms,
                                )
                            )
                            handle.flush()
                            sample_count += 1
                            if journal is not None and stream_id is not None:
                                if not stream_started:
                                    journal.transition(
                                        DiagnosticState.LIVE,
                                        reason_code="FIRST_SAMPLE_COMMITTED",
                                    )
                                    journal.stream_started(stream_id)
                                    stream_started = True
                                journal.record_sample(stream_id, elapsed)
                            if not stream_started and journal is None:
                                stream_started = True

                            if journal is not None and stream_started:
                                deadline_ms = self.poll_interval_s * 1000.0
                                if query_elapsed_ms > deadline_ms and journal.state == DiagnosticState.LIVE:
                                    journal.transition(
                                        DiagnosticState.DEGRADED,
                                        reason_code="POLL_DEADLINE_MISSED",
                                        payload={
                                            "query_elapsed_ms": query_elapsed_ms,
                                            "deadline_ms": round(deadline_ms, 3),
                                        },
                                        severity="WARN",
                                    )
                                elif (
                                    query_elapsed_ms <= deadline_ms
                                    and journal.state == DiagnosticState.DEGRADED
                                ):
                                    journal.transition(
                                        DiagnosticState.LIVE,
                                        reason_code="POLL_TIMING_RECOVERED",
                                    )
                            self.events.put(("sample", (elapsed, voltage, raw, sample_count)))
                            remaining = self.poll_interval_s - (time.monotonic() - loop_started)
                            if remaining > 0:
                                self.stop_event.wait(remaining)
                finally:
                    handle.flush()
        except Exception as exc:
            stream_error = f"{type(exc).__name__}: {exc}"
            self.stop_event.set()
            if journal is not None:
                if stream_id is not None:
                    try:
                        fault_intervention_end = journal.stop_interventions_for_stream(
                            stream_id,
                            elapsed_seconds=self._intervention_elapsed(),
                        )
                    except Exception as intervention_exc:
                        stream_error = (
                            f"{stream_error}; intervention end failed: "
                            f"{type(intervention_exc).__name__}: {intervention_exc}"
                        )
                try:
                    journal.record_error(
                        reason_code="LIVE_STREAM_FAILED",
                        message=stream_error,
                        stream_id=stream_id,
                    )
                except Exception:
                    pass
                try:
                    if journal.state == DiagnosticState.LIVE:
                        journal.transition(
                            DiagnosticState.DEGRADED,
                            reason_code="LIVE_STREAM_DEGRADED",
                            severity="ERROR",
                        )
                    if journal.state in {
                        DiagnosticState.OBSERVE_READY,
                        DiagnosticState.LIVE,
                        DiagnosticState.DEGRADED,
                        DiagnosticState.RECOVERING,
                    }:
                        journal.transition(
                            DiagnosticState.FAULT_LATCHED,
                            reason_code="LIVE_STREAM_FAULT_LATCHED",
                            severity="ERROR",
                        )
                except Exception:
                    pass
            self.events.put(
                (
                    "stream_error",
                    {
                        "message": stream_error,
                        "intervention_end": fault_intervention_end,
                    },
                )
            )
        finally:
            host_stream_fault = getattr(self, "stream_stop_fault", None)
            if stream_error is None and host_stream_fault is not None:
                stream_error = f"intervention recorder fault: {host_stream_fault}"
            if journal is not None and stream_id is not None:
                try:
                    if simulation_context is not None:
                        journal.set_stream_fault_evidence(
                            stream_id,
                            scenario=simulation_context.scenario_id,
                            consumed_rule_ids=simulation_context.consumed_rule_ids,
                            query_history=simulation_context.query_history,
                        )
                    if stream_error is None and journal.state in {
                        DiagnosticState.LIVE,
                        DiagnosticState.DEGRADED,
                    }:
                        journal.transition(
                            DiagnosticState.OBSERVE_READY,
                            reason_code="LIVE_STREAM_PAUSED",
                        )
                    journal.finish_stream(
                        stream_id,
                        outcome="paused" if stream_error is None else "fault",
                        error=stream_error,
                    )
                except Exception as exc:
                    finalization_error = f"{type(exc).__name__}: {exc}"
                    if stream_error is None:
                        stream_error = finalization_error
                        try:
                            if journal.state == DiagnosticState.OBSERVE_READY:
                                journal.transition(
                                    DiagnosticState.FAULT_LATCHED,
                                    reason_code="STREAM_FINALIZATION_FAILED",
                                    severity="ERROR",
                                )
                        except Exception:
                            pass
                        self.events.put(
                            (
                                "stream_error",
                                {"message": stream_error, "intervention_end": None},
                            )
                        )
                    else:
                        stream_error = f"{stream_error}; stream finalization failed: {finalization_error}"
            self.events.put(
                (
                    "stream_stopped",
                    {
                        "sample_count": sample_count,
                        "error": stream_error,
                        "stream_id": stream_id,
                    },
                )
            )

    def _intervention_elapsed(self) -> float:
        return max(0.0, time.monotonic() - self.stream_start_monotonic)

    def _latch_intervention_recorder_fault(self, message: str) -> None:
        self.recording_fault_latched = True
        self.stream_had_error = True
        self.stream_stop_fault = message
        self.intervention_ready = False
        self.live_running = False
        self.stop_event.set()
        self.mark_intervention_button.configure(state="disabled")
        self.intervention_type_combo.configure(state="disabled")
        self.intervention_location_entry.configure(state="disabled")
        if self.diagnostic_run is not None:
            try:
                self.diagnostic_run.record_error(
                    reason_code="INTERVENTION_RECORDING_FAILED",
                    message=message,
                    stream_id=self.stream_id,
                )
            except Exception:
                pass
            try:
                if self.diagnostic_run.state in {
                    DiagnosticState.OBSERVE_READY,
                    DiagnosticState.LIVE,
                    DiagnosticState.DEGRADED,
                    DiagnosticState.RECOVERING,
                }:
                    self.diagnostic_run.transition(
                        DiagnosticState.FAULT_LATCHED,
                        reason_code="INTERVENTION_RECORDING_FAILED",
                        severity="ERROR",
                    )
            except Exception:
                pass
        self.status_var.set(f"Intervention recorder fault latched: {message}")
        if not self.closing:
            self.messagebox.showerror(
                "Intervention recorder failure",
                f"Live observation is stopping because interventions.jsonl could not be committed: {message}",
            )

    def _apply_committed_intervention_end(
        self,
        record: dict[str, object],
        *,
        show_status: bool,
    ) -> bool:
        active = self.active_intervention
        if active is None:
            return False
        if str(active["intervention_id"]) != str(record.get("intervention_id")):
            return False
        completed = dict(active)
        completed["end_elapsed_seconds"] = float(record["elapsed_seconds"])
        self.interventions.append(completed)
        self.active_intervention = None
        self.mark_intervention_button.configure(
            text="Mark Intervention: Start",
            state=(
                "normal"
                if self.live_running and self.intervention_ready and not self.stop_event.is_set()
                else "disabled"
            ),
        )
        self.intervention_type_combo.configure(state="readonly")
        self.intervention_location_entry.configure(state="normal")
        self._draw_plot()
        if show_status:
            self.status_var.set(
                f"Intervention {completed['number']} ended at {record['elapsed_seconds']:.3f} s; "
                "interventions.jsonl was flushed. No instrument message was sent."
            )
        return True

    def _end_active_intervention(self, *, show_status: bool = True) -> bool:
        active = self.active_intervention
        if active is None:
            return True
        if self.diagnostic_run is None:
            self._latch_intervention_recorder_fault("diagnostic journal is unavailable")
            return False
        elapsed = self._intervention_elapsed()
        try:
            record = self.diagnostic_run.end_intervention(
                str(active["intervention_id"]),
                elapsed_seconds=elapsed,
            )
        except Exception as exc:
            if self.stop_event.is_set() or self.diagnostic_run.state not in {
                DiagnosticState.LIVE,
                DiagnosticState.DEGRADED,
            }:
                self.mark_intervention_button.configure(state="disabled")
                return False
            self._latch_intervention_recorder_fault(f"{type(exc).__name__}: {exc}")
            return False
        return self._apply_committed_intervention_end(
            record,
            show_status=show_status,
        )

    def _mark_intervention(self) -> None:
        if (
            not self.live_running
            or not self.intervention_ready
            or self.stop_event.is_set()
            or self.diagnostic_run is None
            or self.stream_id is None
        ):
            return
        if self.active_intervention is not None:
            self._end_active_intervention()
            return
        intervention_type = self.intervention_type_var.get().strip()
        location = self.intervention_location_var.get().strip()
        if not location:
            self.messagebox.showwarning(
                "Location required",
                "Enter the physical location before starting an intervention interval.",
            )
            return
        elapsed = self._intervention_elapsed()
        try:
            record = self.diagnostic_run.start_intervention(
                self.stream_id,
                elapsed_seconds=elapsed,
                intervention_type=intervention_type,
                location=location,
            )
        except Exception as exc:
            self._latch_intervention_recorder_fault(f"{type(exc).__name__}: {exc}")
            return
        number = len(self.interventions) + 1
        self.active_intervention = {
            "number": number,
            "intervention_id": record["intervention_id"],
            "start_elapsed_seconds": float(record["elapsed_seconds"]),
            "intervention_type": record["intervention_type"],
            "location": record["location"],
        }
        self.mark_intervention_button.configure(
            text="Mark Intervention: End",
            state="normal",
        )
        self.intervention_type_combo.configure(state="disabled")
        self.intervention_location_entry.configure(state="disabled")
        self._draw_plot()
        self.status_var.set(
            f"Intervention {number} started at {record['elapsed_seconds']:.3f} s · "
            f"{record['intervention_type']} @ {record['location']} · "
            "interventions.jsonl was flushed. No instrument message was sent."
        )

    def _pause_stream(self) -> None:
        self.intervention_ready = False
        self.mark_intervention_button.configure(state="disabled")
        self._end_active_intervention(show_status=False)
        self.live_running = False
        recorder_error = None
        if self.diagnostic_run is not None:
            try:
                self.diagnostic_run.record_event(
                    "pause_requested",
                    reason_code="PAUSE_REQUESTED",
                    stream_id=self.stream_id,
                )
            except Exception as exc:
                recorder_error = f"{type(exc).__name__}: {exc}"
                self.recording_fault_latched = True
                self.stream_had_error = True
                self.stream_stop_fault = recorder_error
                try:
                    if self.diagnostic_run.state in {
                        DiagnosticState.LIVE,
                        DiagnosticState.DEGRADED,
                    }:
                        self.diagnostic_run.transition(
                            DiagnosticState.FAULT_LATCHED,
                            reason_code="PAUSE_EVENT_RECORDING_FAILED",
                            severity="ERROR",
                        )
                except Exception:
                    pass
        self.stop_event.set()
        self.pause_button.configure(state="disabled")
        self.mark_intervention_button.configure(state="disabled")
        self.status_var.set(
            "Stopping after the current VISA query completes..."
            if recorder_error is None
            else f"Stopping with recorder fault latched: {recorder_error}"
        )

    def _single_fetch(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        if (
            not self.configuration
            or not self.diagnostic_run
            or self.diagnostic_run.state != DiagnosticState.OBSERVE_READY
        ):
            self.messagebox.showwarning("Diagnostics required", "Rerun diagnostics before Single FETCh?.")
            return
        if not self._confirm_real_access():
            return
        try:
            self.diagnostic_run.record_event(
                "single_fetch_requested",
                reason_code="SINGLE_FETCH_REQUESTED",
            )
        except Exception as exc:
            self.recording_fault_latched = True
            try:
                if self.diagnostic_run.state == DiagnosticState.OBSERVE_READY:
                    self.diagnostic_run.transition(
                        DiagnosticState.FAULT_LATCHED,
                        reason_code="SINGLE_FETCH_EVENT_RECORDING_FAILED",
                        severity="ERROR",
                    )
            except Exception:
                pass
            self.start_button.configure(state="disabled")
            self.single_button.configure(state="disabled")
            self.diagnostic_state_var.set(
                f"State: {self.diagnostic_run.state.value} · RECORDER FAULT LATCHED"
            )
            self.messagebox.showerror(
                "Recorder failure",
                f"Single FETCh? blocked because evidence logging failed: {exc}",
            )
            return
        self._set_busy(True)
        mode = self.mode_var.get()
        single_context = None
        if mode == "simulate":
            scenario = (
                self.simulation_context.scenario_id
                if self.simulation_context is not None
                else self.selected_fault
            )
            single_context = SimulationContext(scenario, ALLOWED_QUERIES)
        self.worker = threading.Thread(
            target=self._single_fetch_worker,
            args=(mode, single_context, self.diagnostic_run),
            daemon=True,
        )
        self.worker.start()

    def _single_fetch_worker(
        self,
        mode: str,
        simulation_context: SimulationContext | None = None,
        journal: RunJournal | None = None,
    ) -> None:
        try:
            if mode == "real" and simulation_context is not None:
                raise ValueError("fault injection is forbidden in real mode")
            started = time.perf_counter()
            with session_factory(mode, "stream", simulation_context) as session:
                identity = session.query("*IDN?")
                if not identity_is_expected(identity):
                    raise RuntimeError(f"Identity mismatch: {identity!r}")
                raw = session.query(FETCH_QUERY)
            query_ms = round((time.perf_counter() - started) * 1000, 3)
            voltage = parse_voltage(raw)
            elapsed = self.samples[-1][0] if self.samples else 0.0
            if journal is not None:
                journal.record_event(
                    "single_fetch_completed",
                    reason_code="SINGLE_FETCH_COMPLETED",
                    payload={
                        "query_elapsed_ms": query_ms,
                        "fault_scenario": (
                            simulation_context.scenario_id
                            if simulation_context is not None
                            else "nominal"
                        ),
                        "consumed_rule_ids": (
                            list(simulation_context.consumed_rule_ids)
                            if simulation_context is not None
                            else []
                        ),
                        "query_history": (
                            list(simulation_context.query_history)
                            if simulation_context is not None
                            else []
                        ),
                    },
                )
            self.events.put(("single", (elapsed, voltage, raw, query_ms)))
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            if journal is not None:
                try:
                    journal.record_error(
                        reason_code="SINGLE_FETCH_FAILED",
                        message=message,
                        payload={
                            "fault_scenario": (
                                simulation_context.scenario_id
                                if simulation_context is not None
                                else "nominal"
                            ),
                            "consumed_rule_ids": (
                                list(simulation_context.consumed_rule_ids)
                                if simulation_context is not None
                                else []
                            ),
                            "query_history": (
                                list(simulation_context.query_history)
                                if simulation_context is not None
                                else []
                            ),
                        },
                    )
                except Exception:
                    pass
                try:
                    if journal.state == DiagnosticState.OBSERVE_READY:
                        journal.transition(
                            DiagnosticState.FAULT_LATCHED,
                            reason_code="SINGLE_FETCH_FAULT_LATCHED",
                            severity="ERROR",
                        )
                except Exception:
                    pass
            self.events.put(("single_error", message))

    def _clear_plot(self) -> None:
        self.samples.clear()
        self.interventions.clear()
        self.active_intervention = None
        self.reading_var.set("No sample")
        self.raw_var.set("Raw: —")
        self._draw_plot()
        self.status_var.set(
            "Plot memory cleared. Existing CSV and interventions.jsonl evidence were not deleted."
        )

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        self.config_button.configure(state=state)
        self.mode_combo.configure(state="disabled" if busy else "readonly")
        self.fault_combo.configure(
            state=(
                "disabled"
                if busy or self.mode_var.get() == "real"
                else "readonly"
            )
        )
        self.output_button.configure(state="disabled" if busy else "normal")
        if busy:
            self.start_button.configure(state="disabled")
            self.single_button.configure(state="disabled")
            self.mark_intervention_button.configure(state="disabled")
        else:
            ready = live_start_is_safe(
                self.configuration,
                self.diagnostic_run.state if self.diagnostic_run else None,
                recording_fault_latched=self.recording_fault_latched,
                stream_had_error=self.stream_had_error,
            )
            self.start_button.configure(state="normal" if ready else "disabled")
            self.single_button.configure(state="normal" if ready else "disabled")

    def _show_configuration(self, report: dict[str, object]) -> None:
        self.config_tree.delete(*self.config_tree.get_children())
        transcript = report.get("transcript", [])
        for item in transcript if isinstance(transcript, list) else []:
            if not isinstance(item, dict):
                continue
            response = item.get("response")
            response_present = response is not None and bool(str(response).strip())
            value = (
                response
                if item.get("ok") and response_present
                else item.get("error", "<empty response>")
            )
            status = "PASS" if item.get("ok") and response_present else "BLOCKED"
            self.config_tree.insert(
                "",
                "end",
                values=(status, item.get("name", ""), value, item.get("command", "")),
                tags=(status,),
            )
        diagnostics = report.get("diagnostics", {})
        checks = diagnostics.get("checks", []) if isinstance(diagnostics, dict) else []
        for check in checks if isinstance(checks, list) else []:
            if not isinstance(check, dict):
                continue
            status = str(check.get("status", "UNKNOWN"))
            observed = check.get("observed")
            expected = check.get("expected")
            self.config_tree.insert(
                "",
                "end",
                values=(
                    status,
                    check.get("check_id", ""),
                    json.dumps(observed, ensure_ascii=False),
                    json.dumps(expected, ensure_ascii=False),
                ),
                tags=(status,),
            )

    def _drain_events(self) -> None:
        try:
            while True:
                event, payload = self.events.get_nowait()
                if event == "configuration":
                    (
                        report,
                        run_directory,
                        evidence_file,
                        journal,
                        simulation_context,
                    ) = payload
                    self.configuration = report
                    self.run_directory = run_directory
                    self.diagnostic_run = journal
                    self.simulation_context = simulation_context
                    self.poll_interval_s = float(report["derived_poll_interval_s"])
                    self._show_configuration(report)
                    diagnostics = report.get("diagnostics", {})
                    ready = bool(
                        isinstance(diagnostics, dict)
                        and diagnostics.get("can_start_live")
                        and journal.state == DiagnosticState.OBSERVE_READY
                    )
                    self._set_busy(False)
                    self.start_button.configure(state="normal" if ready else "disabled")
                    self.single_button.configure(state="normal" if ready else "disabled")
                    summary = diagnostics.get("summary", {}) if isinstance(diagnostics, dict) else {}
                    self.diagnostic_state_var.set(f"State: {journal.state.value}")
                    self.readiness_var.set(
                        "Readiness: "
                        f"{diagnostics.get('overall', 'UNKNOWN')} · "
                        f"PASS {summary.get('pass', 0)} · WARN {summary.get('warn', 0)} · "
                        f"BLOCKED {summary.get('blocked', 0)} · UNKNOWN {summary.get('unknown', 0)}"
                    )
                    self.status_var.set(
                        f"Diagnostics complete: {len(report['values'])}/{len(CONFIG_QUERIES)} responses; "
                        f"poll interval {self.poll_interval_s:.3f} s; "
                        f"live {'enabled' if ready else 'blocked'}."
                    )
                    self.evidence_var.set(
                        f"Manifest: {journal.manifest_path} · Snapshot: {evidence_file} · "
                        f"Interventions: {journal.interventions_path}"
                    )
                    self.mode_status_var.set(
                        "Real query-only VISA access confirmed." if report["mode"] == "real"
                        else (
                            "Simulation diagnostic loaded; no VISA communication · fault scenario: "
                            f"{report.get('fault_injection', {}).get('scenario', 'nominal')}"
                        )
                    )
                elif event == "configuration_error":
                    message, run_directory, failure_file, journal, simulation_context = payload
                    self.recording_fault_latched = True
                    self.configuration = None
                    self.run_directory = run_directory
                    self.diagnostic_run = journal
                    self.simulation_context = simulation_context
                    self.config_tree.delete(*self.config_tree.get_children())
                    self._set_busy(False)
                    self.start_button.configure(state="disabled")
                    self.single_button.configure(state="disabled")
                    committed_state = journal.state.value if journal is not None else "DISCONNECTED"
                    self.diagnostic_state_var.set(
                        f"State: FAULT_LATCHED (last committed: {committed_state})"
                    )
                    self.readiness_var.set("Readiness: BLOCKED · diagnostic evidence recorder failed")
                    self.status_var.set(f"Diagnostic failure: {message}")
                    if journal is not None:
                        self.evidence_var.set(
                            f"Partial manifest: {journal.manifest_path} · failure: {failure_file or 'not written'}"
                        )
                    elif run_directory is not None:
                        self.evidence_var.set(f"Partial diagnostic directory: {run_directory}")
                    if not self.closing:
                        self.messagebox.showerror("GPIB6 diagnostic failed", str(message))
                elif event == "sample":
                    elapsed, voltage, raw, count = payload
                    self.samples.append((elapsed, voltage))
                    if self.live_running and not self.stop_event.is_set():
                        self.intervention_ready = True
                        self.mark_intervention_button.configure(state="normal")
                    self.reading_var.set(format_voltage(voltage))
                    self.raw_var.set(f"Raw: {raw}")
                    if self.diagnostic_run is not None:
                        self.diagnostic_state_var.set(
                            f"State: {self.diagnostic_run.state.value}"
                        )
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
                    if self.diagnostic_run is not None:
                        self.diagnostic_state_var.set(
                            f"State: {self.diagnostic_run.state.value}"
                        )
                elif event == "single_error":
                    self._set_busy(False)
                    self.start_button.configure(state="disabled")
                    self.single_button.configure(state="disabled")
                    if self.diagnostic_run is not None:
                        self.diagnostic_state_var.set(
                            f"State: {self.diagnostic_run.state.value}"
                        )
                    self.status_var.set(f"Single FETCh? failed: {payload}")
                    if not self.closing:
                        self.messagebox.showerror("Single FETCh? failed", str(payload))
                elif event == "error":
                    self._set_busy(False)
                    self.status_var.set(f"Error: {payload}")
                    if not self.closing:
                        self.messagebox.showerror("GPIB6 monitor error", str(payload))
                elif event == "stream_error":
                    if isinstance(payload, dict):
                        message = str(payload.get("message", "unknown stream error"))
                        intervention_end = payload.get("intervention_end")
                    else:
                        message = str(payload)
                        intervention_end = None
                    self.intervention_ready = False
                    self.mark_intervention_button.configure(state="disabled")
                    if isinstance(intervention_end, dict):
                        self._apply_committed_intervention_end(
                            intervention_end,
                            show_status=False,
                        )
                    self.live_running = False
                    self.stream_had_error = True
                    self.recording_fault_latched = True
                    self.mark_intervention_button.configure(state="disabled")
                    if self.diagnostic_run is not None:
                        self.diagnostic_state_var.set(
                            f"State: {self.diagnostic_run.state.value}"
                        )
                    self.status_var.set(f"Live stream error: {message}")
                    if not self.closing:
                        self.messagebox.showerror("Live stream stopped", message)
                elif event == "stream_stopped":
                    self.intervention_ready = False
                    self.mark_intervention_button.configure(state="disabled")
                    self.live_running = False
                    self.pause_button.configure(state="disabled")
                    self.mark_intervention_button.configure(state="disabled")
                    self.clear_button.configure(state="normal")
                    self._set_busy(False)
                    count = int(payload.get("sample_count", 0))
                    error = payload.get("error")
                    self.stream_id = None
                    if self.diagnostic_run is not None:
                        self.diagnostic_state_var.set(
                            f"State: {self.diagnostic_run.state.value}"
                        )
                        self.evidence_var.set(
                            f"Manifest: {self.diagnostic_run.manifest_path} · Events: "
                            f"{self.diagnostic_run.events_path} · Interventions: "
                            f"{self.diagnostic_run.interventions_path}"
                        )
                    if error is None and not self.stream_had_error:
                        self.status_var.set(
                            f"Paused safely after {count} samples. VISA session and CSV are closed."
                        )
        except queue.Empty:
            pass
        if not self.closing:
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

        plot_interventions = [dict(item) for item in self.interventions]
        if self.active_intervention is not None:
            active = dict(self.active_intervention)
            latest_sample_elapsed = self.samples[-1][0] if self.samples else float(
                active["start_elapsed_seconds"]
            )
            active["end_elapsed_seconds"] = max(
                float(active["start_elapsed_seconds"]),
                latest_sample_elapsed,
            )
            plot_interventions.append(active)
        visible, visible_interventions = visible_plot_data(
            self.samples,
            plot_interventions,
        )
        if not visible:
            canvas.create_text(width / 2, height / 2, text="No voltage samples", fill="#687481")
            canvas.create_text(width / 2, height - 15, text="Elapsed host time (s)", fill="#33404d")
            canvas.create_text(18, height / 2, text="Voltage", angle=90, fill="#33404d")
            return

        xs = [point[0] for point in visible]
        ys = [point[1] for point in visible]
        x_values = list(xs)
        for interval in visible_interventions:
            x_values.extend(
                (
                    float(interval["start_elapsed_seconds"]),
                    float(interval["end_elapsed_seconds"]),
                )
            )
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

        for interval in visible_interventions:
            start_elapsed = float(interval["start_elapsed_seconds"])
            end_elapsed = float(interval["end_elapsed_seconds"])
            start_x = left + (start_elapsed - x_min) / (x_max - x_min) * plot_w
            end_x = left + (end_elapsed - x_min) / (x_max - x_min) * plot_w
            canvas.create_rectangle(
                start_x,
                top,
                end_x,
                top + plot_h,
                fill="#ffd9dd",
                stipple="gray50",
                outline="",
            )
            canvas.create_line(start_x, top, start_x, top + plot_h, fill="#d62728", width=2)
            canvas.create_line(end_x, top, end_x, top + plot_h, fill="#d62728", width=2)
            canvas.create_text(
                start_x + 4,
                top + 4,
                text=(
                    f"Intervention {interval['number']} · "
                    f"{interval['intervention_type']} @ {interval['location']}"
                ),
                anchor="nw",
                fill="#b01f2e",
            )

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
        canvas.create_text(width / 2, height - 12, text="Elapsed host time (s)", fill="#33404d")
        canvas.create_text(15, height / 2, text="Voltage (V)", angle=90, fill="#33404d")

    def _close(self) -> None:
        if self.closing:
            return
        self.closing = True
        self.intervention_ready = False
        self.mark_intervention_button.configure(state="disabled")
        self._end_active_intervention(show_status=False)
        self.live_running = False
        self.stop_event.set()
        self.config_button.configure(state="disabled")
        self.start_button.configure(state="disabled")
        self.pause_button.configure(state="disabled")
        self.mark_intervention_button.configure(state="disabled")
        self.intervention_type_combo.configure(state="disabled")
        self.intervention_location_entry.configure(state="disabled")
        self.single_button.configure(state="disabled")
        self.mode_combo.configure(state="disabled")
        self.fault_combo.configure(state="disabled")
        self.output_button.configure(state="disabled")
        self.status_var.set("Closing after the current query and evidence flush complete...")
        if self.diagnostic_run is not None:
            try:
                self.diagnostic_run.record_event(
                    "window_close_requested",
                    reason_code="WINDOW_CLOSE_REQUESTED",
                )
            except Exception:
                pass
        self.root.after(50, self._wait_for_close)

    def _wait_for_close(self) -> None:
        if self.worker and self.worker.is_alive():
            self.root.after(100, self._wait_for_close)
            return
        self._drain_events()
        self._finalize_diagnostic_run("window_closed")
        self.root.destroy()


def main() -> int:
    import tkinter as tk

    root = tk.Tk()
    MonitorApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
