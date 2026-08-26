"""English Tk GUI for the manual-refresh 6221/2182A pair observer."""

from __future__ import annotations

import queue
import threading
from pathlib import Path

from .pair_observer import observe_pair


NANOVOLTMETER_CHOICES = {
    "2182A | GPIB6 | S/N 1340129 | FW C02/A02": "GPIB0::6::INSTR",
    "2182A | GPIB7 | S/N 4510267 | FW C08/B01": "GPIB0::7::INSTR",
}

CURRENT_SOURCE_CHOICES = {
    "6221 | GPIB9 | S/N 4533811 | FW D04/700x": "GPIB0::9::INSTR",
    "6221 | GPIB10 | S/N 4581062 | FW D04/700x": "GPIB0::10::INSTR",
}


def _instrument_value(report: dict[str, object], name: str) -> str:
    instrument = report.get("instrument")
    if not isinstance(instrument, dict):
        return "—"
    item = instrument.get(name)
    value = item.get("value") if isinstance(item, dict) else item
    return "—" if value is None else str(value)


def _format_number(value: object, suffix: str = "") -> str:
    if value is None:
        return "Not calculated"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{number:.12g}{suffix}"


class PairObserverApp:
    def __init__(self, root) -> None:
        import tkinter as tk
        from tkinter import filedialog, ttk

        self.tk = tk
        self.ttk = ttk
        self.filedialog = filedialog
        self.root = root
        self.root.title("Keithley 6221 / 2182A Manual Pair Observer")
        self.root.geometry("1260x820")
        self.root.minsize(1040, 700)
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.output_root = Path.cwd() / "pair_runs"
        self.last_result: dict[str, object] | None = None

        self._configure_style()
        self._build_ui()
        # This timer only transfers completed worker results to Tk.  It never
        # calls VISA and is not an instrument auto-refresh loop.
        self.root.after(100, self._drain_events)

    def _configure_style(self) -> None:
        style = self.ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("PairTitle.TLabel", font=("Segoe UI", 18, "bold"))
        style.configure("PairSafe.TLabel", foreground="#176b3a", font=("Segoe UI", 10, "bold"))
        style.configure("PairWarning.TLabel", foreground="#8a5a00", font=("Segoe UI", 9, "bold"))
        style.configure("PairAction.TButton", font=("Segoe UI", 10, "bold"), padding=(12, 8))
        style.configure("PairValue.TLabel", font=("Segoe UI", 10, "bold"))
        style.configure("Treeview", rowheight=28, font=("Segoe UI", 9))

    def _build_ui(self) -> None:
        tk, ttk = self.tk, self.ttk
        outer = ttk.Frame(self.root, padding=16)
        outer.pack(fill="both", expand=True)

        ttk.Label(
            outer,
            text="Keithley 6221 / 2182A Manual Pair Observer",
            style="PairTitle.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            outer,
            text="Choose any confirmed 6221 and any confirmed 2182A. The software does not infer physical wiring.",
        ).pack(anchor="w", pady=(2, 0))
        ttk.Label(
            outer,
            text="QUERY ONLY · BUTTON-TRIGGERED SNAPSHOTS · NO AUTOMATIC REFRESH · NO OUTPUT OR TRIGGER CONTROL",
            style="PairSafe.TLabel",
        ).pack(anchor="w", pady=(7, 2))
        ttk.Label(
            outer,
            text=(
                "The operator performs wiring and every front-panel action. "
                "Close LabVIEW and NI MAX test panels before real VISA access."
            ),
            style="PairWarning.TLabel",
        ).pack(anchor="w", pady=(0, 10))

        selection = ttk.LabelFrame(outer, text="1. Pair and mode", padding=10)
        selection.pack(fill="x")
        selection.columnconfigure(1, weight=1)
        selection.columnconfigure(3, weight=1)

        ttk.Label(selection, text="6221 current source:").grid(row=0, column=0, sticky="w")
        self.source_var = tk.StringVar(value=next(iter(CURRENT_SOURCE_CHOICES)))
        self.source_combo = ttk.Combobox(
            selection,
            state="readonly",
            textvariable=self.source_var,
            values=tuple(CURRENT_SOURCE_CHOICES),
            width=48,
        )
        self.source_combo.grid(row=0, column=1, sticky="ew", padx=(6, 16))

        ttk.Label(selection, text="2182A nanovoltmeter:").grid(row=0, column=2, sticky="w")
        self.meter_var = tk.StringVar(value=next(iter(NANOVOLTMETER_CHOICES)))
        self.meter_combo = ttk.Combobox(
            selection,
            state="readonly",
            textvariable=self.meter_var,
            values=tuple(NANOVOLTMETER_CHOICES),
            width=48,
        )
        self.meter_combo.grid(row=0, column=3, sticky="ew", padx=(6, 0))

        ttk.Label(selection, text="Mode:").grid(row=1, column=0, sticky="w", pady=(9, 0))
        self.mode_var = tk.StringVar(value="simulate")
        self.mode_combo = ttk.Combobox(
            selection,
            state="readonly",
            textvariable=self.mode_var,
            values=("simulate", "real"),
            width=16,
        )
        self.mode_combo.grid(row=1, column=1, sticky="w", padx=(6, 16), pady=(9, 0))

        ttk.Button(selection, text="Change Evidence Folder", command=self._choose_output).grid(
            row=1, column=2, sticky="w", pady=(9, 0)
        )
        self.output_label = ttk.Label(selection, text=str(self.output_root))
        self.output_label.grid(row=1, column=3, sticky="ew", padx=(6, 0), pady=(9, 0))

        metadata = ttk.LabelFrame(
            outer,
            text="2. Optional local resistor metadata (saved to JSON; never sent to an instrument)",
            padding=10,
        )
        metadata.pack(fill="x", pady=(10, 0))
        for column in (1, 3, 5):
            metadata.columnconfigure(column, weight=1)

        self.resistor_id_var = tk.StringVar()
        self.nominal_var = tk.StringVar()
        self.tolerance_var = tk.StringVar()
        self.notes_var = tk.StringVar()
        fields = (
            ("Resistor ID:", self.resistor_id_var, 0, 0),
            ("Nominal resistance (ohms):", self.nominal_var, 0, 2),
            ("Tolerance (%):", self.tolerance_var, 0, 4),
            ("Notes:", self.notes_var, 1, 0),
        )
        for label, variable, row, column in fields:
            ttk.Label(metadata, text=label).grid(row=row, column=column, sticky="w", pady=(6 if row else 0, 0))
            span = 5 if label == "Notes:" else 1
            ttk.Entry(metadata, textvariable=variable).grid(
                row=row,
                column=column + 1,
                columnspan=span,
                sticky="ew",
                padx=(5, 14 if span == 1 else 0),
                pady=(6 if row else 0, 0),
            )

        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=(12, 8))
        self.config_button = ttk.Button(
            actions,
            text="Read Pair Configuration",
            style="PairAction.TButton",
            command=lambda: self._start("configuration"),
        )
        self.config_button.pack(side="left")
        self.measurement_button = ttk.Button(
            actions,
            text="Read Latest Measurement Snapshot",
            style="PairAction.TButton",
            command=lambda: self._start("measurement"),
        )
        self.measurement_button.pack(side="left", padx=(8, 0))
        ttk.Label(
            actions,
            text="Each click creates a new evidence directory. Nothing is overwritten.",
        ).pack(side="left", padx=(14, 0))

        summary = ttk.LabelFrame(outer, text="Latest summary", padding=10)
        summary.pack(fill="x")
        summary.columnconfigure(1, weight=1)
        summary.columnconfigure(3, weight=1)
        self.summary_vars = {
            "output": tk.StringVar(value="—"),
            "current": tk.StringVar(value="—"),
            "compliance": tk.StringVar(value="—"),
            "interlock": tk.StringVar(value="—"),
            "voltage": tk.StringVar(value="—"),
            "resistance": tk.StringVar(value="—"),
        }
        summary_fields = (
            ("6221 output", "output", 0, 0),
            ("Programmed current", "current", 0, 2),
            ("Voltage compliance", "compliance", 1, 0),
            ("6221 interlock", "interlock", 1, 2),
            ("2182A cached voltage", "voltage", 2, 0),
            ("Local V/I estimate", "resistance", 2, 2),
        )
        for label, key, row, column in summary_fields:
            ttk.Label(summary, text=label + ":").grid(row=row, column=column, sticky="w", pady=2)
            ttk.Label(summary, textvariable=self.summary_vars[key], style="PairValue.TLabel").grid(
                row=row, column=column + 1, sticky="w", padx=(7, 22), pady=2
            )

        detail_box = ttk.LabelFrame(outer, text="Complete selected-pair evidence", padding=8)
        detail_box.pack(fill="both", expand=True, pady=(10, 6))
        self.detail = tk.Text(
            detail_box,
            wrap="none",
            relief="flat",
            background="#f7f8fa",
            font=("Consolas", 9),
            padx=8,
            pady=8,
        )
        self.detail.pack(side="left", fill="both", expand=True)
        y_scroll = ttk.Scrollbar(detail_box, orient="vertical", command=self.detail.yview)
        y_scroll.pack(side="right", fill="y")
        self.detail.configure(yscrollcommand=y_scroll.set, state="disabled")

        self.status_var = tk.StringVar(
            value="Ready. Simulation is the default. No instrument message is sent until a button is clicked."
        )
        ttk.Label(outer, textvariable=self.status_var).pack(anchor="w")

    def _choose_output(self) -> None:
        selected = self.filedialog.askdirectory(initialdir=str(self.output_root.parent))
        if selected:
            self.output_root = Path(selected)
            self.output_label.configure(text=str(self.output_root))

    def _metadata(self) -> dict[str, str]:
        return {
            "resistor_id": self.resistor_id_var.get(),
            "nominal_resistance_ohm": self.nominal_var.get(),
            "tolerance_percent": self.tolerance_var.get(),
            "notes": self.notes_var.get(),
        }

    def _start(self, operation: str) -> None:
        if self.config_button.instate(["disabled"]):
            return
        self._set_busy(True)
        self.status_var.set(
            f"Running one {self.mode_var.get()} query-only {operation} snapshot. No automatic refresh..."
        )
        self._set_detail("Snapshot in progress...")
        arguments = {
            "nanovoltmeter_resource": NANOVOLTMETER_CHOICES[self.meter_var.get()],
            "current_source_resource": CURRENT_SOURCE_CHOICES[self.source_var.get()],
            "operation": operation,
            "mode": self.mode_var.get(),
            "local_metadata": self._metadata(),
        }
        threading.Thread(target=self._worker, args=(arguments,), daemon=True).start()

    def _worker(self, arguments: dict[str, object]) -> None:
        try:
            result = observe_pair(self.output_root, **arguments)
            self.events.put(("done", result))
        except Exception as exc:
            self.events.put(("error", f"{type(exc).__name__}: {exc}"))

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "done":
                    self._show_result(payload)
                else:
                    self._show_error(str(payload))
        except queue.Empty:
            pass
        self.root.after(100, self._drain_events)

    def _set_busy(self, busy: bool) -> None:
        button_state = ["disabled"] if busy else ["!disabled"]
        self.config_button.state(button_state)
        self.measurement_button.state(button_state)
        self.source_combo.configure(state="disabled" if busy else "readonly")
        self.meter_combo.configure(state="disabled" if busy else "readonly")
        self.mode_combo.configure(state="disabled" if busy else "readonly")

    def _show_result(self, result: dict[str, object]) -> None:
        self._set_busy(False)
        self.last_result = result
        summary = result["summary"]
        source = summary["6221"]
        meter = summary["2182a"]
        calculation = summary["calculation"]
        self.summary_vars["output"].set(str(source["output"]))
        self.summary_vars["current"].set(f'{source["programmed_current_a"] or "—"} A')
        self.summary_vars["compliance"].set(f'{source["voltage_compliance_v"] or "—"} V')
        self.summary_vars["interlock"].set(str(source["interlock"]))
        self.summary_vars["voltage"].set(f'{meter["latest_cached_voltage_v"] or "—"} V')
        self.summary_vars["resistance"].set(_format_number(calculation["resistance_ohm"], " ohm"))
        self._set_detail(self._format_detail(result))
        self.status_var.set(
            f'{str(result["operation"]).capitalize()} complete; status: {summary["status"]}. '
            f'Evidence: {result["evidence_file"]}'
        )

    def _show_error(self, message: str) -> None:
        from tkinter import messagebox

        self._set_busy(False)
        self.status_var.set(f"Snapshot did not start or stopped: {message}")
        self._set_detail(message)
        messagebox.showerror("Pair Snapshot Not Completed", message)

    def _format_detail(self, result: dict[str, object]) -> str:
        summary = result["summary"]
        calculation = summary["calculation"]
        lines = [
            f'Operation: {result["operation"]}',
            f'Mode: {result["mode"]}',
            f'6221: {result["pair"]["6221_resource"]}',
            f'2182A: {result["pair"]["2182a_resource"]}',
            f'Evidence: {result["evidence_file"]}',
            "",
            "Safety:",
            "  Query only: yes",
            "  Automatic polling: no",
            "  Output/trigger/configuration controls: none",
            "",
            "Calculation:",
            f'  Resistance: {_format_number(calculation["resistance_ohm"], " ohm")}',
            f'  Absolute error: {_format_number(calculation["absolute_error_ohm"], " ohm")}',
            f'  Relative error: {_format_number(calculation["relative_error_percent"], " %")}',
            f'  Interpretation: {calculation["status"]}',
            "",
        ]
        warnings = summary.get("warnings", [])
        if warnings:
            lines.append("Notices:")
            lines.extend(f"  - {item}" for item in warnings)
            lines.append("")

        instruments = result.get("instruments", {})
        for key, title in (("6221", "6221 parameters"), ("2182a", "2182A parameters")):
            lines.append(title + ":")
            report = instruments.get(key, {}) if isinstance(instruments, dict) else {}
            instrument = report.get("instrument", {}) if isinstance(report, dict) else {}
            if isinstance(instrument, dict) and instrument:
                for name, item in instrument.items():
                    value = item.get("value") if isinstance(item, dict) else item
                    lines.append(f"  {name:<30} {value}")
            else:
                lines.append("  No parameter values were returned.")
            transcript = report.get("transcript", []) if isinstance(report, dict) else []
            failed = [
                item for item in transcript
                if isinstance(item, dict) and not item.get("ok")
            ]
            if failed:
                lines.append("  Communication error:")
                for item in failed:
                    lines.append(f'    {item.get("name", "query")}: {item.get("error", "unknown error")}')
            lines.append("")
        return "\n".join(lines)

    def _set_detail(self, text: str) -> None:
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", text)
        self.detail.configure(state="disabled")


def main() -> None:
    try:
        import tkinter as tk
    except ImportError as exc:
        raise SystemExit("Pair GUI requires Python with Tkinter support.") from exc

    root = tk.Tk()
    PairObserverApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
