"""Tk GUI for one-click, query-only scans of the confirmed laboratory map."""

from __future__ import annotations

import json
import queue
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import __version__
from .catalog import PROFILES
from .collector import collect, host_environment
from .lab_setup import LAB_INSTRUMENTS
from .production import production_host_readiness, require_production_host
from .transports import PyVisaQueryTransport, SimulatedTransport


PROFILE_FOR_ASSIGNMENT = {
    "2182a-gpib6": "2182a",
    "2182a-gpib7": "2182a",
    "6221-gpib9": "6221",
    "6221-gpib10": "6221",
    "2450-gpib25": "2450-tsp",
    "2450-gpib26": "2450-tsp",
}


def _instrument_value(report: dict[str, object], name: str) -> str | None:
    instrument = report.get("instrument", {})
    if not isinstance(instrument, dict):
        return None
    item = instrument.get(name)
    if isinstance(item, dict):
        value = item.get("value")
        return None if value is None else str(value)
    return None if item is None else str(item)


def _identity_parts(identity: str | None) -> tuple[str, str, str]:
    if not identity:
        return "—", "—", "—"
    fields = [part.strip() for part in identity.split(",")]
    model = fields[1].removeprefix("MODEL ").strip() if len(fields) > 1 else "—"
    serial = fields[2] if len(fields) > 2 else "—"
    firmware = fields[3] if len(fields) > 3 else "—"
    return model, serial, firmware


def summarize_report(report: dict[str, object]) -> dict[str, object]:
    """Create display fields and explicit safety observations from one report."""
    identity = _instrument_value(report, "identity")
    model, serial, firmware = _identity_parts(identity)
    profile = str(report.get("profile", ""))
    warnings: list[str] = []

    if profile == "6221":
        output_raw = _instrument_value(report, "output_enabled")
        output = "OFF" if output_raw == "0" else "ON" if output_raw == "1" else output_raw or "—"
        interlock_raw = _instrument_value(report, "interlock_closed")
        # Keithley 6221 defines 1 as closed and 0 as tripped/open for this query.
        interlock = "Closed" if interlock_raw == "1" else "Open / tripped" if interlock_raw == "0" else interlock_raw or "—"
        if output_raw == "1":
            warnings.append("6221 output is currently ON")
        if interlock_raw == "0":
            warnings.append("6221 interlock is open / tripped")
    elif profile == "2450-tsp":
        output_raw = _instrument_value(report, "source_output")
        output = "OFF" if output_raw in {"0", "smu.OFF"} else "ON" if output_raw in {"1", "smu.ON"} else output_raw or "—"
        interlock_raw = _instrument_value(report, "interlock")
        interlock = "Normal (not tripped)" if interlock_raw in {"0", "smu.OFF"} else "Tripped" if interlock_raw in {"1", "smu.ON"} else interlock_raw or "—"
        if output in {"ON", "smu.ON", "1"}:
            warnings.append("2450 source output is currently ON")
        if interlock_raw in {"1", "smu.ON"}:
            warnings.append("2450 interlock is tripped")
    else:
        output = "N/A (voltmeter)"
        interlock = "N/A"

    safety = report.get("safety", {})
    transcript = report.get("transcript", [])
    failed = [row for row in transcript if isinstance(row, dict) and not row.get("ok")]
    if isinstance(safety, dict):
        if safety.get("stopped_after_first_io_error"):
            warnings.append("An I/O error occurred; remaining queries for this instrument were stopped")
        if safety.get("stopped_after_identity_mismatch"):
            warnings.append("Identity mismatch; remaining queries for this instrument were stopped")

    return {
        "resource": report.get("visa_resource") or "—",
        "profile": profile,
        "model": model,
        "serial": serial,
        "firmware": firmware,
        "output": output,
        "interlock": interlock,
        "line_frequency_hz": _instrument_value(report, "line_frequency_hz") or "—",
        "queries_ok": len(transcript) - len(failed),
        "queries_total": len(transcript),
        "warnings": warnings,
        "status": "warning" if warnings else "passed",
    }


def _write_new_json(path: Path, data: object) -> None:
    rendered = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    with path.open("x", encoding="utf-8") as stream:
        stream.write(rendered)


def _new_run_directory(output_root: Path, mode: str) -> Path:
    output_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    run_dir = output_root / f"{stamp}-{'real' if mode == 'real' else 'simulation'}-gui-core"
    run_dir.mkdir(exist_ok=False)
    return run_dir


def scan_confirmed_lab(
    output_root: Path,
    *,
    mode: str = "real",
    timeout_ms: int = 3000,
    progress: Callable[[str, str, dict[str, object] | None], None] | None = None,
    real_transport_factory=PyVisaQueryTransport,
    host_gate=require_production_host,
    readiness_provider=production_host_readiness,
) -> dict[str, object]:
    """Scan exactly the six confirmed addresses with the proven core allowlists."""
    if mode not in {"real", "simulate"}:
        raise ValueError(f"unsupported GUI scan mode: {mode!r}")
    if mode == "real":
        # The gate runs before a VISA resource is opened or messaged.
        host_gate()

    run_dir = _new_run_directory(Path(output_root), mode)
    started = datetime.now(timezone.utc)
    rows: list[dict[str, object]] = []

    for assignment in LAB_INSTRUMENTS:
        profile_key = PROFILE_FOR_ASSIGNMENT[assignment.key]
        profile = PROFILES[profile_key]
        if progress:
            progress(assignment.key, "scanning", None)

        transport = None
        try:
            if mode == "real":
                transport = real_transport_factory(
                    profile,
                    assignment.resource,
                    timeout_ms=timeout_ms,
                )
            else:
                transport = SimulatedTransport(profile)
                transport.resource_name = assignment.resource

            report = collect(profile, transport, "core")
            report["production_readiness"] = (
                readiness_provider() if mode == "real" else {"host_gate_passed": None, "blockers": []}
            )
            report["confirmed_lab_assignment"] = assignment.as_dict()
            report["gui_scan_mode"] = mode
            summary = summarize_report(report)
            unsafe_stop = bool(
                report["safety"]["stopped_after_first_io_error"]
                or report["safety"]["stopped_after_identity_mismatch"]
            )
            if unsafe_stop:
                summary["status"] = "error"
            evidence_name = f"{assignment.key}-core.json"
            _write_new_json(run_dir / evidence_name, report)
            row = {
                "assignment": assignment.as_dict(),
                "profile": profile_key,
                "ok": not unsafe_stop,
                "summary": summary,
                "evidence_file": evidence_name,
                "report": report,
            }
        except Exception as exc:
            summary = {
                "resource": assignment.resource,
                "profile": profile_key,
                "model": assignment.expected_model,
                "serial": "—",
                "firmware": "—",
                "output": "—",
                "interlock": "—",
                "line_frequency_hz": "—",
                "queries_ok": 0,
                "queries_total": 0,
                "warnings": [f"{type(exc).__name__}: {exc}"],
                "status": "error",
            }
            evidence_name = f"{assignment.key}-error.json"
            error_evidence = {
                "operation": "GUI core scan",
                "mode": mode,
                "assignment": assignment.as_dict(),
                "profile": profile_key,
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
                "host_environment": host_environment(),
            }
            _write_new_json(run_dir / evidence_name, error_evidence)
            row = {
                "assignment": assignment.as_dict(),
                "profile": profile_key,
                "ok": False,
                "summary": summary,
                "evidence_file": evidence_name,
                "report": error_evidence,
            }
        finally:
            if transport is not None:
                try:
                    transport.close()
                except Exception as close_exc:
                    # The query evidence remains useful. Surface a close failure
                    # as a warning instead of losing the completed batch scan.
                    close_message = f"VISA session close warning: {type(close_exc).__name__}: {close_exc}"
                    if "row" in locals():
                        row["summary"]["warnings"].append(close_message)
                        if row["summary"]["status"] == "passed":
                            row["summary"]["status"] = "warning"

        rows.append(row)
        if progress:
            progress(assignment.key, str(row["summary"]["status"]), row)

    finished = datetime.now(timezone.utc)
    batch = {
        "schema_version": 1,
        "operation": "one-click query-only GUI core scan of confirmed laboratory map",
        "mode": mode,
        "project_version": __version__,
        "started_utc": started.isoformat(),
        "finished_utc": finished.isoformat(),
        "run_directory": str(run_dir),
        "host_environment": host_environment(),
        "production_readiness": (
            readiness_provider() if mode == "real" else {"host_gate_passed": None, "blockers": []}
        ),
        "safety": {
            "query_only": True,
            "scope": "core",
            "fixed_confirmed_address_map": True,
            "2450_command_set": "TSP",
            "generic_write_controls_exposed": False,
            "reset_clear_trigger_and_acquisition_controls_exposed": False,
        },
        "all_six_completed_without_io_or_identity_error": all(row["ok"] for row in rows),
        "warning_count": sum(len(row["summary"]["warnings"]) for row in rows),
        "instruments": [
            {key: value for key, value in row.items() if key != "report"}
            for row in rows
        ],
    }
    _write_new_json(run_dir / "gui-scan-summary.json", batch)
    batch["rows"] = rows
    return batch


class InstrumentProbeApp:
    def __init__(self, root) -> None:
        import tkinter as tk
        from tkinter import filedialog, ttk

        self.tk = tk
        self.ttk = ttk
        self.filedialog = filedialog
        self.root = root
        self.root.title("Laboratory Instrument Read-Only Scanner")
        self.root.geometry("1220x760")
        self.root.minsize(980, 620)
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.rows_by_key: dict[str, dict[str, object]] = {}
        self.output_root = Path.cwd() / "gui_runs"

        self._configure_style()
        self._build_ui()
        self.root.after(100, self._drain_events)

    def _configure_style(self) -> None:
        style = self.ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Title.TLabel", font=("Segoe UI", 19, "bold"))
        style.configure("Subtitle.TLabel", font=("Segoe UI", 10))
        style.configure("Safe.TLabel", foreground="#176b3a", font=("Segoe UI", 10, "bold"))
        style.configure("Scan.TButton", font=("Segoe UI", 11, "bold"), padding=(18, 10))
        style.configure("Treeview", rowheight=30, font=("Segoe UI", 9))
        style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))

    def _build_ui(self) -> None:
        tk, ttk = self.tk, self.ttk
        outer = ttk.Frame(self.root, padding=18)
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer)
        header.pack(fill="x")
        ttk.Label(header, text="Six Keithley Instruments", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text="Fixed addresses · Core allowlist · Automatic evidence capture · No configuration controls",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(2, 0))
        ttk.Label(
            header,
            text="QUERY ONLY: no reset, clear, trigger, acquisition, or output changes.",
            style="Safe.TLabel",
        ).pack(anchor="w", pady=(8, 12))

        controls = ttk.Frame(outer)
        controls.pack(fill="x", pady=(0, 12))
        ttk.Label(controls, text="Mode:").pack(side="left")
        # Always start offline. Real VISA access requires an explicit selection
        # on every launch, while the actual scan remains one button click.
        self.mode_var = tk.StringVar(value="simulate")
        self.mode_combo = ttk.Combobox(
            controls,
            state="readonly",
            width=25,
            textvariable=self.mode_var,
            values=("real", "simulate"),
        )
        self.mode_combo.pack(side="left", padx=(4, 12))
        self.scan_button = ttk.Button(
            controls, text="Scan Six Instruments", style="Scan.TButton", command=self._start_scan
        )
        self.scan_button.pack(side="left")
        ttk.Button(controls, text="Change Output Folder", command=self._choose_output).pack(side="left", padx=8)
        self.output_label = ttk.Label(controls, text=str(self.output_root))
        self.output_label.pack(side="left", padx=(8, 0), fill="x", expand=True)

        table_frame = ttk.Frame(outer)
        table_frame.pack(fill="x")
        columns = ("status", "resource", "model", "serial", "firmware", "output", "interlock", "queries")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", height=7)
        headings = {
            "status": "Status", "resource": "VISA Resource", "model": "Model", "serial": "Serial",
            "firmware": "Firmware", "output": "Output", "interlock": "Interlock", "queries": "Queries",
        }
        widths = {"status": 90, "resource": 165, "model": 85, "serial": 105, "firmware": 105, "output": 120, "interlock": 130, "queries": 70}
        for name in columns:
            self.tree.heading(name, text=headings[name])
            self.tree.column(name, width=widths[name], anchor="center", stretch=name in {"resource", "interlock"})
        self.tree.pack(side="left", fill="x", expand=True)
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        scrollbar.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.tag_configure("passed", background="#e8f6ed")
        self.tree.tag_configure("warning", background="#fff4d6")
        self.tree.tag_configure("error", background="#fde8e8")
        self.tree.tag_configure("scanning", background="#e7f0fa")
        self.tree.bind("<<TreeviewSelect>>", self._show_selected)

        for assignment in LAB_INSTRUMENTS:
            self.tree.insert(
                "", "end", iid=assignment.key,
                values=("Waiting", assignment.resource, assignment.expected_model, "—", "—", "—", "—", "0/0"),
            )

        detail_box = ttk.LabelFrame(outer, text="Complete Core Information for Selected Instrument", padding=10)
        detail_box.pack(fill="both", expand=True, pady=(14, 8))
        self.detail = tk.Text(
            detail_box, wrap="none", height=16, relief="flat", background="#f7f8fa",
            font=("Consolas", 10), padx=8, pady=8,
        )
        self.detail.pack(side="left", fill="both", expand=True)
        detail_scroll = ttk.Scrollbar(detail_box, orient="vertical", command=self.detail.yview)
        detail_scroll.pack(side="right", fill="y")
        self.detail.configure(yscrollcommand=detail_scroll.set, state="disabled")

        self.status_var = tk.StringVar(value="Ready. Offline simulation is the default; select real explicitly for production VISA access.")
        ttk.Label(outer, textvariable=self.status_var).pack(anchor="w", pady=(2, 0))

    def _choose_output(self) -> None:
        selected = self.filedialog.askdirectory(initialdir=str(self.output_root.parent))
        if selected:
            self.output_root = Path(selected)
            self.output_label.configure(text=str(self.output_root))

    def _start_scan(self) -> None:
        if not self.scan_button.instate(["!disabled"]):
            return
        mode = self.mode_var.get()
        self.scan_button.state(["disabled"])
        self.mode_combo.configure(state="disabled")
        self.rows_by_key.clear()
        self._set_detail("Scan in progress...")
        self.status_var.set("Running offline simulation..." if mode == "simulate" else "Running sequential real query-only core scan...")
        for assignment in LAB_INSTRUMENTS:
            self.tree.item(
                assignment.key,
                values=("Waiting", assignment.resource, assignment.expected_model, "—", "—", "—", "—", "0/0"),
                tags=(),
            )

        worker = threading.Thread(target=self._scan_worker, args=(mode,), daemon=True)
        worker.start()

    def _scan_worker(self, mode: str) -> None:
        def progress(key: str, status: str, row: dict[str, object] | None) -> None:
            self.events.put(("progress", (key, status, row)))

        try:
            result = scan_confirmed_lab(
                self.output_root, mode=mode, timeout_ms=3000, progress=progress
            )
            self.events.put(("done", result))
        except Exception as exc:
            self.events.put(("fatal", f"{type(exc).__name__}: {exc}"))

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "progress":
                    key, status, row = payload
                    self._update_row(key, status, row)
                elif kind == "done":
                    self._scan_done(payload)
                elif kind == "fatal":
                    self._scan_fatal(str(payload))
        except queue.Empty:
            pass
        self.root.after(100, self._drain_events)

    def _update_row(self, key: str, status: str, row: dict[str, object] | None) -> None:
        if status == "scanning":
            current = list(self.tree.item(key, "values"))
            current[0] = "Scanning"
            self.tree.item(key, values=current, tags=("scanning",))
            self.status_var.set(f"Scanning {current[1]}...")
            return
        if not row:
            return
        self.rows_by_key[key] = row
        summary = row["summary"]
        label = {"passed": "Passed", "warning": "Warning", "error": "Error"}.get(status, status)
        values = (
            label, summary["resource"], summary["model"], summary["serial"], summary["firmware"],
            summary["output"], summary["interlock"], f'{summary["queries_ok"]}/{summary["queries_total"]}',
        )
        self.tree.item(key, values=values, tags=(status,))

    def _scan_done(self, result: dict[str, object]) -> None:
        self.scan_button.state(["!disabled"])
        self.mode_combo.configure(state="readonly")
        run_dir = result["run_directory"]
        warning_count = result["warning_count"]
        all_ok = result["all_six_completed_without_io_or_identity_error"]
        self.status_var.set(
            f"Complete: {'all six instruments communicated successfully' if all_ok else 'one or more instruments had errors'}; "
            f"warnings: {warning_count}. Evidence: {run_dir}"
        )
        if self.tree.selection():
            self._show_selected()

    def _scan_fatal(self, message: str) -> None:
        from tkinter import messagebox

        self.scan_button.state(["!disabled"])
        self.mode_combo.configure(state="readonly")
        self.status_var.set(f"Scan did not start or was stopped: {message}")
        messagebox.showerror("Read-Only Scan Not Executed", message)

    def _show_selected(self, _event=None) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        key = selection[0]
        row = self.rows_by_key.get(key)
        if not row:
            self._set_detail("This instrument has not completed its scan yet.")
            return
        report = row["report"]
        summary = row["summary"]
        lines = [
            f'VISA resource: {summary["resource"]}',
            f'Model: {summary["model"]}    Serial: {summary["serial"]}    Firmware: {summary["firmware"]}',
            f'Output: {summary["output"]}    Interlock: {summary["interlock"]}',
            f'Evidence file: {row["evidence_file"]}',
            "",
        ]
        warnings = summary.get("warnings", [])
        if warnings:
            lines.append("Safety / communication notices:")
            lines.extend(f"  - {item}" for item in warnings)
            lines.append("")
        instrument = report.get("instrument", {}) if isinstance(report, dict) else {}
        if isinstance(instrument, dict):
            lines.append("Core parameters:")
            for name, item in instrument.items():
                value = item.get("value") if isinstance(item, dict) else item
                lines.append(f"  {name:<34} {value}")
        else:
            lines.append(json.dumps(report, indent=2, ensure_ascii=False))
        self._set_detail("\n".join(lines))

    def _set_detail(self, text: str) -> None:
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", text)
        self.detail.configure(state="disabled")


def main() -> None:
    try:
        import tkinter as tk
    except ImportError as exc:
        raise SystemExit("GUI requires Python with Tkinter support.") from exc

    root = tk.Tk()
    InstrumentProbeApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
