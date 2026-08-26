"""Inventory-driven query-only diagnostics with GPIB6 2182A live voltage.

The instrument protocol is intentionally small and auditable:

* configuration: exact allow-listed SCPI queries only;
* live data: FETCh? only;
* no generic write API and no configuration controls.

The graph uses Tkinter Canvas, so the only external runtime dependency is
PyVISA (plus the NI-VISA installation already present on the laboratory PC).
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import queue
import re
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from diagnostic_core import (
    GPIB6_TARGET,
    OPERATION_CONDITION_COMMAND,
    OPERATION_OBSERVATION_NAMES,
    OPERATION_OBSERVATION_OFFSETS_SECONDS,
    DeviceTarget,
    DiagnosticState,
    evaluate_observe_readiness,
    evaluate_readiness,
    identity_is_exact,
    parse_idn,
    summarize_2182a_operation_observation,
)
from fault_injection import FAULT_SCENARIOS, FAULT_SCENARIO_NAMES, SimulationContext
from instrument_inventory import (
    InventoryEntry,
    InventorySnapshot,
    build_simulated_inventory,
    refresh_inventory,
)
from instrument_profiles import (
    CommandSet,
    InstrumentTarget,
    KNOWN_ASSETS,
    diagnostic_queries_for_profile,
    live_query_for_target,
    profile_for_key,
    query_is_applicable,
    summary_rows_for_profile,
    target_for,
    validate_profile_read_transaction,
)
from run_evidence import APP_RELEASE_TAG, INTERVENTION_TYPES, RecorderError, RunJournal
from stream_quality import analyze_stream_csv


DEFAULT_TARGET_KEY = "2182a-gpib6"
RESOURCE = GPIB6_TARGET.resource
EXPECTED_MODEL = GPIB6_TARGET.model
EXPECTED_SERIAL = GPIB6_TARGET.serial

CONFIG_QUERIES: tuple[tuple[str, str], ...] = (
    ("identity", "*IDN?"),
    ("scpi_version", "SYST:VERS?"),
    ("line_frequency_hz", "SYST:LFREQUENCY?"),
    ("power_on_setup", "SYST:POSETUP?"),
    ("system_autozero", "SYST:AZERO?"),
    ("front_autozero", "SYST:FAZERO?"),
    ("line_sync", "SYST:LSYNC?"),
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
    ("operation_condition", "STAT:OPER:COND?"),
    ("measurement_condition", "STAT:MEAS:COND?"),
    ("questionable_condition", "STAT:QUES:COND?"),
    ("data_format", "FORM:DATA?"),
    ("format_elements", "FORM:ELEM?"),
)

FETCH_QUERY = "FETCh?"


class OperationOwner(NamedTuple):
    """Immutable ownership metadata for one host-side worker operation."""

    operation_id: str
    kind: str
    mode: str
    target_key: str | None = None
    inventory_snapshot_id: str | None = None
    run_id: str | None = None
    stream_id: str | None = None
ALLOWED_QUERIES = frozenset(command for _, command in CONFIG_QUERIES) | {FETCH_QUERY}

CSV_FIELDS = (
    "elapsed_seconds",
    "voltage_v",
    "raw_response",
    "query_elapsed_ms",
)
PLOT_WINDOW_SECONDS = 600.0


def operation_observation_plan(
    profile_key: str | None,
) -> tuple[tuple[str, float], ...]:
    if profile_key != "2182a":
        return ()
    return tuple(zip(OPERATION_OBSERVATION_NAMES, OPERATION_OBSERVATION_OFFSETS_SECONDS))

SIMULATED_VALUES = {
    "*IDN?": "KEITHLEY INSTRUMENTS INC.,MODEL 2182A,1340129,C02  /A02",
    "SYST:VERS?": "1991.0",
    "SYST:LFREQUENCY?": "50",
    "SYST:POSETUP?": "SAV0",
    "SYST:AZERO?": "1",
    "SYST:FAZERO?": "1",
    "SYST:LSYNC?": "1",
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
    "STAT:OPER:COND?": "16",
    "STAT:MEAS:COND?": "32",
    "STAT:QUES:COND?": "0",
    "FORM:DATA?": "ASC",
    "FORM:ELEM?": "READ",
}


def _identity_for(target_key: str) -> str:
    target = target_for(target_key)
    return f"{target.vendor},MODEL {target.model},{target.serial},{target.firmware}"


SIMULATED_VALUES_BY_TARGET: dict[str, dict[str, str]] = {
    "2182a-gpib6": dict(SIMULATED_VALUES),
    "2182a-gpib7": {
        **SIMULATED_VALUES,
        "*IDN?": _identity_for("2182a-gpib7"),
        "SENS:VOLT:DC:NPLC?": "1.00",
        "SENS:VOLT:DC:CHAN1:RANG?": "0.100000",
        "SENS:VOLT:DC:CHAN1:RANG:AUTO?": "1",
    },
    "6221-gpib9": {
        "*IDN?": _identity_for("6221-gpib9"),
        "OUTP?": "0",
        "OUTP:INTERLOCK:TRIPPED?": "0",
        "STAT:OPER:COND?": "1024",
        "STAT:MEAS:COND?": "0",
        "STAT:QUES:COND?": "0",
        "*STB?": "0",
        "SOUR:CURR:RANG?": "0.100000",
        "SOUR:CURR:RANG:AUTO?": "0",
        "SOUR:CURR:COMP?": "10.0000",
        "SOUR:CURR:FILT?": "0",
        "OUTP:RESPONSE?": "FAST",
        "OUTP:ISHIELD?": "OLOW",
        "OUTP:LTEARTH?": "0",
    },
    "6221-gpib10": {
        "*IDN?": _identity_for("6221-gpib10"),
        "OUTP?": "0",
        "OUTP:INTERLOCK:TRIPPED?": "0",
        "STAT:OPER:COND?": "1024",
        "STAT:MEAS:COND?": "0",
        "STAT:QUES:COND?": "0",
        "*STB?": "0",
        "SOUR:CURR:RANG?": "0.100000",
        "SOUR:CURR:RANG:AUTO?": "0",
        "SOUR:CURR:COMP?": "10.0000",
        "SOUR:CURR:FILT?": "0",
        "OUTP:RESPONSE?": "FAST",
        "OUTP:ISHIELD?": "OLOW",
        "OUTP:LTEARTH?": "0",
    },
    "2450-gpib25": {
        "*IDN?": _identity_for("2450-gpib25"),
        "print(localnode.model)": "2450",
        "print(localnode.serialno)": "04584128",
        "print(localnode.version)": "1.7.12b",
        "print(localnode.linefreq)": "50",
        "print(smu.terminals)": "0",
        "print(smu.source.output)": "0",
        "print(smu.source.offmode)": "2",
        "print(smu.source.func)": "1",
        "print(smu.source.level)": "0",
        "print(smu.source.autorange)": "1",
        "print(smu.source.range)": "2",
        "print(smu.measure.func)": "0",
        "print(smu.measure.sense)": "0",
        "print(smu.measure.autorange)": "1",
        "print(smu.measure.range)": "0.001",
        "print(smu.measure.nplc)": "1",
        "print(smu.measure.autozero.enable)": "1",
        "print(smu.source.readback)": "1",
        "print(smu.measure.filter.enable)": "0",
        "print(smu.measure.filter.type)": "0",
        "print(smu.measure.filter.count)": "10",
        "print(smu.source.ilimit.level)": "0.001",
        "print(smu.source.ilimit.tripped)": "0",
        "print(smu.source.vlimit.level)": "10",
        "print(smu.source.vlimit.tripped)": "0",
        "print(smu.source.protect.level)": "20",
        "print(smu.source.protect.tripped)": "0",
        "print(smu.interlock.enable)": "0",
        "print(smu.interlock.tripped)": "0",
        "print(status.condition)": "0",
        "print(status.operation.condition)": "0",
        "print(status.questionable.condition)": "0",
    },
}


def resolve_target(target_ref: str | InstrumentTarget) -> InstrumentTarget:
    return target_ref if isinstance(target_ref, InstrumentTarget) else target_for(target_ref)


def target_from_inventory_entry(entry: InventoryEntry) -> InstrumentTarget | None:
    """Freeze one recognised inventory identity as a diagnostic target.

    Known assets retain their historical fixture key, but only the one exact
    GPIB6 identity retains Live capability.  Any future instance receives the
    reusable profile selected by its exact model and remains diagnostic-only.
    """

    if (
        entry.status != "recognized"
        or entry.identity is None
        or entry.profile_key is None
        or entry.idn_raw is None
    ):
        return None
    for known in KNOWN_ASSETS.values():
        if (
            entry.resource.upper() == known.resource.upper()
            and identity_is_exact(entry.idn_raw, core_target_for(known))
        ):
            return known

    address_parts = entry.resource.split("::")
    address = address_parts[1] if len(address_parts) > 1 else "unknown"
    safe_serial = re.sub(r"[^A-Za-z0-9_.-]+", "_", entry.identity.serial).strip("_")
    if not safe_serial:
        safe_serial = "unknown-serial"
    key = f"{entry.profile_key}-gpib{address}-{safe_serial}"
    return InstrumentTarget(
        key=key,
        label=(
            f"Keithley {entry.identity.model} · GPIB{address} · "
            f"S/N {entry.identity.serial}"
        ),
        resource=entry.resource,
        vendor=entry.identity.vendor,
        model=entry.identity.model,
        serial=entry.identity.serial,
        firmware=entry.identity.firmware,
        profile_key=entry.profile_key,
        live_supported=False,
    )


def inventory_entry_label(entry: InventoryEntry) -> str:
    target = target_from_inventory_entry(entry)
    if target is not None:
        return target.label
    if entry.identity is not None:
        return (
            f"{entry.resource} · {entry.identity.model} · "
            f"S/N {entry.identity.serial} · {entry.status}"
        )
    return f"{entry.resource} · identity unavailable · {entry.status}"


def core_target_for(target_ref: str | InstrumentTarget) -> DeviceTarget:
    target = resolve_target(target_ref)
    return DeviceTarget(
        resource=target.resource,
        vendor=target.vendor,
        model=target.model,
        serial=target.serial,
        firmware=target.firmware,
        role=target.key,
    )


def profile_id_for(target_ref: str | InstrumentTarget) -> str:
    target = resolve_target(target_ref)
    if target.profile_key is None:
        raise ValueError(f"target has no approved diagnostic profile: {target.label}")
    return f"keithley-{target.profile_key}-read-only-{APP_RELEASE_TAG}"


def allowed_queries_for_target(
    target_ref: str | InstrumentTarget,
) -> frozenset[str]:
    target = resolve_target(target_ref)
    if target.profile_key is None:
        return frozenset({"*IDN?"})
    profile = profile_for_key(target.profile_key)
    commands = {spec.command for spec in profile.diagnostic_queries}
    if is_approved_gpib6_live_target(target) and profile.live_query is not None:
        commands.add(profile.live_query.command)
    return frozenset(commands)


def fault_scenarios_for_target(
    target_ref: str | InstrumentTarget,
) -> tuple[str, ...]:
    target = resolve_target(target_ref)
    if is_approved_gpib6_live_target(target):
        return tuple(
            name
            for name in FAULT_SCENARIO_NAMES
            if not name.startswith(("6221_", "2450_"))
        )
    # These scenarios are command-independent or target the universal *IDN?
    # without pretending that 2182A configuration/FETCh faults apply to a source.
    common = ("nominal", "malformed_identity", "event_write_failure")
    if target.profile_key == "6221":
        return common + (
            "6221_output_on",
            "6221_interlock_invalid",
            "6221_over_temperature",
            "6221_compliance_active",
            "6221_calibration_questionable",
            "6221_invalid_response",
            "6221_configuration_timeout",
        )
    if target.profile_key == "2450":
        return common + (
            "2450_output_on",
            "2450_invalid_source_mode",
            "2450_active_limit",
            "2450_protection_tripped",
            "2450_interlock_invalid",
            "2450_invalid_response",
            "2450_configuration_timeout",
        )
    return common


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


def json_payload_sha256(payload: object) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


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


def format_health_axes(health_axes: dict[str, object]) -> str:
    if not health_axes:
        return "Health axes: not evaluated"
    return "Health axes: " + " · ".join(
        f"{name.replace('_', ' ')} {status}"
        for name, status in health_axes.items()
    )


def instrument_status_key(
    snapshot: InventorySnapshot,
    entry: InventoryEntry,
) -> tuple[str, str, str, str, str, str]:
    identity = entry.identity
    return (
        snapshot.snapshot_id,
        entry.resource,
        identity.vendor if identity is not None else "",
        identity.model if identity is not None else "",
        identity.serial if identity is not None else "",
        identity.firmware if identity is not None else "",
    )


def primary_diagnostic_issue(report: dict[str, object]) -> str:
    diagnostics = report.get("diagnostics")
    checks = diagnostics.get("checks") if isinstance(diagnostics, dict) else None
    check_items = checks if isinstance(checks, list) else []
    for wanted_status in ("BLOCKED", "WARN", "UNKNOWN"):
        for check in check_items:
            if not isinstance(check, dict) or check.get("status") != wanted_status:
                continue
            check_id = str(check.get("check_id", "unnamed check"))
            message = str(check.get("message", "No detail recorded."))
            return f"{wanted_status} {check_id} · {message}"
    transcript = report.get("transcript")
    query_items = transcript if isinstance(transcript, list) else []
    slowest_name = None
    slowest_elapsed = -1.0
    for item in query_items:
        if not isinstance(item, dict) or item.get("skipped"):
            continue
        try:
            elapsed = float(item.get("elapsed_ms"))
        except (TypeError, ValueError):
            continue
        if elapsed > slowest_elapsed:
            slowest_name = str(item.get("name", "query"))
            slowest_elapsed = elapsed
    if slowest_name is not None:
        return f"No blocking issue · slowest query {slowest_name} {slowest_elapsed:.3f} ms"
    return "No blocking issue recorded."


def identity_is_expected(
    identity: str,
    target_ref: str | InstrumentTarget = DEFAULT_TARGET_KEY,
) -> bool:
    return identity_is_exact(identity, core_target_for(target_ref))


def is_approved_gpib6_live_target(target_ref: str | InstrumentTarget) -> bool:
    target = resolve_target(target_ref)
    approved = KNOWN_ASSETS[DEFAULT_TARGET_KEY]
    return bool(
        target.live_supported
        and target.resource == approved.resource
        and target.vendor == approved.vendor
        and target.model == approved.model
        and target.serial == approved.serial
        and target.firmware == approved.firmware
    )


def live_start_is_safe(
    configuration: dict[str, object] | None,
    state: DiagnosticState | None,
    *,
    recording_fault_latched: bool,
    stream_had_error: bool,
) -> bool:
    diagnostics = configuration.get("diagnostics") if configuration else None
    capabilities = configuration.get("capabilities") if configuration else None
    live_authorized = bool(
        isinstance(capabilities, dict)
        and capabilities.get("live_supported")
        and capabilities.get("live_authorized")
    )
    return bool(
        isinstance(diagnostics, dict)
        and diagnostics.get("can_start_live")
        and live_authorized
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

    def __init__(
        self,
        timeout_ms: int = 3000,
        *,
        target: InstrumentTarget | None = None,
        phase: str = "diagnostic",
    ) -> None:
        if target is None:
            raise ValueError("Real VISA session requires an explicit inventory target")
        resolved_target = resolve_target(target)
        if resolved_target.profile_key is None:
            raise ValueError("unsupported target has no diagnostic session")
        try:
            import pyvisa
        except ImportError as exc:
            raise RuntimeError(
                "PyVISA is missing from C:\\LabAutomation\\.venv. "
                "Install pyvisa in that shared environment."
            ) from exc
        self.pyvisa = pyvisa
        self.timeout_ms = timeout_ms
        self.target = resolved_target
        self.phase = "live" if phase == "stream" else "diagnostic"
        self.manager = None
        self.instrument = None

    def __enter__(self):
        self.manager = self.pyvisa.ResourceManager()
        self.instrument = self.manager.open_resource(self.target.resource)
        self.instrument.timeout = self.timeout_ms
        self.instrument.write_termination = "\n"
        self.instrument.read_termination = "\n"
        return self

    def query(self, command: str) -> str:
        validate_profile_read_transaction(
            self.target.profile_key,
            command,
            phase=self.phase,
            live_approved=is_approved_gpib6_live_target(self.target),
        )
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
        *,
        target_key: str = DEFAULT_TARGET_KEY,
        target: InstrumentTarget | None = None,
    ) -> None:
        self.target = resolve_target(target if target is not None else target_key)
        if self.target.profile_key is None:
            raise ValueError("unsupported target has no diagnostic simulation")
        self.transaction_phase = "live" if phase == "stream" else "diagnostic"
        self.allowed_queries = allowed_queries_for_target(self.target)
        self.context = context or SimulationContext("nominal", self.allowed_queries)
        self.phase = phase

    def __enter__(self):
        return self

    def query(self, command: str) -> str:
        validate_profile_read_transaction(
            self.target.profile_key,
            command,
            phase=self.transaction_phase,
            live_approved=is_approved_gpib6_live_target(self.target),
        )
        return self.context.execute_query(
            self.phase,
            command,
            lambda: (
                self.context.next_voltage()
                if command == FETCH_QUERY
                else SIMULATED_VALUES_BY_TARGET[self.target.key][command]
            ),
        )

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self.phase == "config" and self.context.should_fail_config_session_close():
            raise OSError("simulated configuration session close failure")
        return None


def session_factory(
    mode: str,
    phase: str = "config",
    simulation_context: SimulationContext | None = None,
    *,
    target_key: str | None = None,
    target: InstrumentTarget | None = None,
):
    if mode == "real":
        if simulation_context is not None:
            raise ValueError("fault injection is forbidden in real mode")
        if target_key is not None:
            raise ValueError("real mode forbids static target_key selection")
        if target is None:
            raise ValueError("real mode requires an explicit inventory target")
        return RealSession(target=target, phase=phase)
    if mode == "simulate":
        if target is None and target_key is None:
            target_key = DEFAULT_TARGET_KEY
        return SimulatedSession(
            simulation_context,
            phase,
            target_key=target_key,
            target=target,
        )
    raise ValueError(f"Unsupported mode: {mode}")


def collect_configuration(
    mode: str,
    simulation_context: SimulationContext | None = None,
    *,
    recorder_ready: bool = True,
    target_key: str | None = None,
    target: InstrumentTarget | None = None,
) -> dict[str, object]:
    if mode == "real" and simulation_context is not None:
        raise ValueError("fault injection is forbidden in real mode")
    if mode == "real":
        if target_key is not None:
            raise ValueError("real configuration forbids static target_key selection")
        if target is None:
            raise ValueError("real configuration requires an explicit inventory target")
    transcript: list[dict[str, object]] = []
    values: dict[str, str] = {}
    target = resolve_target(
        target
        if target is not None
        else (target_key if target_key is not None else DEFAULT_TARGET_KEY)
    )
    if target.profile_key is None:
        raise ValueError(f"unsupported model has no diagnostic profile: {target.model}")
    core_target = core_target_for(target)
    profile = profile_for_key(target.profile_key)
    query_specs = diagnostic_queries_for_profile(target.profile_key)
    query_specs_by_name = {spec.name: spec for spec in query_specs}
    observation_plan = operation_observation_plan(target.profile_key)
    live_authorized = is_approved_gpib6_live_target(target)
    primary_complete = True
    try:
        with session_factory(
            mode,
            "config",
            simulation_context,
            target=target,
        ) as session:
            session_started = time.perf_counter()
            for spec in query_specs:
                name, command = spec.name, spec.command
                if not query_is_applicable(spec, values):
                    transcript.append(
                        {
                            "name": name,
                            "command": command,
                            "ok": True,
                            "skipped": True,
                            "condition": spec.condition,
                            "reason": "not applicable to the observed source function",
                            "elapsed_ms": 0.0,
                        }
                    )
                    continue
                started = time.perf_counter()
                item: dict[str, object] = {
                    "name": name,
                    "command": command,
                }
                if command == OPERATION_CONDITION_COMMAND:
                    item["session_elapsed_seconds"] = round(
                        started - session_started,
                        9,
                    )
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
                    if name == "identity" and not identity_is_expected(response, target):
                        item.update(
                            {
                                "ok": False,
                                "error": (
                                    "Exact identity mismatch. Expected "
                                    f"{target.vendor}, MODEL {target.model}, "
                                    f"serial {target.serial}, firmware {target.firmware}; "
                                    f"got {response!r}"
                                ),
                            }
                        )
                        transcript.append(item)
                        primary_complete = False
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
                    primary_complete = False
                    break
            if primary_complete:
                for source_name in profile.snapshot_end_names:
                    spec = query_specs_by_name[source_name]
                    end_name = f"snapshot_end.{source_name}"
                    started = time.perf_counter()
                    item = {
                        "name": end_name,
                        "source_name": source_name,
                        "phase": "snapshot_end",
                        "command": spec.command,
                    }
                    if spec.command == OPERATION_CONDITION_COMMAND:
                        item["session_elapsed_seconds"] = round(
                            started - session_started,
                            9,
                        )
                    try:
                        response = session.query(spec.command)
                        values[end_name] = response
                        item.update(
                            {
                                "ok": True,
                                "response": response,
                                "elapsed_ms": round(
                                    (time.perf_counter() - started) * 1000,
                                    3,
                                ),
                            }
                        )
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
                        primary_complete = False
                        break
                if observation_plan and primary_complete:
                    observation_started = time.perf_counter()
                    observation_started_elapsed = observation_started - session_started
                    for sample_index, (name, scheduled_offset) in enumerate(observation_plan):
                        if mode == "real":
                            remaining = observation_started + scheduled_offset - time.perf_counter()
                            if remaining > 0.0:
                                time.sleep(remaining)
                        started = time.perf_counter()
                        observed_offset = (
                            started - observation_started
                            if mode == "real"
                            else scheduled_offset
                        )
                        session_elapsed = (
                            started - session_started
                            if mode == "real"
                            else observation_started_elapsed + scheduled_offset
                        )
                        item = {
                            "name": name,
                            "phase": "operation_observation",
                            "sample_index": sample_index,
                            "scheduled_offset_seconds": scheduled_offset,
                            "observation_elapsed_seconds": round(observed_offset, 9),
                            "session_elapsed_seconds": round(session_elapsed, 9),
                            "command": OPERATION_CONDITION_COMMAND,
                        }
                        try:
                            response = session.query(OPERATION_CONDITION_COMMAND)
                            values[name] = response
                            item.update(
                                {
                                    "ok": True,
                                    "response": response,
                                    "elapsed_ms": round(
                                        (time.perf_counter() - started) * 1000,
                                        3,
                                    ),
                                }
                            )
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
        else:
            transcript.append(
                {
                    "name": "session_lifecycle",
                    "command": None,
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "elapsed_ms": 0.0,
                }
            )

    operation_observation = (
        summarize_2182a_operation_observation(transcript)
        if target.profile_key == "2182a"
        else None
    )
    required_names = [
        spec.name for spec in query_specs if query_is_applicable(spec, values)
    ] + [f"snapshot_end.{name}" for name in profile.snapshot_end_names] + [
        name for name, _offset in observation_plan
    ]
    if is_approved_gpib6_live_target(target):
        readiness = evaluate_readiness(
            values,
            transcript,
            required_names,
            recorder_ready=recorder_ready,
            consistency_names=profile.consistency_names,
            operation_observation=operation_observation,
        )
    else:
        readiness = evaluate_observe_readiness(
            values,
            transcript,
            required_names,
            recorder_ready=recorder_ready,
            target=core_target,
            instrument_family=target.profile_key,
            consistency_names=profile.consistency_names,
            operation_observation=operation_observation,
        )
    readiness_dict = readiness.as_dict(
        profile_id=profile_id_for(target),
        live_supported=target.live_supported,
        live_authorized=live_authorized,
    )
    diagnostics_acceptable = readiness.can_start_live
    can_start_live = diagnostics_acceptable and live_authorized
    readiness_dict.update(
        {
            "diagnostics_acceptable": diagnostics_acceptable,
            "live_supported": target.live_supported,
            "live_authorized": live_authorized,
            "can_start_live": can_start_live,
        }
    )
    summary_rows = [
        asdict(row)
        for row in summary_rows_for_profile(target.profile_key, target, values)
    ]
    if operation_observation is not None:
        summary_rows.insert(
            1,
            {
                "key": "operation_observation",
                "label": "Operation B0 observation",
                "value": (
                    f"{operation_observation['classification']}; "
                    f"set {operation_observation['b0_set_count']}/"
                    f"{operation_observation['valid_sample_count']}; "
                    f"window {operation_observation['observed_window_seconds']} s"
                ),
                "source_names": ("operation_observation",),
            },
        )
    return {
        "created_at": now_iso(),
        "operation": "query-only single-instrument diagnostic snapshot",
        "mode": mode,
        "target_key": target.key,
        "profile_id": profile_id_for(target),
        "command_set": profile.command_set.value,
        "resource": target.resource,
        "expected_identity": {
            "vendor": target.vendor,
            "model": target.model,
            "serial": target.serial,
            "firmware": target.firmware,
        },
        "capabilities": {
            "diagnostics_supported": True,
            "live_supported": target.live_supported,
            "live_authorized": live_authorized,
            "live_query": (
                profile.live_query.command
                if live_authorized and profile.live_query is not None
                else None
            ),
        },
        "safety": {
            "query_only": True,
            "exact_allowlist": True,
            "generic_write_api_exposed": False,
            "configuration_controls_exposed": False,
            "command_set": profile.command_set.value,
            "live_query": (
                profile.live_query.command
                if live_authorized and profile.live_query is not None
                else None
            ),
        },
        "values": values,
        "transcript": transcript,
        "operation_condition_observation": operation_observation,
        "precision_safety_summary": summary_rows,
        "query_plan": {
            "candidate_count": (
                len(query_specs)
                + len(profile.snapshot_end_names)
                + len(observation_plan)
            ),
            "executed_count": sum(
                1
                for item in transcript
                if item.get("command") is not None and not item.get("skipped")
            ),
            "skipped_count": sum(1 for item in transcript if item.get("skipped")),
            "required_names": required_names,
            "snapshot_end_names": list(profile.snapshot_end_names),
            "consistency_names": list(profile.consistency_names),
            "operation_observation": (
                operation_observation["protocol"]
                if operation_observation is not None
                else None
            ),
        },
        "diagnostics": readiness_dict,
        "live_readiness": {
            "identity_matches": identity_is_expected(values.get("identity", ""), target),
            "diagnostics_acceptable": diagnostics_acceptable,
            "live_supported": target.live_supported,
            "live_authorized": live_authorized,
            "ready": can_start_live,
            "overall": readiness.overall.value,
            "blockers": readiness_dict["blockers"],
            "warnings": readiness_dict["warnings"],
        },
        "derived_poll_interval_s": (
            derive_poll_interval(values) if live_authorized else None
        ),
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
        self.root.title(
            f"Keithley Inventory-Driven Query-Only Diagnostic Core {APP_RELEASE_TAG}"
        )
        self.root.geometry("1520x920")
        self.root.minsize(1180, 760)

        self.events: queue.Queue[tuple[OperationOwner, str, object]] = queue.Queue()
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
        self.inventory_snapshot: InventorySnapshot | None = None
        self.inventory_usable = False
        self.inventory_evidence_file: Path | None = None
        self.inventory_label_to_entry: dict[str, InventoryEntry] = {}
        self.inventory_tree_label_by_iid: dict[str, str] = {}
        self.instrument_status_by_key: dict[
            tuple[str, str, str, str, str, str], dict[str, object]
        ] = {}
        self.operation_pending = False
        self.pending_diagnostic_status_key: tuple[
            str, str, str, str, str, str
        ] | None = None
        self.current_diagnostic_status_key: tuple[
            str, str, str, str, str, str
        ] | None = None
        self.selected_inventory_label: str | None = None
        self.selected_target: InstrumentTarget | None = None
        self.diagnostic_target: InstrumentTarget | None = None
        self.active_operation: OperationOwner | None = None
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
            text=f"Keithley Single-Instrument Query-Only Diagnostic Core {APP_RELEASE_TAG}",
            style="Title.TLabel",
        ).pack(anchor="w")
        self.target_detail_var = tk.StringVar()
        ttk.Label(outer, textvariable=self.target_detail_var).pack(anchor="w", pady=(2, 0))
        self.safety_scope_var = tk.StringVar()
        ttk.Label(
            outer,
            textvariable=self.safety_scope_var,
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
        self.inventory_button = ttk.Button(
            controls,
            text="Refresh Inventory",
            command=self._refresh_inventory,
        )
        self.inventory_button.pack(side="left", padx=(0, 12))
        ttk.Label(controls, text="Instrument:").pack(side="left")
        self.target_var = tk.StringVar(value="")
        self.target_combo = ttk.Combobox(
            controls,
            state="disabled",
            width=52,
            values=(),
            textvariable=self.target_var,
        )
        self.target_combo.pack(side="left", padx=(5, 12))
        self.target_combo.bind("<<ComboboxSelected>>", self._target_changed)

        self.inventory_status_var = tk.StringVar(value="Inventory not loaded")
        ttk.Label(outer, textvariable=self.inventory_status_var).pack(
            anchor="w", pady=(4, 0)
        )

        inventory_box = ttk.LabelFrame(
            outer,
            text="Instrument Status Overview · latest result is display-only after selection changes",
            padding=6,
        )
        inventory_box.pack(fill="x", pady=(6, 0))
        self.inventory_tree = ttk.Treeview(
            inventory_box,
            columns=(
                "resource",
                "identity",
                "inventory",
                "profile",
                "diagnostic",
                "issue",
                "checked",
            ),
            show="headings",
            selectmode="browse",
            height=4,
        )
        for column, heading, width in (
            ("resource", "Resource", 145),
            ("identity", "Exact identity", 255),
            ("inventory", "Inventory", 105),
            ("profile", "Profile", 110),
            ("diagnostic", "Latest diagnostic", 125),
            ("issue", "Primary issue", 430),
            ("checked", "Checked at", 190),
        ):
            self.inventory_tree.heading(column, text=heading)
            self.inventory_tree.column(
                column,
                width=width,
                stretch=column in {"identity", "issue"},
            )
        self.inventory_tree.pack(fill="x", expand=True)
        self.inventory_tree.bind("<<TreeviewSelect>>", self._inventory_tree_selected)
        for status, color in (
            ("PASS", "#176b3a"),
            ("WARN", "#8a5a00"),
            ("BLOCKED", "#a51d2d"),
            ("UNKNOWN", "#5f6872"),
        ):
            self.inventory_tree.tag_configure(status, foreground=color)

        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=(6, 0))
        self.config_button = ttk.Button(
            actions,
            text="1. Run Read-Only Diagnostics",
            style="Action.TButton",
            state="disabled",
            command=self._read_configuration,
        )
        self.config_button.pack(side="left")
        self.start_button = ttk.Button(
            actions,
            text="2. Start Live Plot",
            style="Action.TButton",
            state="disabled",
            command=self._start_stream,
        )
        self.start_button.pack(side="left", padx=(7, 0))
        self.pause_button = ttk.Button(actions, text="Pause", state="disabled", command=self._pause_stream)
        self.pause_button.pack(side="left", padx=(7, 0))
        self.single_button = ttk.Button(
            actions, text="Single FETCh?", state="disabled", command=self._single_fetch
        )
        self.single_button.pack(side="left", padx=(7, 0))
        self.clear_button = ttk.Button(actions, text="Clear Plot", command=self._clear_plot)
        self.clear_button.pack(side="left", padx=(7, 0))
        self.output_button = ttk.Button(actions, text="Output Folder", command=self._choose_output)
        self.output_button.pack(side="left", padx=(7, 0))

        diagnostic_controls = ttk.Frame(outer)
        diagnostic_controls.pack(fill="x", pady=(6, 0))
        ttk.Label(diagnostic_controls, text="Simulation fault:").pack(side="left")
        self.fault_var = tk.StringVar(value="nominal")
        self.fault_combo = ttk.Combobox(
            diagnostic_controls,
            state="readonly",
            width=28,
            values=fault_scenarios_for_target(DEFAULT_TARGET_KEY),
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
        ttk.Label(outer, textvariable=self.readiness_var).pack(anchor="w", pady=(1, 1))
        self.health_axes_var = tk.StringVar(value="Health axes: not evaluated")
        ttk.Label(
            outer,
            textvariable=self.health_axes_var,
            wraplength=1450,
        ).pack(anchor="w", pady=(1, 6))
        self.primary_issue_var = tk.StringVar(value="Primary issue: not evaluated")
        ttk.Label(
            outer,
            textvariable=self.primary_issue_var,
            wraplength=1450,
        ).pack(anchor="w", pady=(0, 6))

        body = ttk.Panedwindow(outer, orient="horizontal")
        body.pack(fill="both", expand=True)
        left = ttk.Frame(body, padding=(0, 0, 8, 0))
        right = ttk.Frame(body)
        body.add(left, weight=2)
        body.add(right, weight=3)

        summary_box = ttk.LabelFrame(
            left,
            text="Precision & Safety Settings · read-only current values",
            padding=8,
        )
        summary_box.pack(fill="x")
        self.summary_tree = ttk.Treeview(
            summary_box,
            columns=("status", "setting", "value", "interpretation"),
            show="headings",
            selectmode="browse",
            height=10,
        )
        self.summary_tree.heading("status", text="Status")
        self.summary_tree.heading("setting", text="Setting")
        self.summary_tree.heading("value", text="Current readback")
        self.summary_tree.heading("interpretation", text="Interpretation")
        self.summary_tree.column("status", width=82, stretch=False)
        self.summary_tree.column("setting", width=150, stretch=False)
        self.summary_tree.column("value", width=330, stretch=True)
        self.summary_tree.column("interpretation", width=270, stretch=True)
        self.summary_tree.pack(side="left", fill="x", expand=True)
        summary_scroll = ttk.Scrollbar(
            summary_box,
            orient="vertical",
            command=self.summary_tree.yview,
        )
        summary_scroll.pack(side="right", fill="y")
        self.summary_tree.configure(yscrollcommand=summary_scroll.set)
        for status, color in (
            ("PASS", "#176b3a"),
            ("READ", "#123e73"),
            ("WARN", "#8a5a00"),
            ("BLOCKED", "#a51d2d"),
            ("UNKNOWN", "#5f6872"),
            ("N/A", "#5f6872"),
        ):
            self.summary_tree.tag_configure(status, foreground=color)

        config_box = ttk.LabelFrame(left, text="Complete Read-Only Evidence", padding=8)
        config_box.pack(fill="both", expand=True, pady=(8, 0))
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

        self.reading_box = ttk.LabelFrame(right, text="Latest voltage · GPIB6 Live only", padding=10)
        self.reading_box.pack(fill="x")
        self.reading_var = tk.StringVar(value="No sample")
        ttk.Label(self.reading_box, textvariable=self.reading_var, style="Reading.TLabel").pack(side="left")
        self.raw_var = tk.StringVar(value="Raw: —")
        ttk.Label(self.reading_box, textvariable=self.raw_var).pack(side="right")

        self.plot_box = ttk.LabelFrame(
            right,
            text="Voltage versus elapsed host time · latest 10 minutes",
            padding=6,
        )
        self.plot_box.pack(fill="both", expand=True, pady=(8, 0))
        self.canvas = tk.Canvas(self.plot_box, background="#ffffff", highlightthickness=0)
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
        self._install_inventory_snapshot(self.inventory_snapshot)

    def _current_inventory_entry(self) -> InventoryEntry | None:
        label = getattr(self, "selected_inventory_label", None)
        return self.inventory_label_to_entry.get(label) if label else None

    def _current_target(self) -> InstrumentTarget | None:
        return getattr(self, "selected_target", None)

    def _begin_operation(
        self,
        *,
        kind: str,
        mode: str,
        target_key: str | None = None,
        inventory_snapshot_id: str | None = None,
        run_id: str | None = None,
        stream_id: str | None = None,
    ) -> OperationOwner:
        if self.active_operation is not None:
            raise RuntimeError("another worker operation is still active")
        owner = OperationOwner(
            operation_id=str(uuid.uuid4()),
            kind=kind,
            mode=mode,
            target_key=target_key,
            inventory_snapshot_id=inventory_snapshot_id,
            run_id=run_id,
            stream_id=stream_id,
        )
        self.active_operation = owner
        self._set_busy(True)
        return owner

    def _finish_operation(self, owner: OperationOwner) -> bool:
        if owner != self.active_operation:
            return False
        self.active_operation = None
        self._set_busy(False)
        return True

    def _status_record_for_entry(
        self,
        entry: InventoryEntry | None,
    ) -> dict[str, object] | None:
        snapshot = self.inventory_snapshot
        if snapshot is None or entry is None:
            return None
        return self.instrument_status_by_key.get(
            instrument_status_key(snapshot, entry)
        )

    def _refresh_inventory_status_tree(self) -> None:
        tree = getattr(self, "inventory_tree", None)
        if tree is None:
            return
        tree.delete(*tree.get_children())
        self.inventory_tree_label_by_iid = {}
        for index, (label, entry) in enumerate(self.inventory_label_to_entry.items()):
            identity = entry.identity
            identity_text = (
                f"{identity.model} · S/N {identity.serial} · FW {identity.firmware}"
                if identity is not None
                else "identity unavailable"
            )
            record = self._status_record_for_entry(entry)
            report = record.get("report") if isinstance(record, dict) else None
            overall = str(record.get("overall", "NOT RUN")) if record else "NOT RUN"
            is_current = bool(
                record is not None
                and self.inventory_snapshot is not None
                and instrument_status_key(self.inventory_snapshot, entry)
                == getattr(self, "current_diagnostic_status_key", None)
            )
            diagnostic_text = (
                overall if is_current or record is None else f"{overall} · STALE"
            )
            issue = (
                str(record.get("primary_issue", ""))
                if record is not None
                else str(entry.error or entry.profile_resolution)
            )
            checked_at = str(record.get("checked_at", "—")) if record else "—"
            tag = overall if overall in {"PASS", "WARN", "BLOCKED", "UNKNOWN"} else "UNKNOWN"
            item_id = f"instrument-{index}"
            tree.insert(
                "",
                "end",
                iid=item_id,
                values=(
                    entry.resource,
                    identity_text,
                    entry.status,
                    entry.profile_key or "identity only",
                    diagnostic_text,
                    issue,
                    checked_at,
                ),
                tags=(tag,),
            )
            self.inventory_tree_label_by_iid[item_id] = label
            if label == self.selected_inventory_label:
                tree.selection_set(item_id)

    def _inventory_tree_selected(self, _event=None) -> None:
        tree = self.inventory_tree
        selected = tree.selection()
        if not selected:
            return
        label = self.inventory_tree_label_by_iid.get(str(selected[0]))
        if label is None:
            return
        self.target_var.set(label)
        self._target_changed()

    def _cache_diagnostic_status(
        self,
        report: dict[str, object],
        *,
        evidence: object,
    ) -> None:
        snapshot = self.inventory_snapshot
        status_key = getattr(self, "pending_diagnostic_status_key", None)
        if status_key is None:
            entry = self._current_inventory_entry()
            if snapshot is not None and entry is not None:
                status_key = instrument_status_key(snapshot, entry)
        if status_key is None or report.get("resource") != status_key[1]:
            return
        diagnostics = report.get("diagnostics")
        overall = (
            str(diagnostics.get("overall", "UNKNOWN"))
            if isinstance(diagnostics, dict)
            else "UNKNOWN"
        )
        self.instrument_status_by_key[status_key] = {
            "report": report,
            "overall": overall,
            "primary_issue": primary_diagnostic_issue(report),
            "checked_at": str(report.get("created_at", "—")),
            "evidence": str(evidence),
        }
        self.current_diagnostic_status_key = status_key
        self._refresh_inventory_status_tree()

    def _cache_diagnostic_failure(self, message: object, evidence: object) -> None:
        snapshot = self.inventory_snapshot
        status_key = getattr(self, "pending_diagnostic_status_key", None)
        if status_key is None:
            entry = self._current_inventory_entry()
            if snapshot is not None and entry is not None:
                status_key = instrument_status_key(snapshot, entry)
        if status_key is None:
            return
        self.instrument_status_by_key[status_key] = {
            "report": None,
            "overall": "BLOCKED",
            "primary_issue": f"BLOCKED diagnostic recorder · {message}",
            "checked_at": now_iso(),
            "evidence": str(evidence),
        }
        self.current_diagnostic_status_key = status_key
        self._refresh_inventory_status_tree()

    def _show_retained_status(self) -> bool:
        record = self._status_record_for_entry(self._current_inventory_entry())
        if record is None:
            return False
        report = record.get("report")
        if isinstance(report, dict):
            self._show_configuration(report)
            diagnostics = report.get("diagnostics")
            diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
            summary = diagnostics.get("summary")
            summary = summary if isinstance(summary, dict) else {}
            self.diagnostic_state_var.set("State: STALE · retained display only")
            self.readiness_var.set(
                "Readiness: STALE · last "
                f"{diagnostics.get('overall', 'UNKNOWN')} · "
                f"PASS {summary.get('pass', 0)} · WARN {summary.get('warn', 0)} · "
                f"BLOCKED {summary.get('blocked', 0)} · UNKNOWN {summary.get('unknown', 0)}"
            )
            axes_text = format_health_axes(diagnostics.get("health_axes", {}))
            self.health_axes_var.set(f"Last {axes_text.lower()} · STALE")
        else:
            self._clear_report_trees()
            self.diagnostic_state_var.set("State: STALE · last diagnostic failed")
            self.readiness_var.set("Readiness: STALE · last result BLOCKED")
            self.health_axes_var.set("Health axes: unavailable from failed diagnostic")
        self.primary_issue_var.set(
            f"Primary issue (STALE): {record.get('primary_issue', 'not recorded')}"
        )
        self.evidence_var.set(f"Retained evidence: {record.get('evidence', '—')}")
        self.status_var.set(
            "Showing the selected instrument's retained result as STALE; run Read-Only Diagnostics to re-authorize current state."
        )
        self.start_button.configure(state="disabled")
        self.single_button.configure(state="disabled")
        return True

    def _current_target_key(self) -> str:
        """Compatibility helper; production selection is inventory-driven."""

        target = self._current_target()
        if target is not None:
            return target.key
        if not hasattr(self, "selected_target"):
            return DEFAULT_TARGET_KEY
        raise RuntimeError("No recognised instrument is selected from inventory")

    def _install_inventory_snapshot(
        self,
        snapshot: InventorySnapshot | None,
        *,
        preferred_resource: str | None = None,
    ) -> None:
        self.inventory_snapshot = snapshot
        self.inventory_usable = bool(
            snapshot is not None
            and snapshot.refresh_error is None
            and snapshot.manager_close_error is None
        )
        self.inventory_label_to_entry = {}
        self.selected_inventory_label = None
        self.selected_target = None
        entries = snapshot.entries if snapshot is not None else ()
        for entry in entries:
            label = inventory_entry_label(entry)
            if label in self.inventory_label_to_entry:
                label = f"{label} · {entry.resource}"
            self.inventory_label_to_entry[label] = entry
        labels = tuple(self.inventory_label_to_entry)
        self.target_combo.configure(
            values=labels,
            state="readonly" if labels else "disabled",
        )

        selected_label = None
        preferred = (preferred_resource or "GPIB0::6::INSTR").upper()
        for label, entry in self.inventory_label_to_entry.items():
            if entry.resource.upper() == preferred:
                selected_label = label
                break
        if selected_label is None and labels:
            selected_label = labels[0]
        self.target_var.set(selected_label or "")
        self.selected_inventory_label = selected_label
        if selected_label is not None:
            self.selected_target = target_from_inventory_entry(
                self.inventory_label_to_entry[selected_label]
            )

        if snapshot is None:
            self.inventory_status_var.set(
                "Inventory not loaded · Real mode requires explicit Refresh Inventory"
            )
        else:
            counts = snapshot.counts
            unresolved = (
                counts.unknown_model_count
                + counts.malformed_identity_count
                + counts.command_set_ambiguous_count
                + counts.io_error_count
            )
            self.inventory_status_var.set(
                f"Inventory {snapshot.snapshot_id[:8]} · {counts.filtered_gpib_count} GPIB "
                f"resource(s) · {counts.recognized_profile_count} recognised · "
                f"{unresolved} unresolved/error"
            )
        self.config_button.configure(
            state=(
                "normal"
                if self.selected_target is not None and self.inventory_usable
                else "disabled"
            )
        )
        target = self.selected_target
        fault_scenarios = (
            fault_scenarios_for_target(target)
            if target is not None
            else ("nominal",)
        )
        if self.selected_fault not in fault_scenarios:
            self.selected_fault = "nominal"
            self.fault_var.set("nominal")
        self.fault_combo.configure(
            values=fault_scenarios,
            state=(
                "readonly"
                if target is not None and self.mode_var.get() == "simulate"
                else "disabled"
            ),
        )
        self._refresh_inventory_status_tree()
        self._update_target_presentation()

    def _clear_report_trees(self) -> None:
        for name in ("summary_tree", "config_tree"):
            tree = getattr(self, name, None)
            if tree is not None:
                tree.delete(*tree.get_children())

    def _update_target_presentation(self) -> None:
        target = self._current_target()
        entry = self._current_inventory_entry()
        if target is None:
            if entry is None:
                self.target_detail_var.set(
                    "No inventory target selected. Refresh Inventory to enumerate GPIB0."
                )
            else:
                self.target_detail_var.set(
                    f"Identity-only entry: {entry.resource} · status {entry.status} · "
                    f"{entry.error or entry.profile_resolution}"
                )
            self.safety_scope_var.set(
                "QUERY ONLY · IDENTITY EVIDENCE ONLY · NO MODEL PROFILE OR LIVE ACCESS"
            )
            self.reading_box.configure(text="No approved diagnostic profile")
            self.plot_box.configure(text="GPIB6 voltage plot unavailable")
            return
        profile = profile_for_key(str(target.profile_key))
        self.target_detail_var.set(
            f"Inventory target: {target.resource} · MODEL {target.model} · "
            f"S/N {target.serial} · shared {profile.key} profile · "
            f"{profile.command_set.value}"
        )
        if target.live_supported:
            self.safety_scope_var.set(
                "QUERY ONLY · MODEL-SPECIFIC BEGIN/END SNAPSHOT + *IDN?/FETCh? LIVE · "
                "NO RESET, ABORT, INIT, TRIGGER, OR CONFIGURATION WRITES"
            )
            self.reading_box.configure(text="Latest voltage · GPIB6 2182A")
            self.plot_box.configure(
                text="Voltage versus elapsed host time · latest 10 minutes"
            )
        else:
            self.safety_scope_var.set(
                "QUERY ONLY · MODEL-SPECIFIC DIAGNOSTIC SNAPSHOT · "
                "LIVE DISABLED FOR THIS PROFILE · NO CONFIGURATION WRITES"
            )
            self.reading_box.configure(text="Diagnostics only · no Live transaction")
            self.plot_box.configure(
                text="GPIB6 voltage plot retained as a separate capability"
            )

    def _target_changed(self, _event=None) -> None:
        selected_label = self.target_var.get()
        entry = self.inventory_label_to_entry.get(selected_label)
        if entry is None:
            self.target_var.set(self.selected_inventory_label or "")
            return
        if selected_label == self.selected_inventory_label:
            return
        if getattr(self, "operation_pending", False) or (
            self.worker and self.worker.is_alive()
        ):
            self.target_var.set(self.selected_inventory_label or "")
            self._refresh_inventory_status_tree()
            self.messagebox.showwarning(
                "Diagnostic run active",
                "Wait for the current operation or Pause before changing instrument.",
            )
            return
        self._invalidate_current_diagnostics("target_changed")
        self.selected_inventory_label = selected_label
        self.selected_target = target_from_inventory_entry(entry)
        self.diagnostic_target = None
        self.real_access_confirmed = False
        self.selected_fault = "nominal"
        self.fault_var.set("nominal")
        self.fault_combo.configure(
            values=(
                fault_scenarios_for_target(self.selected_target)
                if self.selected_target is not None
                else ("nominal",)
            ),
            state=(
                "readonly"
                if self.mode_var.get() == "simulate" and self.selected_target is not None
                else "disabled"
            ),
        )
        self.samples.clear()
        self.interventions.clear()
        self.active_intervention = None
        self.reading_var.set(
            "No sample"
            if self.selected_target is not None and self.selected_target.live_supported
            else "Diagnostics only"
        )
        self.raw_var.set("Raw: —")
        self._clear_report_trees()
        self._draw_plot()
        self._update_target_presentation()
        self.config_button.configure(
            state=(
                "normal"
                if self.selected_target is not None and self.inventory_usable
                else "disabled"
            )
        )
        retained_status_shown = self._show_retained_status()
        self._refresh_inventory_status_tree()
        if self.selected_target is not None:
            self.mode_status_var.set(
                f"Selected {self.selected_target.label}; "
                + (
                    "retained result is STALE until diagnostics run again."
                    if retained_status_shown
                    else "run Read-Only Diagnostics."
                )
            )
            if not retained_status_shown:
                self.status_var.set(
                    "Instrument changed; previous readiness was invalidated."
                )
        else:
            self.mode_status_var.set(
                f"Selected {entry.resource}; identity retained but no diagnostic profile is approved."
            )
            self.status_var.set(
                "Diagnostics blocked at identity classification; no model-specific query will be sent."
            )

    def _mode_changed(self, _event=None) -> None:
        if self.worker and self.worker.is_alive():
            self.mode_var.set(self.selected_mode)
            self.messagebox.showwarning("Monitor running", "Pause the live plot before changing mode.")
            return
        self._finalize_diagnostic_run("mode_changed")
        self.selected_mode = self.mode_var.get()
        self.configuration = None
        self.current_diagnostic_status_key = None
        self.pending_diagnostic_status_key = None
        self.run_directory = None
        self.simulation_context = None
        self.stream_id = None
        self.real_access_confirmed = False
        self.diagnostic_target = None
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
        self.health_axes_var.set("Health axes: not evaluated")
        self.primary_issue_var.set("Primary issue: not evaluated")
        self._clear_report_trees()
        if self.selected_mode == "real":
            self.fault_var.set("nominal")
            self.selected_fault = "nominal"
            self.fault_combo.configure(state="disabled")
            self.inventory_evidence_file = None
            self._install_inventory_snapshot(None)
            self.mode_status_var.set(
                "Real VISA selected; no communication yet. Click Refresh Inventory explicitly."
            )
        else:
            self.inventory_evidence_file = None
            self._install_inventory_snapshot(None)
            self.mode_status_var.set(
                "Simulation selected; click Refresh Inventory to load HANDOFF fixtures without VISA communication."
            )

    def _confirm_inventory_refresh(self) -> bool:
        if self.mode_var.get() != "real":
            return True
        return self.messagebox.askyesno(
            "Confirm exclusive GPIB0 inventory access",
            "Confirm all of the following:\n\n"
            "• LabVIEW and every NI MAX instrument/test panel on GPIB0 are closed.\n"
            "• No other process is controlling the GPIB0 bus.\n"
            "• You want one VISA list_resources() call now.\n"
            "• For each returned GPIB0 primary-address INSTR resource only, the program may send *IDN? exactly once.\n\n"
            "No missing address is constructed or probed. There is no retry, clear, reset, trigger, or configuration write.",
        )

    def _refresh_inventory(self) -> None:
        if getattr(self, "operation_pending", False) or (
            self.worker and self.worker.is_alive()
        ):
            return
        if not self._confirm_inventory_refresh():
            return
        self.real_access_confirmed = False
        self._invalidate_current_diagnostics("inventory_refresh_requested")
        self.diagnostic_target = None
        mode = self.mode_var.get()
        owner = self._begin_operation(kind="inventory", mode=mode)
        self.status_var.set(
            "Writing inventory refresh plan before the first identity query..."
        )
        self.worker = threading.Thread(
            target=self._inventory_worker,
            args=(owner, mode, self.output_root),
            daemon=True,
        )
        self.worker.start()

    def _inventory_worker(
        self,
        owner: OperationOwner,
        mode: str,
        output_root: Path,
    ) -> None:
        run_directory: Path | None = None
        plan_file: Path | None = None
        snapshot: InventorySnapshot | None = None
        phase = "preflight"
        try:
            run_directory = output_root / (
                f"{timestamp_name()}-{mode}-inventory-{APP_RELEASE_TAG}"
            )
            run_directory.mkdir(parents=True, exist_ok=False)
            plan_file = run_directory / "inventory-refresh-plan.json"
            write_json_atomic(
                plan_file,
                {
                    "created_at": now_iso(),
                    "operation": "explicit query-only GPIB0 inventory refresh",
                    "mode": mode,
                    "safety": {
                        "evidence_committed_before_first_query": True,
                        "list_resources_calls": 1 if mode == "real" else 0,
                        "resource_filter": "GPIB0 primary addresses 1-30, INSTR only",
                        "identity_query": "*IDN?",
                        "identity_queries_per_resource_max": 1,
                        "timeout_ms": 2000,
                        "sequential": True,
                        "retry": False,
                        "address_construction_or_probe": False,
                        "instrument_writes": False,
                    },
                },
            )
            phase = "scan"
            snapshot = (
                refresh_inventory()
                if mode == "real"
                else build_simulated_inventory()
            )
            phase = "persist"
            snapshot_file = run_directory / "inventory-snapshot.json"
            write_json_atomic(snapshot_file, snapshot.as_dict())
            self.events.put(
                (
                    owner,
                    "inventory",
                    (snapshot, run_directory, plan_file, snapshot_file),
                )
            )
        except Exception as exc:
            self.events.put(
                (
                    owner,
                    "inventory_error",
                    (
                        f"{type(exc).__name__}: {exc}",
                        phase,
                        run_directory,
                        plan_file,
                        snapshot,
                    ),
                )
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
        self.diagnostic_target = None
        self.current_diagnostic_status_key = None
        self.pending_diagnostic_status_key = None
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
        self.health_axes_var.set("Health axes: not evaluated")
        self.primary_issue_var.set("Primary issue: not evaluated")
        self._clear_report_trees()
        self._refresh_inventory_status_tree()

    def _confirm_real_access(self) -> bool:
        if self.mode_var.get() != "real" or self.real_access_confirmed:
            return True
        target = self._current_target()
        if target is None:
            self.messagebox.showerror(
                "Inventory required",
                "Refresh Inventory and select a recognised instrument first.",
            )
            return False
        confirmed = self.messagebox.askyesno(
            "Confirm exclusive GPIB access",
            "Confirm all of the following:\n\n"
            f"• LabVIEW is stopped and has released {target.resource}.\n"
            f"• NI MAX test panels for {target.resource} are closed.\n"
            f"• Expected identity is MODEL {target.model}, S/N {target.serial}.\n"
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
        if getattr(self, "operation_pending", False) or (
            self.worker and self.worker.is_alive()
        ):
            return
        target = self._current_target()
        snapshot = self.inventory_snapshot
        if target is None or snapshot is None or not self.inventory_usable:
            self.messagebox.showwarning(
                "Inventory target required",
                "Refresh Inventory and select an instrument with an approved model profile.",
            )
            return
        if self.mode_var.get() == "real" and snapshot.source != "real":
            self.messagebox.showerror(
                "Real inventory required",
                "The current target is not from a real Inventory Refresh.",
            )
            return
        if not self._confirm_real_access():
            return
        self._finalize_diagnostic_run("new_diagnostic_requested")
        self.configuration = None
        current_entry = self._current_inventory_entry()
        self.current_diagnostic_status_key = None
        self.pending_diagnostic_status_key = (
            instrument_status_key(snapshot, current_entry)
            if current_entry is not None
            else None
        )
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
        self.health_axes_var.set("Health axes: evaluating")
        self.primary_issue_var.set("Primary issue: evaluating")
        self.diagnostic_state_var.set("State: VERIFYING_IDENTITY (pending recorder preflight)")
        self.status_var.set("Creating run evidence before the first allow-listed query...")
        mode = self.mode_var.get()
        owner = self._begin_operation(
            kind="diagnostic",
            mode=mode,
            target_key=target.key,
            inventory_snapshot_id=snapshot.snapshot_id,
        )
        fault_scenario = self.fault_var.get() if mode == "simulate" else "nominal"
        self.selected_fault = fault_scenario
        output_root = self.output_root
        self.diagnostic_target = target
        self.worker = threading.Thread(
            target=self._configuration_worker,
            args=(
                owner,
                mode,
                fault_scenario,
                output_root,
                target,
                snapshot.snapshot_id,
                snapshot,
                self.inventory_evidence_file,
            ),
            daemon=True,
        )
        self.worker.start()

    def _configuration_worker(
        self,
        owner: OperationOwner,
        mode: str,
        fault_scenario: str,
        output_root: Path,
        target_ref: str | InstrumentTarget | None = None,
        inventory_snapshot_id: str | None = None,
        inventory_snapshot: InventorySnapshot | None = None,
        inventory_snapshot_file: Path | None = None,
    ) -> None:
        run_directory: Path | None = None
        evidence_file: Path | None = None
        journal: RunJournal | None = None
        simulation_context: SimulationContext | None = None
        try:
            if mode == "real" and not isinstance(target_ref, InstrumentTarget):
                raise ValueError(
                    "real diagnostic worker requires an InstrumentTarget from inventory"
                )
            if mode == "real" and (
                inventory_snapshot is None or inventory_snapshot_id is None
            ):
                raise ValueError(
                    "real diagnostic worker requires the frozen inventory snapshot"
                )
            target = resolve_target(target_ref or DEFAULT_TARGET_KEY)
            if target.profile_key is None:
                raise ValueError(f"target has no approved profile: {target.label}")
            profile = profile_for_key(target.profile_key)
            query_specs = diagnostic_queries_for_profile(target.profile_key)
            observation_plan = operation_observation_plan(target.profile_key)
            allowed_queries = allowed_queries_for_target(target)
            if mode == "real" and fault_scenario != "nominal":
                raise ValueError("fault injection is forbidden in real mode")
            if fault_scenario not in fault_scenarios_for_target(target):
                raise ValueError(
                    f"fault scenario {fault_scenario!r} is not valid for {target.key!r}"
                )
            run_directory = output_root / (
                f"{timestamp_name()}-{mode}-{target.key}-diagnostic-{APP_RELEASE_TAG}"
            )
            run_directory.mkdir(parents=True, exist_ok=False)
            inventory_reference_file: Path | None = None
            inventory_payload_hash: str | None = None
            source_inventory_file_hash: str | None = None
            if inventory_snapshot is not None:
                if inventory_snapshot.snapshot_id != inventory_snapshot_id:
                    raise ValueError("inventory snapshot ID does not match frozen snapshot")
                if inventory_snapshot.source != mode:
                    raise ValueError(
                        "inventory snapshot source does not match diagnostic mode"
                    )
                snapshot_targets = tuple(
                    target_from_inventory_entry(entry)
                    for entry in inventory_snapshot.entries
                )
                if target not in snapshot_targets:
                    raise ValueError("selected target is not present in the frozen inventory snapshot")
                inventory_payload = inventory_snapshot.as_dict()
                inventory_payload_hash = json_payload_sha256(inventory_payload)
                if mode == "real" and inventory_snapshot_file is None:
                    raise ValueError(
                        "real diagnostic requires persisted inventory snapshot evidence"
                    )
                if inventory_snapshot_file is not None:
                    if not inventory_snapshot_file.is_file():
                        raise FileNotFoundError(
                            f"inventory snapshot evidence is missing: {inventory_snapshot_file}"
                        )
                    source_inventory_bytes = inventory_snapshot_file.read_bytes()
                    source_inventory_file_hash = hashlib.sha256(
                        source_inventory_bytes
                    ).hexdigest()
                    try:
                        source_inventory_payload = json.loads(
                            source_inventory_bytes.decode("utf-8-sig")
                        )
                    except Exception as exc:
                        raise ValueError(
                            "persisted inventory snapshot is not valid JSON"
                        ) from exc
                    if (
                        json_payload_sha256(source_inventory_payload)
                        != inventory_payload_hash
                    ):
                        raise ValueError(
                            "persisted inventory snapshot does not match frozen snapshot"
                        )
                inventory_reference_file = (
                    run_directory / "inventory-snapshot-reference.json"
                )
                write_json_atomic(
                    inventory_reference_file,
                    {
                        "schema_version": 1,
                        "created_at": now_iso(),
                        "inventory_snapshot_id": inventory_snapshot_id,
                        "source_file": (
                            str(inventory_snapshot_file)
                            if inventory_snapshot_file is not None
                            else None
                        ),
                        "source_file_sha256": source_inventory_file_hash,
                        "snapshot_payload_sha256": inventory_payload_hash,
                        "snapshot": inventory_payload,
                    },
                )
            if mode == "simulate":
                simulation_context = SimulationContext(fault_scenario, allowed_queries)
            target_manifest = asdict(target)
            target_manifest["inventory_snapshot_id"] = inventory_snapshot_id
            target_manifest["inventory_snapshot_reference"] = (
                inventory_reference_file.name
                if inventory_reference_file is not None
                else None
            )
            target_manifest["inventory_snapshot_payload_sha256"] = (
                inventory_payload_hash
            )
            target_manifest["inventory_source_file_sha256"] = (
                source_inventory_file_hash
            )
            journal = RunJournal(
                run_directory,
                mode=mode,
                allowed_queries=allowed_queries,
                command_set=profile.command_set.value,
                profile_id=profile_id_for(target),
                target=target_manifest,
                live_supported=target.live_supported,
                live_authorized=is_approved_gpib6_live_target(target),
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
                payload={
                    "target_key": target.key,
                    "inventory_snapshot_id": inventory_snapshot_id,
                    "inventory_snapshot_reference": (
                        inventory_reference_file.name
                        if inventory_reference_file is not None
                        else None
                    ),
                    "candidate_query_count": (
                        len(query_specs)
                        + len(profile.snapshot_end_names)
                        + len(observation_plan)
                    ),
                },
            )
            query_specs_by_name = {spec.name: spec for spec in query_specs}
            journal.record_event(
                "query_plan_committed",
                reason_code="QUERY_PLAN_COMMITTED",
                payload={
                    "target_key": target.key,
                    "inventory_snapshot_id": inventory_snapshot_id,
                    "command_set": profile.command_set.value,
                    "queries": [
                        {
                            "name": spec.name,
                            "command": spec.command,
                            "condition": spec.condition,
                            "phase": "snapshot_begin",
                        }
                        for spec in query_specs
                    ]
                    + [
                        {
                            "name": f"snapshot_end.{name}",
                            "source_name": name,
                            "command": query_specs_by_name[name].command,
                            "condition": None,
                            "phase": "snapshot_end",
                        }
                        for name in profile.snapshot_end_names
                    ]
                    + [
                        {
                            "name": name,
                            "command": OPERATION_CONDITION_COMMAND,
                            "condition": None,
                            "phase": "operation_observation",
                            "scheduled_offset_seconds": scheduled_offset,
                        }
                        for name, scheduled_offset in observation_plan
                    ],
                    "consistency_names": list(profile.consistency_names),
                    "operation_observation": (
                        {
                            "command": OPERATION_CONDITION_COMMAND,
                            "dedicated_duration_seconds": OPERATION_OBSERVATION_OFFSETS_SECONDS[-1],
                            "dedicated_sample_count": len(observation_plan),
                            "scheduled_offsets_seconds": [
                                offset for _name, offset in observation_plan
                            ],
                        }
                        if observation_plan
                        else None
                    ),
                    "query_only": True,
                },
            )
            report = collect_configuration(
                mode,
                simulation_context,
                recorder_ready=True,
                target=target,
            )
            report["inventory_snapshot_id"] = inventory_snapshot_id
            report["inventory_snapshot_reference"] = (
                inventory_reference_file.name
                if inventory_reference_file is not None
                else None
            )
            report["inventory_snapshot_payload_sha256"] = inventory_payload_hash
            report["inventory_source_file_sha256"] = source_inventory_file_hash
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
                str(report.get("values", {}).get("identity", "")),
                target,
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
            if bool(diagnostics.get("diagnostics_acceptable")):
                journal.transition(
                    DiagnosticState.OBSERVE_READY,
                    reason_code="DIAGNOSTICS_OBSERVE_READY",
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
                    owner,
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
                                "target_key": (
                                    target.key
                                    if "target" in locals()
                                    else str(target_ref)
                                ),
                                "inventory_snapshot_id": inventory_snapshot_id,
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
                    owner,
                    "configuration_error",
                    (message, run_directory, failure_file, journal, simulation_context),
                )
            )

    def _start_stream(self) -> None:
        if getattr(self, "operation_pending", False) or (
            self.worker and self.worker.is_alive()
        ):
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
        target = self.diagnostic_target
        if (
            target is None
            or not is_approved_gpib6_live_target(target)
            or not self.configuration.get("capabilities", {}).get("live_supported")
        ):
            self.messagebox.showerror(
                "Live not supported",
                "This instrument profile is diagnostic-only. No Live transaction is defined.",
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
        snapshot_id = self.configuration.get("inventory_snapshot_id")
        owner = self._begin_operation(
            kind="live",
            mode=mode,
            target_key=target.key,
            inventory_snapshot_id=(
                snapshot_id if isinstance(snapshot_id, str) else None
            ),
            run_id=self.diagnostic_run.run_id,
            stream_id=stream_id,
        )
        stream_context = None
        if mode == "simulate":
            scenario = (
                self.simulation_context.scenario_id
                if self.simulation_context is not None
                else self.selected_fault
            )
            stream_context = SimulationContext(
                scenario,
                allowed_queries_for_target(target),
            )
        self.live_running = True
        self.stream_had_error = False
        self.start_button.configure(state="disabled")
        self.config_button.configure(state="disabled")
        self.inventory_button.configure(state="disabled")
        self.target_combo.configure(state="disabled")
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
                owner,
                mode,
                csv_path,
                stream_context,
                self.diagnostic_run,
                stream_id,
                target,
            ),
            daemon=True,
        )
        self.worker.start()

    def _stream_worker(
        self,
        owner: OperationOwner,
        mode: str,
        csv_path: Path,
        simulation_context: SimulationContext | None = None,
        journal: RunJournal | None = None,
        stream_id: str | None = None,
        target: InstrumentTarget | None = None,
    ) -> None:
        sample_count = 0
        stream_started = False
        stream_error: str | None = None
        fault_intervention_end: dict[str, object] | None = None

        try:
            if mode == "real" and simulation_context is not None:
                raise ValueError("fault injection is forbidden in real mode")
            if target is None:
                raise ValueError("Live worker requires an explicit frozen target")
            if not is_approved_gpib6_live_target(target):
                raise ValueError("Live is restricted to the exact approved GPIB6 asset")
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
                    with session_factory(
                        mode,
                        "stream",
                        simulation_context,
                        target=target,
                    ) as session:
                        identity = session.query("*IDN?")
                        if not identity_is_expected(identity, target):
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
                            self.events.put(
                                (
                                    owner,
                                    "sample",
                                    (elapsed, voltage, raw, sample_count),
                                )
                            )
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
                    owner,
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
                    if csv_path.is_file():
                        quality_path = csv_path.with_suffix(".quality.json")
                        write_json_atomic(
                            quality_path,
                            analyze_stream_csv(csv_path),
                        )
                        journal.set_stream_quality(stream_id, quality_path.name)
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
                                owner,
                                "stream_error",
                                {"message": stream_error, "intervention_end": None},
                            )
                        )
                    else:
                        stream_error = f"{stream_error}; stream finalization failed: {finalization_error}"
            self.events.put(
                (
                    owner,
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
        if getattr(self, "operation_pending", False) or (
            self.worker and self.worker.is_alive()
        ):
            return
        if (
            not self.configuration
            or not self.diagnostic_run
            or self.diagnostic_run.state != DiagnosticState.OBSERVE_READY
        ):
            self.messagebox.showwarning("Diagnostics required", "Rerun diagnostics before Single FETCh?.")
            return
        target = self.diagnostic_target
        if (
            target is None
            or not is_approved_gpib6_live_target(target)
            or not self.configuration.get("capabilities", {}).get("live_supported", False)
        ):
            self.messagebox.showerror(
                "Single FETCh? not supported",
                "This instrument profile has no approved Live voltage transaction.",
            )
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
        mode = self.mode_var.get()
        snapshot_id = self.configuration.get("inventory_snapshot_id")
        owner = self._begin_operation(
            kind="single",
            mode=mode,
            target_key=target.key,
            inventory_snapshot_id=(
                snapshot_id if isinstance(snapshot_id, str) else None
            ),
            run_id=self.diagnostic_run.run_id,
        )
        single_context = None
        if mode == "simulate":
            scenario = (
                self.simulation_context.scenario_id
                if self.simulation_context is not None
                else self.selected_fault
            )
            single_context = SimulationContext(
                scenario,
                allowed_queries_for_target(target),
            )
        self.worker = threading.Thread(
            target=self._single_fetch_worker,
            args=(owner, mode, single_context, self.diagnostic_run, target),
            daemon=True,
        )
        self.worker.start()

    def _single_fetch_worker(
        self,
        owner: OperationOwner,
        mode: str,
        simulation_context: SimulationContext | None = None,
        journal: RunJournal | None = None,
        target: InstrumentTarget | None = None,
    ) -> None:
        try:
            if mode == "real" and simulation_context is not None:
                raise ValueError("fault injection is forbidden in real mode")
            if target is None:
                raise ValueError("Single FETCh? worker requires an explicit frozen target")
            if not is_approved_gpib6_live_target(target):
                raise ValueError("Single FETCh? is restricted to the exact approved GPIB6 asset")
            started = time.perf_counter()
            with session_factory(
                mode,
                "stream",
                simulation_context,
                target=target,
            ) as session:
                identity = session.query("*IDN?")
                if not identity_is_expected(identity, target):
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
            self.events.put(
                (owner, "single", (elapsed, voltage, raw, query_ms))
            )
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
            self.events.put((owner, "single_error", message))

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
        self.operation_pending = bool(busy)
        target_ready = bool(self._current_target() is not None and self.inventory_usable)
        self.config_button.configure(
            state="disabled" if busy or not target_ready else "normal"
        )
        self.inventory_button.configure(state="disabled" if busy else "normal")
        self.target_combo.configure(
            state=(
                "disabled"
                if busy or not self.inventory_label_to_entry
                else "readonly"
            )
        )
        self.mode_combo.configure(state="disabled" if busy else "readonly")
        self.fault_combo.configure(
            state=(
                "disabled"
                if busy or self.mode_var.get() == "real" or not target_ready
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
            ) and bool(
                self.diagnostic_target is not None
                and is_approved_gpib6_live_target(self.diagnostic_target)
            )
            self.start_button.configure(state="normal" if ready else "disabled")
            self.single_button.configure(state="normal" if ready else "disabled")

    def _show_configuration(self, report: dict[str, object]) -> None:
        summary_tree = getattr(self, "summary_tree", None)
        if summary_tree is not None:
            summary_tree.delete(*summary_tree.get_children())
            summary = report.get("precision_safety_summary", [])
            diagnostics = report.get("diagnostics", {})
            checks = diagnostics.get("checks", []) if isinstance(diagnostics, dict) else []
            for row in summary if isinstance(summary, list) else []:
                if not isinstance(row, dict):
                    continue
                key = str(row.get("key", ""))
                value = str(row.get("value", "—"))
                source_names = tuple(row.get("source_names", ()))
                status = self._summary_row_status(
                    key,
                    value,
                    source_names,
                    report,
                    checks if isinstance(checks, list) else [],
                )
                summary_tree.insert(
                    "",
                    "end",
                    values=(
                        status,
                        row.get("label", key),
                        value,
                        self._summary_interpretation(key, value),
                    ),
                    tags=(status,),
                )
        self.config_tree.delete(*self.config_tree.get_children())
        transcript = report.get("transcript", [])
        for item in transcript if isinstance(transcript, list) else []:
            if not isinstance(item, dict):
                continue
            if item.get("skipped"):
                self.config_tree.insert(
                    "",
                    "end",
                    values=(
                        "N/A",
                        item.get("name", ""),
                        item.get("reason", "conditional query skipped"),
                        item.get("command", ""),
                    ),
                    tags=("N/A",),
                )
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

    @staticmethod
    def _summary_interpretation(key: str, value: str) -> str:
        meanings = {
            "identity": "Must exactly match the frozen inventory identity.",
            "accuracy_noise": "Actual performance requires calibration, warm-up, wiring, and data characterization.",
            "range": "Observed setting; no sample/device profile is applied yet.",
            "integration": "NPLC/line-frequency integration window, not total sample period.",
            "filter": "Observed noise/time-response setting.",
            "operation_observation": "Short query-only B0 evidence window; it does not identify FULL ACAL or Autozero.",
            "remote_sense": "Model-specific; configured and effective sense are kept distinct.",
            "compliance": "Observed safety limit/state; never adjusted by this GUI.",
            "output": "Safety-critical readback; output is never changed by this GUI.",
        }
        if value == "N/A":
            return "Not provided by this instrument model."
        return meanings.get(key, "Observed read-only configuration evidence.")

    @staticmethod
    def _summary_row_status(
        key: str,
        value: str,
        source_names: tuple[str, ...],
        report: dict[str, object],
        checks: list[object],
    ) -> str:
        if value == "N/A":
            return "N/A"
        if key == "accuracy_noise":
            return "UNKNOWN"
        if "—" in value or "unavailable" in value.lower():
            return "UNKNOWN"
        values = report.get("values", {})
        target_key = str(report.get("target_key", DEFAULT_TARGET_KEY))
        if key == "output" and isinstance(values, dict):
            raw = values.get("output_enabled", values.get("source_output"))
            if str(raw).strip().upper() in {"1", "ON", "SMU.ON"}:
                return "BLOCKED"
        if key == "compliance" and isinstance(values, dict):
            tripped_names = ("current_limit_tripped", "voltage_limit_tripped")
            if any(str(values.get(name, "0")).strip().upper() in {"1", "ON", "SMU.ON"} for name in tripped_names):
                return "BLOCKED"
            if target_key.startswith("6221-"):
                try:
                    if int(float(str(values.get("measurement_condition", "0")))) & (1 << 3):
                        return "BLOCKED"
                except (TypeError, ValueError):
                    return "UNKNOWN"
        relevant_statuses: list[str] = []
        for check in checks:
            if not isinstance(check, dict):
                continue
            check_id = str(check.get("check_id", ""))
            if key == "identity" and check_id.startswith("identity."):
                relevant_statuses.append(str(check.get("status", "UNKNOWN")))
            elif any(name and name in check_id for name in source_names):
                relevant_statuses.append(str(check.get("status", "UNKNOWN")))
        for status in ("BLOCKED", "UNKNOWN", "WARN"):
            if status in relevant_statuses:
                return status
        if relevant_statuses and all(status == "PASS" for status in relevant_statuses):
            return "PASS"
        return "READ"

    def _drain_events(self) -> None:
        try:
            while True:
                owner, event, payload = self.events.get_nowait()
                if owner != self.active_operation:
                    continue
                if event == "inventory":
                    snapshot, run_directory, plan_file, snapshot_file = payload
                    previous_target = self._current_target()
                    preferred_resource = (
                        previous_target.resource if previous_target is not None else None
                    )
                    self.inventory_evidence_file = snapshot_file
                    self._install_inventory_snapshot(
                        snapshot,
                        preferred_resource=preferred_resource,
                    )
                    self._finish_operation(owner)
                    counts = snapshot.counts
                    self.evidence_var.set(
                        f"Inventory plan: {plan_file} · Snapshot: {snapshot_file}"
                    )
                    if snapshot.refresh_error or snapshot.manager_close_error:
                        error = snapshot.refresh_error or snapshot.manager_close_error
                        self.mode_status_var.set(
                            "Inventory refresh evidence retained, but the snapshot is blocked."
                        )
                        self.status_var.set(f"Inventory refresh blocked: {error}")
                        if not self.closing:
                            self.messagebox.showerror(
                                "Inventory refresh blocked",
                                f"{error}\n\nNo model-specific diagnostic query is enabled.",
                            )
                    else:
                        self.mode_status_var.set(
                            (
                                "Real inventory complete: one list_resources() call and at most one *IDN? per retained resource."
                                if snapshot.source == "real"
                                else "Simulation inventory refreshed; no VISA communication."
                            )
                        )
                        self.status_var.set(
                            f"Inventory complete: {counts.filtered_gpib_count} GPIB resource(s), "
                            f"{counts.recognized_profile_count} recognised profile(s)."
                        )
                elif event == "inventory_error":
                    message, phase, run_directory, plan_file, snapshot = payload
                    self.inventory_evidence_file = None
                    self._install_inventory_snapshot(
                        snapshot if phase == "persist" else None
                    )
                    self.inventory_usable = False
                    self.config_button.configure(state="disabled")
                    self._finish_operation(owner)
                    if phase == "preflight":
                        explanation = (
                            "Inventory was not run because its evidence preflight failed."
                        )
                    elif phase == "scan":
                        explanation = (
                            "Inventory scan did not return normally after the plan was committed."
                        )
                    else:
                        explanation = (
                            "Inventory identity queries completed, but the snapshot could not be persisted; "
                            "in-memory entries are view-only."
                        )
                    self.mode_status_var.set(explanation)
                    self.status_var.set(f"Inventory failure: {message}")
                    self.evidence_var.set(
                        f"Partial inventory directory: {run_directory or 'not created'} · "
                        f"plan: {plan_file or 'not written'} · failed phase: {phase}"
                    )
                    if not self.closing:
                        self.messagebox.showerror("Inventory refresh failed", str(message))
                elif event == "configuration":
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
                    derived_poll = report.get("derived_poll_interval_s")
                    self.poll_interval_s = (
                        float(derived_poll) if derived_poll is not None else 0.5
                    )
                    self._show_configuration(report)
                    self._cache_diagnostic_status(
                        report,
                        evidence=evidence_file,
                    )
                    self.pending_diagnostic_status_key = None
                    diagnostics = report.get("diagnostics", {})
                    ready = bool(
                        isinstance(diagnostics, dict)
                        and diagnostics.get("can_start_live")
                        and journal.state == DiagnosticState.OBSERVE_READY
                    )
                    diagnostics_acceptable = bool(
                        isinstance(diagnostics, dict)
                        and diagnostics.get("diagnostics_acceptable")
                        and journal.state == DiagnosticState.OBSERVE_READY
                    )
                    live_supported = bool(
                        report.get("capabilities", {}).get("live_supported")
                    )
                    self._finish_operation(owner)
                    live_ready = bool(
                        ready
                        and self.diagnostic_target is not None
                        and is_approved_gpib6_live_target(self.diagnostic_target)
                    )
                    self.start_button.configure(
                        state="normal" if live_ready else "disabled"
                    )
                    self.single_button.configure(
                        state="normal" if live_ready else "disabled"
                    )
                    summary = diagnostics.get("summary", {}) if isinstance(diagnostics, dict) else {}
                    self.diagnostic_state_var.set(f"State: {journal.state.value}")
                    self.readiness_var.set(
                        "Readiness: "
                        f"{diagnostics.get('overall', 'UNKNOWN')} · "
                        f"PASS {summary.get('pass', 0)} · WARN {summary.get('warn', 0)} · "
                        f"BLOCKED {summary.get('blocked', 0)} · UNKNOWN {summary.get('unknown', 0)}"
                    )
                    self.health_axes_var.set(
                        format_health_axes(diagnostics.get("health_axes", {}))
                    )
                    self.primary_issue_var.set(
                        f"Primary issue: {primary_diagnostic_issue(report)}"
                    )
                    self.status_var.set(
                        "Diagnostics complete: "
                        f"{report.get('query_plan', {}).get('executed_count', len(report['values']))}/"
                        f"{report.get('query_plan', {}).get('candidate_count', len(report['values']))} "
                        "candidate reads executed; "
                        + (
                            f"poll interval {self.poll_interval_s:.3f} s; Live "
                            f"{'enabled' if live_ready else 'blocked'}."
                            if live_supported
                            else (
                                "diagnostics accepted; Live unsupported for this profile."
                                if diagnostics_acceptable
                                else "diagnostics blocked; Live unsupported for this profile."
                            )
                        )
                    )
                    self.evidence_var.set(
                        f"Manifest: {journal.manifest_path} · Snapshot: {evidence_file} · "
                        f"Inventory ref: "
                        f"{run_directory / str(report.get('inventory_snapshot_reference')) if report.get('inventory_snapshot_reference') else '—'} · "
                        f"Interventions: {journal.interventions_path}"
                    )
                    self.mode_status_var.set(
                        f"Real query-only VISA access confirmed for {report['resource']}."
                        if report["mode"] == "real"
                        else (
                            "Simulation diagnostic loaded; no VISA communication · fault scenario: "
                            f"{report.get('fault_injection', {}).get('scenario', 'nominal')}"
                        )
                    )
                elif event == "configuration_error":
                    message, run_directory, failure_file, journal, simulation_context = payload
                    self.recording_fault_latched = True
                    self._cache_diagnostic_failure(
                        message,
                        failure_file or run_directory or "partial evidence unavailable",
                    )
                    self.pending_diagnostic_status_key = None
                    self.configuration = None
                    self.diagnostic_target = None
                    self.run_directory = run_directory
                    self.diagnostic_run = journal
                    self.simulation_context = simulation_context
                    self._clear_report_trees()
                    self._finish_operation(owner)
                    self.start_button.configure(state="disabled")
                    self.single_button.configure(state="disabled")
                    committed_state = journal.state.value if journal is not None else "DISCONNECTED"
                    self.diagnostic_state_var.set(
                        f"State: FAULT_LATCHED (last committed: {committed_state})"
                    )
                    self.readiness_var.set("Readiness: BLOCKED · diagnostic evidence recorder failed")
                    self.health_axes_var.set("Health axes: unavailable · evidence recorder failed")
                    self.primary_issue_var.set(
                        f"Primary issue: BLOCKED diagnostic recorder · {message}"
                    )
                    self.status_var.set(f"Diagnostic failure: {message}")
                    if journal is not None:
                        self.evidence_var.set(
                            f"Partial manifest: {journal.manifest_path} · failure: {failure_file or 'not written'}"
                        )
                    elif run_directory is not None:
                        self.evidence_var.set(f"Partial diagnostic directory: {run_directory}")
                    if not self.closing:
                        self.messagebox.showerror("Instrument diagnostic failed", str(message))
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
                    self._finish_operation(owner)
                    if self.diagnostic_run is not None:
                        self.diagnostic_state_var.set(
                            f"State: {self.diagnostic_run.state.value}"
                        )
                elif event == "single_error":
                    self._finish_operation(owner)
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
                    self._finish_operation(owner)
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
                    if payload.get("stream_id") != self.stream_id:
                        continue
                    self.intervention_ready = False
                    self.mark_intervention_button.configure(state="disabled")
                    self.live_running = False
                    self.pause_button.configure(state="disabled")
                    self.mark_intervention_button.configure(state="disabled")
                    self.clear_button.configure(state="normal")
                    self._finish_operation(owner)
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

        def draw_y_axis_label(text: str) -> None:
            try:
                canvas.create_text(
                    15,
                    height / 2,
                    text=text,
                    angle=90,
                    fill="#33404d",
                )
            except self.tk.TclError:
                canvas.create_text(
                    30,
                    height / 2,
                    text=text,
                    fill="#33404d",
                )

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
            draw_y_axis_label("Voltage")
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
        draw_y_axis_label("Voltage (V)")

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
        self.inventory_button.configure(state="disabled")
        self.target_combo.configure(state="disabled")
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
