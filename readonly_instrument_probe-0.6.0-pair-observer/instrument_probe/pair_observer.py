"""Manual-refresh, query-only observer for one 6221/2182A pair.

The operator owns every physical action: wiring, front-panel configuration,
output state, current polarity, trigger, and experiment start/stop.  This module
only takes explicit snapshots after a GUI button press.  It never polls.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import __version__
from .catalog import PROFILES, Profile, QuerySpec
from .collector import collect, host_environment
from .lab_setup import validate_confirmed_target
from .production import production_host_readiness, require_production_host
from .transports import PyVisaQueryTransport, SimulatedTransport


NANOVOLTMETER_RESOURCES = (
    "GPIB0::6::INSTR",
    "GPIB0::7::INSTR",
)

CURRENT_SOURCE_RESOURCES = (
    "GPIB0::9::INSTR",
    "GPIB0::10::INSTR",
)


# The setup snapshot starts with the 13 and 18 core queries already proven on
# the production instruments, then adds a small resistor-experiment subset.
PAIR_SETUP_QUERY_NAMES = {
    "6221": (
        "identity",
        "scpi_version",
        "power_on_setup",
        "output_enabled",
        "output_low_to_earth",
        "triax_inner_shield",
        "output_response",
        "interlock_closed",
        "current_level_a",
        "current_range_auto",
        "current_range_a",
        "voltage_compliance_v",
        "analog_filter",
        "delta_nv_present",
        "delta_high_a",
        "delta_low_a",
        "delta_delay_s",
        "delta_count",
        "delta_cold_switch",
        "delta_armed",
        "trigger_source",
    ),
    "2182a": (
        "identity",
        "scpi_version",
        "line_frequency_hz",
        "power_on_setup",
        "sense_function",
        "active_channel",
        "nplc",
        "ch1_range_v",
        "ch1_autorange",
        "ch2_range_v",
        "ch2_autorange",
        "ch1_digital_filter",
        "ch1_analog_filter",
        "ch2_digital_filter",
        "ch2_analog_filter",
        "trigger_count",
        "trigger_delay_s",
        "trigger_source",
    ),
}


# A measurement snapshot is deliberately short.  SENS:DATA:LATEST? returns
# the 2182A cached latest value; unlike READ?/MEAS?, it does not initiate a new
# acquisition.  The safety gate continues to block acquisition/buffer reads.
PAIR_MEASUREMENT_QUERY_NAMES = {
    "6221": (
        "identity",
        "output_enabled",
        "interlock_closed",
        "current_level_a",
        "current_range_a",
        "voltage_compliance_v",
        "delta_nv_present",
        "delta_high_a",
        "delta_low_a",
        "delta_armed",
    ),
    "2182a": (
        "identity",
        "sense_function",
        "active_channel",
        "nplc",
        "ch1_range_v",
        "ch1_autorange",
        "ch2_range_v",
        "ch2_autorange",
        "latest_cached_reading",
    ),
}


def _selected_profile(profile_key: str, names: tuple[str, ...]) -> Profile:
    base = PROFILES[profile_key]
    by_name = {item.name: item for item in base.queries}
    missing = [name for name in names if name not in by_name]
    if missing:
        raise RuntimeError(f"pair query names are missing from {profile_key}: {missing}")
    queries: tuple[QuerySpec, ...] = tuple(by_name[name] for name in names)
    simulated = {
        item.command: base.simulated.get(item.command, "0")
        for item in queries
    }
    return Profile(
        key=base.key,
        model=base.model,
        command_set=base.command_set,
        queries=queries,
        simulated=simulated,
    )


PAIR_PROFILES = {
    operation: {
        profile_key: _selected_profile(profile_key, query_names[profile_key])
        for profile_key in ("6221", "2182a")
    }
    for operation, query_names in (
        ("configuration", PAIR_SETUP_QUERY_NAMES),
        ("measurement", PAIR_MEASUREMENT_QUERY_NAMES),
    )
}


def _value(report: dict[str, object], name: str) -> str | None:
    instrument = report.get("instrument")
    if not isinstance(instrument, dict):
        return None
    item = instrument.get(name)
    if isinstance(item, dict):
        value = item.get("value")
    else:
        value = item
    return None if value is None else str(value)


_NUMBER = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?")


def _first_float(value: str | None) -> float | None:
    if value is None:
        return None
    match = _NUMBER.search(value.strip())
    if match is None:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _report_ok(report: dict[str, object]) -> bool:
    safety = report.get("safety")
    return bool(
        isinstance(safety, dict)
        and not safety.get("stopped_after_first_io_error")
        and not safety.get("stopped_after_identity_mismatch")
    )


def _pair_summary(
    report_6221: dict[str, object],
    report_2182a: dict[str, object],
    operation: str,
    local_metadata: dict[str, str],
) -> dict[str, object]:
    output_raw = _value(report_6221, "output_enabled")
    output = "OFF" if output_raw == "0" else "ON" if output_raw == "1" else output_raw or "—"
    interlock_raw = _value(report_6221, "interlock_closed")
    interlock = "Closed" if interlock_raw == "1" else "Open / tripped" if interlock_raw == "0" else interlock_raw or "—"
    current_raw = _value(report_6221, "current_level_a")
    voltage_raw = _value(report_2182a, "latest_cached_reading")
    delta_armed_raw = _value(report_6221, "delta_armed")

    current_a = _first_float(current_raw)
    voltage_v = _first_float(voltage_raw)
    delta_armed = delta_armed_raw in {"1", "ON"}
    resistance_ohm: float | None = None
    calculation_status = "Not requested for a configuration snapshot."

    if operation == "measurement":
        if not _report_ok(report_6221) or not _report_ok(report_2182a):
            calculation_status = "Not calculated because one instrument did not complete its query set."
        elif output != "ON":
            calculation_status = "Not calculated because the 6221 output is OFF."
        elif delta_armed:
            calculation_status = (
                "Not calculated from V/I because 6221 Delta mode is armed; "
                "the cached 2182A value cannot be unambiguously matched to one polarity."
            )
        elif current_a is None or current_a == 0:
            calculation_status = "Not calculated because the programmed current is missing or zero."
        elif voltage_v is None:
            calculation_status = "Not calculated because the cached 2182A voltage is unavailable."
        else:
            resistance_ohm = voltage_v / current_a
            calculation_status = (
                "Local estimate V/I from one manually requested snapshot; "
                "this is not an accuracy pass/fail result."
            )

    nominal_ohm = _first_float(local_metadata.get("nominal_resistance_ohm"))
    absolute_error_ohm: float | None = None
    relative_error_percent: float | None = None
    if resistance_ohm is not None and nominal_ohm not in (None, 0):
        absolute_error_ohm = resistance_ohm - nominal_ohm
        relative_error_percent = absolute_error_ohm / nominal_ohm * 100.0

    warnings: list[str] = []
    if output == "ON":
        warnings.append("6221 output is ON; this was set manually, not by the observer.")
    if interlock_raw == "0":
        warnings.append("6221 interlock is open / tripped.")
    if not _report_ok(report_6221):
        warnings.append("6221 did not complete every selected query.")
    if not _report_ok(report_2182a):
        warnings.append(
            "2182A did not complete every selected query; if Delta mode owns it over RS-232, "
            "its GPIB interface may be unavailable."
        )

    return {
        "status": "warning" if warnings else "passed",
        "warnings": warnings,
        "6221": {
            "output": output,
            "interlock": interlock,
            "programmed_current_a": current_raw,
            "current_range_a": _value(report_6221, "current_range_a"),
            "voltage_compliance_v": _value(report_6221, "voltage_compliance_v"),
            "delta_nv_present": _value(report_6221, "delta_nv_present"),
            "delta_high_a": _value(report_6221, "delta_high_a"),
            "delta_low_a": _value(report_6221, "delta_low_a"),
            "delta_armed": delta_armed_raw,
        },
        "2182a": {
            "sense_function": _value(report_2182a, "sense_function"),
            "active_channel": _value(report_2182a, "active_channel"),
            "nplc": _value(report_2182a, "nplc"),
            "latest_cached_voltage_v": voltage_raw,
        },
        "calculation": {
            "resistance_ohm": resistance_ohm,
            "nominal_resistance_ohm": nominal_ohm,
            "absolute_error_ohm": absolute_error_ohm,
            "relative_error_percent": relative_error_percent,
            "status": calculation_status,
        },
    }


def _validate_pair(nanovoltmeter_resource: str, current_source_resource: str) -> None:
    if nanovoltmeter_resource not in NANOVOLTMETER_RESOURCES:
        raise ValueError(f"2182A resource is not selectable: {nanovoltmeter_resource!r}")
    if current_source_resource not in CURRENT_SOURCE_RESOURCES:
        raise ValueError(f"6221 resource is not selectable: {current_source_resource!r}")
    validate_confirmed_target("2182a", nanovoltmeter_resource, "core")
    validate_confirmed_target("6221", current_source_resource, "core")


def _write_new_json(path: Path, data: object) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2, ensure_ascii=False)
        stream.write("\n")


def _new_run_directory(output_root: Path, mode: str, operation: str) -> Path:
    output_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    run_dir = output_root / f"{stamp}-{'real' if mode == 'real' else 'simulation'}-pair-{operation}"
    run_dir.mkdir(exist_ok=False)
    return run_dir


def _collect_one(
    profile: Profile,
    resource: str,
    mode: str,
    timeout_ms: int,
    real_transport_factory,
) -> dict[str, object]:
    transport = None
    report: dict[str, object] | None = None
    try:
        if mode == "real":
            transport = real_transport_factory(profile, resource, timeout_ms=timeout_ms)
        else:
            transport = SimulatedTransport(profile)
            transport.resource_name = resource
        report = collect(profile, transport, "full")
        report["scope"] = "pair_observer_allowlist"
    except Exception as exc:
        report = {
            "schema_version": 2,
            "profile": profile.key,
            "instrument_model": profile.model,
            "command_set": profile.command_set,
            "scope": "pair_observer_allowlist",
            "transport": getattr(transport, "backend_name", "not opened"),
            "visa_resource": resource,
            "safety": {
                "query_only": True,
                "exact_allowlist": True,
                "generic_write_api_exposed": False,
                "acquisition_trigger_queries_blocked": True,
                "event_and_error_queue_queries_blocked": True,
                "stopped_after_first_io_error": True,
                "stopped_after_identity_mismatch": False,
            },
            "host_environment": host_environment(),
            "instrument": {},
            "transcript": [
                {
                    "name": "session_open_or_collection",
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            ],
        }
    finally:
        if transport is not None:
            try:
                transport.close()
            except Exception as exc:
                if report is not None:
                    report.setdefault("close_warnings", []).append(
                        f"{type(exc).__name__}: {exc}"
                    )
    if report is None:  # Defensive; every path above assigns a report.
        raise RuntimeError("pair observation produced no instrument report")
    return report


def observe_pair(
    output_root: Path,
    *,
    nanovoltmeter_resource: str,
    current_source_resource: str,
    operation: str,
    mode: str = "real",
    timeout_ms: int = 3000,
    local_metadata: dict[str, str] | None = None,
    real_transport_factory=PyVisaQueryTransport,
    host_gate: Callable[[], object] = require_production_host,
    readiness_provider: Callable[[], dict[str, object]] = production_host_readiness,
) -> dict[str, object]:
    """Take one explicit query-only snapshot of a freely selected lab pair."""
    if operation not in PAIR_PROFILES:
        raise ValueError(f"unsupported pair operation: {operation!r}")
    if mode not in {"real", "simulate"}:
        raise ValueError(f"unsupported pair mode: {mode!r}")
    _validate_pair(nanovoltmeter_resource, current_source_resource)
    if mode == "real":
        host_gate()

    metadata = {
        str(key): str(value).strip()
        for key, value in (local_metadata or {}).items()
        if str(value).strip()
    }
    run_dir = _new_run_directory(Path(output_root), mode, operation)
    started = datetime.now(timezone.utc)
    profiles = PAIR_PROFILES[operation]

    report_6221 = _collect_one(
        profiles["6221"], current_source_resource, mode, timeout_ms, real_transport_factory
    )
    report_2182a = _collect_one(
        profiles["2182a"], nanovoltmeter_resource, mode, timeout_ms, real_transport_factory
    )
    summary = _pair_summary(report_6221, report_2182a, operation, metadata)
    readiness = (
        readiness_provider()
        if mode == "real"
        else {"host_gate_passed": None, "blockers": []}
    )
    finished = datetime.now(timezone.utc)

    result: dict[str, object] = {
        "schema_version": 1,
        "operation": f"manual-refresh query-only pair {operation} snapshot",
        "project_version": __version__,
        "mode": mode,
        "started_utc": started.isoformat(),
        "finished_utc": finished.isoformat(),
        "run_directory": str(run_dir),
        "pair": {
            "6221_resource": current_source_resource,
            "2182a_resource": nanovoltmeter_resource,
            "pairing_source": "operator selection in GUI; physical pairing is not inferred",
        },
        "local_metadata": metadata,
        "safety": {
            "query_only": True,
            "manual_refresh_only": True,
            "automatic_polling": False,
            "generic_command_entry": False,
            "instrument_configuration_writes": False,
            "reset_clear_trigger_acquisition_or_output_control": False,
            "local_calculation_only": True,
        },
        "host_environment": host_environment(),
        "production_readiness": readiness,
        "summary": summary,
        "instruments": {
            "6221": report_6221,
            "2182a": report_2182a,
        },
    }
    evidence_file = run_dir / "pair-observer.json"
    result["evidence_file"] = str(evidence_file)
    _write_new_json(evidence_file, result)
    return result
