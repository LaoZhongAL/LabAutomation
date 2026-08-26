"""Pure model-level diagnostic rules and explicit Live compatibility checks.

This module has no GUI or VISA dependency.  It interprets evidence that was
already collected through the monitor's exact query allowlist; it never sends
an instrument message.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Mapping, Sequence


DIAGNOSTIC_SCHEMA_VERSION = 3
PROFILE_ID = "keithley-2182a-read-only-v1.5"


@dataclass(frozen=True)
class DeviceTarget:
    resource: str
    vendor: str
    model: str
    serial: str
    firmware: str
    role: str


@dataclass(frozen=True)
class DeviceIdentity:
    raw: str
    vendor: str
    model: str
    serial: str
    firmware: str


GPIB6_TARGET = DeviceTarget(
    resource="GPIB0::6::INSTR",
    vendor="KEITHLEY INSTRUMENTS INC.",
    model="2182A",
    serial="1340129",
    firmware="C02 /A02",
    role="hall_bar_voltage_gpib6",
)


class CheckStatus(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    BLOCKED = "BLOCKED"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "N/A"


class ReadinessLayer(str, Enum):
    COMMUNICATION = "communication"
    IDENTITY = "identity"
    CONFIGURATION = "configuration"
    ACQUISITION = "acquisition"
    RECORDER = "recorder"


class DiagnosticState(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    VERIFYING_IDENTITY = "VERIFYING_IDENTITY"
    CHECKING_CONFIG = "CHECKING_CONFIG"
    OBSERVE_READY = "OBSERVE_READY"
    LIVE = "LIVE"
    DEGRADED = "DEGRADED"
    RECOVERING = "RECOVERING"
    FAULT_LATCHED = "FAULT_LATCHED"


@dataclass(frozen=True)
class CheckResult:
    check_id: str
    layer: ReadinessLayer
    status: CheckStatus
    blocks_live: bool
    expected: object
    observed: object
    message: str

    def as_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["layer"] = self.layer.value
        value["status"] = self.status.value
        return value


@dataclass(frozen=True)
class ReadinessReport:
    overall: CheckStatus
    can_start_live: bool
    checks: tuple[CheckResult, ...]

    def health_axes(
        self,
        *,
        live_supported: bool | None = None,
        live_authorized: bool | None = None,
    ) -> dict[str, str]:
        def status_for(
            members: Sequence[CheckResult],
            *,
            empty: CheckStatus = CheckStatus.UNKNOWN,
        ) -> str:
            return (_aggregate_status(members) if members else empty).value

        identity = [check for check in self.checks if check.check_id.startswith("identity.")]
        communication = [
            check for check in self.checks if check.layer == ReadinessLayer.COMMUNICATION
        ]
        snapshot = [
            check
            for check in self.checks
            if check.check_id.startswith("communication.")
            and check.check_id != "communication.configuration_latency"
        ]
        safety = [check for check in self.checks if check.check_id.startswith("safety.")]
        configuration = [
            check
            for check in self.checks
            if check.check_id.startswith(("configuration.", "precision."))
        ]
        profile = [check for check in self.checks if check.check_id.startswith("profile.")]
        if not profile:
            profile = [check for check in configuration if check.blocks_live]
        calibration_condition = [
            check
            for check in self.checks
            if check.check_id.startswith("calibration.condition.")
        ]
        calibration_traceability = [
            check
            for check in self.checks
            if check.check_id.startswith("calibration.traceability.")
        ]
        if live_supported is False:
            live_status = CheckStatus.NOT_APPLICABLE.value
        elif live_supported is True:
            live_status = (
                CheckStatus.PASS.value
                if live_authorized is True and self.can_start_live
                else CheckStatus.BLOCKED.value
            )
        else:
            live_status = CheckStatus.UNKNOWN.value
        return {
            "identity_verified": status_for(identity),
            "transport_healthy": status_for(communication),
            "snapshot_complete": status_for(snapshot),
            "safe_idle": status_for(safety, empty=CheckStatus.NOT_APPLICABLE),
            "configuration_interpretable": status_for(configuration),
            "profile_matched": status_for(profile),
            "calibration_condition": status_for(calibration_condition),
            "calibration_traceability": status_for(calibration_traceability),
            "performance_validated": CheckStatus.UNKNOWN.value,
            "evidence_complete": CheckStatus.UNKNOWN.value,
            "live_authorized": live_status,
        }

    def as_dict(
        self,
        *,
        profile_id: str = PROFILE_ID,
        live_supported: bool | None = None,
        live_authorized: bool | None = None,
    ) -> dict[str, object]:
        blockers = [
            check.message
            for check in self.checks
            if check.blocks_live and check.status != CheckStatus.PASS
        ]
        warnings = [
            check.message for check in self.checks if check.status == CheckStatus.WARN
        ]
        layer_status: dict[str, str] = {}
        for layer in ReadinessLayer:
            members = [check for check in self.checks if check.layer == layer]
            if not members:
                continue
            layer_status[layer.value] = _aggregate_status(members).value
        return {
            "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
            "profile_id": profile_id,
            "overall": self.overall.value,
            "can_start_live": self.can_start_live,
            "layer_status": layer_status,
            "health_axes": self.health_axes(
                live_supported=live_supported,
                live_authorized=live_authorized,
            ),
            "blockers": blockers,
            "warnings": warnings,
            "checks": [check.as_dict() for check in self.checks],
            "summary": {
                "pass": sum(check.status == CheckStatus.PASS for check in self.checks),
                "warn": sum(check.status == CheckStatus.WARN for check in self.checks),
                "blocked": sum(check.status == CheckStatus.BLOCKED for check in self.checks),
                "unknown": sum(check.status == CheckStatus.UNKNOWN for check in self.checks),
                "not_applicable": sum(
                    check.status == CheckStatus.NOT_APPLICABLE for check in self.checks
                ),
                "total": len(self.checks),
            },
        }


class InvalidStateTransition(RuntimeError):
    pass


ALLOWED_STATE_TRANSITIONS = {
    DiagnosticState.DISCONNECTED: {DiagnosticState.VERIFYING_IDENTITY},
    DiagnosticState.VERIFYING_IDENTITY: {
        DiagnosticState.CHECKING_CONFIG,
        DiagnosticState.FAULT_LATCHED,
        DiagnosticState.DISCONNECTED,
    },
    DiagnosticState.CHECKING_CONFIG: {
        DiagnosticState.OBSERVE_READY,
        DiagnosticState.FAULT_LATCHED,
        DiagnosticState.DISCONNECTED,
    },
    DiagnosticState.OBSERVE_READY: {
        DiagnosticState.LIVE,
        DiagnosticState.VERIFYING_IDENTITY,
        DiagnosticState.DISCONNECTED,
        DiagnosticState.FAULT_LATCHED,
    },
    DiagnosticState.LIVE: {
        DiagnosticState.OBSERVE_READY,
        DiagnosticState.DEGRADED,
        DiagnosticState.RECOVERING,
        DiagnosticState.FAULT_LATCHED,
    },
    DiagnosticState.DEGRADED: {
        DiagnosticState.LIVE,
        DiagnosticState.OBSERVE_READY,
        DiagnosticState.RECOVERING,
        DiagnosticState.FAULT_LATCHED,
        DiagnosticState.DISCONNECTED,
    },
    DiagnosticState.RECOVERING: {
        DiagnosticState.VERIFYING_IDENTITY,
        DiagnosticState.FAULT_LATCHED,
        DiagnosticState.DISCONNECTED,
    },
    DiagnosticState.FAULT_LATCHED: {
        DiagnosticState.VERIFYING_IDENTITY,
        DiagnosticState.DISCONNECTED,
    },
}


class DiagnosticStateMachine:
    def __init__(self) -> None:
        self.state = DiagnosticState.DISCONNECTED

    def transition(self, target: DiagnosticState) -> tuple[DiagnosticState, DiagnosticState]:
        before = self.state
        if target == before:
            return before, target
        if target not in ALLOWED_STATE_TRANSITIONS[before]:
            raise InvalidStateTransition(f"invalid diagnostic transition: {before.value} -> {target.value}")
        self.state = target
        return before, target


def normalize_field(value: str) -> str:
    return " ".join(value.strip().split())


def normalize_token(value: str) -> str:
    return normalize_field(value).strip('"').upper()


def parse_idn(raw: str) -> DeviceIdentity:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("empty *IDN? response")
    fields = [normalize_field(field) for field in raw.strip().split(",")]
    if len(fields) != 4:
        raise ValueError(f"expected exactly four *IDN? fields, got {len(fields)}")
    vendor, model_field, serial, firmware = fields
    for field_name, field_value in (
        ("vendor", vendor),
        ("serial", serial),
        ("firmware", firmware),
    ):
        if not field_value:
            raise ValueError(f"empty {field_name} field in *IDN? response")
    model_match = re.fullmatch(r"MODEL\s+(.+)", model_field, flags=re.IGNORECASE)
    if model_match is None:
        raise ValueError(f"invalid model field: {model_field!r}")
    return DeviceIdentity(
        raw=raw.strip(),
        vendor=vendor,
        model=normalize_field(model_match.group(1)),
        serial=serial,
        firmware=firmware,
    )


def identity_checks(raw: str, target: DeviceTarget = GPIB6_TARGET) -> tuple[CheckResult, ...]:
    try:
        identity = parse_idn(raw)
    except Exception as exc:
        return (
            CheckResult(
                "identity.parse",
                ReadinessLayer.IDENTITY,
                CheckStatus.BLOCKED,
                True,
                "four-field Keithley *IDN? response",
                raw,
                f"Identity response cannot be parsed: {type(exc).__name__}: {exc}",
            ),
        )

    comparisons = (
        ("identity.vendor", target.vendor, identity.vendor, "manufacturer"),
        ("identity.model", target.model, identity.model, "model"),
        ("identity.serial", target.serial, identity.serial, "serial number"),
        ("identity.firmware", target.firmware, identity.firmware, "firmware"),
    )
    checks: list[CheckResult] = []
    for check_id, expected, observed, label in comparisons:
        exact = normalize_token(str(observed)) == normalize_token(str(expected))
        checks.append(
            CheckResult(
                check_id,
                ReadinessLayer.IDENTITY,
                CheckStatus.PASS if exact else CheckStatus.BLOCKED,
                True,
                expected,
                observed,
                f"Exact {label} match." if exact else f"Exact {label} mismatch.",
            )
        )
    return tuple(checks)


def identity_is_exact(raw: str, target: DeviceTarget = GPIB6_TARGET) -> bool:
    checks = identity_checks(raw, target)
    return bool(checks) and all(check.status == CheckStatus.PASS for check in checks)


def _finite_float(value: object) -> float:
    parsed = float(str(value).strip())
    if not math.isfinite(parsed):
        raise ValueError(f"non-finite value: {value!r}")
    return parsed


def _integer(value: object) -> int:
    parsed = _finite_float(value)
    if not parsed.is_integer():
        raise ValueError(f"not an integer: {value!r}")
    return int(parsed)


def _check_value(
    check_id: str,
    layer: ReadinessLayer,
    values: Mapping[str, str],
    name: str,
    expected: object,
    parser,
    predicate,
    *,
    blocks_live: bool = True,
    failure_status: CheckStatus = CheckStatus.BLOCKED,
) -> CheckResult:
    raw = values.get(name)
    if raw is None or not str(raw).strip():
        return CheckResult(
            check_id,
            layer,
            CheckStatus.UNKNOWN,
            blocks_live,
            expected,
            raw,
            f"Required response {name!r} is missing or empty.",
        )
    try:
        observed = parser(raw)
    except (TypeError, ValueError) as exc:
        return CheckResult(
            check_id,
            layer,
            failure_status,
            blocks_live,
            expected,
            raw,
            f"Response {name!r} is invalid: {type(exc).__name__}: {exc}",
        )
    passed = bool(predicate(observed))
    return CheckResult(
        check_id,
        layer,
        CheckStatus.PASS if passed else failure_status,
        blocks_live,
        expected,
        observed,
        f"{name} matches the approved baseline."
        if passed
        else f"{name} differs from the approved baseline.",
    )


def _boolean_state(value: object) -> int:
    normalized = normalize_token(str(value))
    if normalized in {"0", "OFF"}:
        return 0
    if normalized in {"1", "ON"}:
        return 1
    raise ValueError(f"not an ON/OFF state: {value!r}")


def _snapshot_values_equal(begin: object, end: object) -> bool:
    try:
        return _boolean_state(begin) == _boolean_state(end)
    except ValueError:
        pass
    try:
        return math.isclose(
            _finite_float(begin),
            _finite_float(end),
            rel_tol=1e-12,
            abs_tol=0.0,
        )
    except (TypeError, ValueError):
        return normalize_token(str(begin)) == normalize_token(str(end))


def _sentinel_consistency_check(
    values: Mapping[str, str],
    consistency_names: Sequence[str],
) -> CheckResult | None:
    if not consistency_names:
        return None
    missing: list[str] = []
    mismatches: list[str] = []
    observed: dict[str, dict[str, object]] = {}
    for name in consistency_names:
        end_name = f"snapshot_end.{name}"
        begin = values.get(name)
        end = values.get(end_name)
        observed[name] = {"begin": begin, "end": end}
        if begin is None or end is None or not str(begin).strip() or not str(end).strip():
            missing.append(name)
        elif not _snapshot_values_equal(begin, end):
            mismatches.append(name)
    if mismatches:
        status = CheckStatus.BLOCKED
        message = "Critical diagnostic fields changed within the same VISA session."
    elif missing:
        status = CheckStatus.UNKNOWN
        message = "Critical diagnostic fields are missing from the begin/end snapshot."
    else:
        status = CheckStatus.PASS
        message = "Critical diagnostic fields remained consistent within the same VISA session."
    return CheckResult(
        "communication.sentinel_consistency",
        ReadinessLayer.COMMUNICATION,
        status,
        True,
        "parsed begin/end values are equal",
        {"values": observed, "missing": missing, "mismatches": mismatches},
        message,
    )


def _scoped_check_id(check_id: str, scope: str | None) -> str:
    if scope is None:
        return check_id
    category, separator, remainder = check_id.partition(".")
    return f"{category}.{scope}.{remainder}" if separator else f"{scope}.{check_id}"


def _2182a_key_status_checks(
    values: Mapping[str, str],
    *,
    scope: str | None = None,
) -> list[CheckResult]:
    checks: list[CheckResult] = []
    for name in ("system_autozero", "front_autozero", "line_sync"):
        checks.append(
            _check_value(
                _scoped_check_id(f"configuration.{name}", scope),
                ReadinessLayer.CONFIGURATION,
                values,
                name,
                "OFF/0 or ON/1",
                _boolean_state,
                lambda value: value in {0, 1},
            )
        )

    checks.append(
        _check_value(
            _scoped_check_id("precision.system_autozero", scope),
            ReadinessLayer.CONFIGURATION,
            values,
            "system_autozero",
            "ON for sustained precision measurements",
            _boolean_state,
            lambda value: value == 1,
            blocks_live=False,
            failure_status=CheckStatus.WARN,
        )
    )

    try:
        line_sync = _boolean_state(values.get("line_sync"))
        nplc = _finite_float(values.get("nplc"))
    except (TypeError, ValueError) as exc:
        checks.append(
            CheckResult(
                _scoped_check_id("precision.line_sync_effective", scope),
                ReadinessLayer.CONFIGURATION,
                CheckStatus.UNKNOWN,
                False,
                "LSYNC is effective only at NPLC >= 1",
                {"line_sync": values.get("line_sync"), "nplc": values.get("nplc")},
                f"LSYNC effectiveness could not be decoded: {type(exc).__name__}: {exc}",
            )
        )
    else:
        line_sync_ineffective = bool(line_sync and nplc < 1.0)
        checks.append(
            CheckResult(
                _scoped_check_id("precision.line_sync_effective", scope),
                ReadinessLayer.CONFIGURATION,
                CheckStatus.WARN if line_sync_ineffective else CheckStatus.PASS,
                False,
                "LSYNC is effective only at NPLC >= 1",
                {"line_sync": line_sync, "nplc": nplc},
                "LSYNC is enabled below 1 NPLC and therefore is not effective."
                if line_sync_ineffective
                else "The LSYNC state is consistent with its documented NPLC limit.",
            )
        )

    condition_masks = (
        ("operation_condition", 0x0531),
        ("measurement_condition", 0x03BF),
        ("questionable_condition", 0x0310),
    )
    for name, mask in condition_masks:
        checks.append(
            _check_value(
                _scoped_check_id(f"status.{name}", scope),
                ReadinessLayer.ACQUISITION,
                values,
                name,
                f"integer condition word using only mask 0x{mask:04X}",
                _integer,
                lambda value, allowed=mask: (
                    0 <= value <= 65535 and value & ~allowed == 0
                ),
            )
        )

    danger_bits = (
        (
            "acquisition.calibrating",
            ReadinessLayer.ACQUISITION,
            "operation_condition",
            0,
            CheckStatus.BLOCKED,
            True,
            "The 2182A is calibrating.",
        ),
        (
            "acquisition.reading_overflow",
            ReadinessLayer.ACQUISITION,
            "measurement_condition",
            0,
            CheckStatus.BLOCKED,
            True,
            "The 2182A reports reading overflow.",
        ),
        (
            "calibration.condition.invalid_constant",
            ReadinessLayer.CONFIGURATION,
            "questionable_condition",
            8,
            CheckStatus.BLOCKED,
            True,
            "The 2182A reports an invalid calibration constant.",
        ),
        (
            "calibration.condition.invalid_acal",
            ReadinessLayer.CONFIGURATION,
            "questionable_condition",
            9,
            CheckStatus.BLOCKED,
            True,
            "The 2182A reports an invalid ACAL.",
        ),
        (
            "status.temperature_reference",
            ReadinessLayer.ACQUISITION,
            "questionable_condition",
            4,
            CheckStatus.WARN,
            False,
            "The thermocouple reference-junction condition is active.",
        ),
    )
    for check_id, layer, name, bit, active_status, blocks, active_message in danger_bits:
        check_id = _scoped_check_id(check_id, scope)
        raw = values.get(name)
        try:
            active = bool(_integer(raw) & (1 << bit))
        except (TypeError, ValueError) as exc:
            checks.append(
                CheckResult(
                    check_id,
                    layer,
                    CheckStatus.UNKNOWN,
                    blocks,
                    f"{name} bit {bit} clear",
                    raw,
                    f"Condition bit could not be decoded: {type(exc).__name__}: {exc}",
                )
            )
        else:
            checks.append(
                CheckResult(
                    check_id,
                    layer,
                    active_status if active else CheckStatus.PASS,
                    blocks,
                    f"{name} bit {bit} clear",
                    active,
                    active_message if active else f"{name} bit {bit} is clear.",
                )
            )
    return checks


def _aggregate_status(checks: Sequence[CheckResult]) -> CheckStatus:
    relevant = [check for check in checks if check.status != CheckStatus.NOT_APPLICABLE]
    if any(check.status == CheckStatus.BLOCKED for check in relevant):
        return CheckStatus.BLOCKED
    if any(check.status == CheckStatus.UNKNOWN for check in relevant):
        return CheckStatus.UNKNOWN
    if any(check.status == CheckStatus.WARN for check in relevant):
        return CheckStatus.WARN
    return CheckStatus.PASS


def _session_lifecycle_failure(
    transcript: Sequence[Mapping[str, object]],
) -> CheckResult | None:
    failures = [
        item
        for item in transcript
        if isinstance(item, Mapping)
        and item.get("name") == "session_lifecycle"
        and not item.get("ok")
    ]
    if not failures:
        return None
    failure = failures[-1]
    return CheckResult(
        "communication.session_lifecycle",
        ReadinessLayer.COMMUNICATION,
        CheckStatus.BLOCKED,
        True,
        "diagnostic session closed cleanly",
        failure.get("error"),
        "The diagnostic session reported a lifecycle failure.",
    )


def evaluate_readiness(
    values: Mapping[str, str],
    transcript: Sequence[Mapping[str, object]],
    required_names: Sequence[str],
    *,
    recorder_ready: bool,
    target: DeviceTarget = GPIB6_TARGET,
    consistency_names: Sequence[str] = (),
) -> ReadinessReport:
    """Evaluate 2182A model state, then the existing Live readout contract.

    The model checks interpret settings returned by the instrument; they do
    not select a channel, range, NPLC, filter, line frequency, or trigger
    recipe.  The additional checks cover only what the current scalar
    FETCh? Live path needs in order to return fresh voltage text.
    """

    model_report = evaluate_observe_readiness(
        values,
        transcript,
        required_names,
        recorder_ready=recorder_ready,
        target=target,
        instrument_family="2182a",
        consistency_names=consistency_names,
    )
    checks = list(model_report.checks)
    text = lambda raw: normalize_token(str(raw))
    checks.extend(
        (
            _check_value(
                "live_compatibility.sense_function",
                ReadinessLayer.ACQUISITION,
                values,
                "sense_function",
                "VOLT:DC for the voltage GUI",
                text,
                lambda value: value == "VOLT:DC",
            ),
            _check_value(
                "live_compatibility.sample_count",
                ReadinessLayer.ACQUISITION,
                values,
                "sample_count",
                "1 for one scalar per FETCh? response",
                _integer,
                lambda value: value == 1,
            ),
            _check_value(
                "live_compatibility.continuous_initiation",
                ReadinessLayer.ACQUISITION,
                values,
                "continuous_initiation",
                "1 for continuously refreshed readings",
                _integer,
                lambda value: value == 1,
            ),
            _check_value(
                "live_compatibility.data_format",
                ReadinessLayer.ACQUISITION,
                values,
                "data_format",
                "ASC for text parsing",
                text,
                lambda value: value == "ASC",
            ),
            _check_value(
                "live_compatibility.format_elements",
                ReadinessLayer.ACQUISITION,
                values,
                "format_elements",
                "READ for one scalar response",
                text,
                lambda value: value == "READ",
            ),
        )
    )
    blocking_failure = any(
        check.blocks_live and check.status != CheckStatus.PASS for check in checks
    )
    return ReadinessReport(
        _aggregate_status(checks),
        not blocking_failure,
        tuple(checks),
    )


def _base_observe_checks(
    values: Mapping[str, str],
    transcript: Sequence[Mapping[str, object]],
    required_names: Sequence[str],
    *,
    recorder_ready: bool,
    target: DeviceTarget,
) -> list[CheckResult]:
    """Checks shared by diagnostic snapshots, without implying Live support."""

    checks = [
        CheckResult(
            "recorder.preflight",
            ReadinessLayer.RECORDER,
            CheckStatus.PASS if recorder_ready else CheckStatus.BLOCKED,
            True,
            True,
            recorder_ready,
            "Evidence directory and diagnostic files are writable."
            if recorder_ready
            else "Evidence recorder preflight failed.",
        )
    ]
    lifecycle_failure = _session_lifecycle_failure(transcript)
    if lifecycle_failure is not None:
        checks.append(lifecycle_failure)
    transcript_by_name = {
        str(item.get("name")): item for item in transcript if isinstance(item, Mapping)
    }
    elapsed_values: list[float] = []
    for name in required_names:
        item = transcript_by_name.get(name)
        response = item.get("response") if item is not None else None
        ok = bool(
            item is not None
            and item.get("ok")
            and response is not None
            and str(response).strip()
        )
        checks.append(
            CheckResult(
                f"communication.{name}",
                ReadinessLayer.COMMUNICATION,
                CheckStatus.PASS if ok else (CheckStatus.BLOCKED if item else CheckStatus.UNKNOWN),
                True,
                "successful non-empty response",
                response if ok else (item.get("error") if item is not None else None),
                f"Required query {name!r} completed."
                if ok
                else f"Required query {name!r} failed or returned no data.",
            )
        )
        if item is not None:
            try:
                elapsed_values.append(_finite_float(item.get("elapsed_ms", 0.0)))
            except Exception:
                pass
    maximum_elapsed_ms = max(elapsed_values, default=0.0)
    checks.append(
        CheckResult(
            "communication.configuration_latency",
            ReadinessLayer.COMMUNICATION,
            CheckStatus.PASS if maximum_elapsed_ms <= 500.0 else CheckStatus.WARN,
            False,
            "maximum query latency <= 500 ms",
            maximum_elapsed_ms,
            "Configuration query latency is within the observation threshold."
            if maximum_elapsed_ms <= 500.0
            else "At least one configuration query exceeded the 500 ms observation threshold.",
        )
    )
    checks.extend(identity_checks(values.get("identity", ""), target))
    return checks


def _bit_is_set(value: object, bit: int) -> bool:
    return bool(_integer(value) & (1 << bit))


def _decode_2450_source_mode(raw: object) -> str | None:
    normalized = normalize_token(str(raw))
    if normalized == "0":
        return "current"
    if normalized == "1":
        return "voltage"
    return None


def evaluate_observe_readiness(
    values: Mapping[str, str],
    transcript: Sequence[Mapping[str, object]],
    required_names: Sequence[str],
    *,
    recorder_ready: bool,
    target: DeviceTarget,
    instrument_family: str,
    consistency_names: Sequence[str] = (),
) -> ReadinessReport:
    """Evaluate model-specific read-only diagnostics for targets without Live.

    ``can_start_live`` in the returned pure report means that the diagnostic
    evidence has no blocking failure.  The caller must still combine it with
    the selected profile's explicit ``live_supported`` capability; this
    function never grants a source instrument a Live voltage path.
    """

    family = instrument_family.strip().lower()
    checks = _base_observe_checks(
        values,
        transcript,
        required_names,
        recorder_ready=recorder_ready,
        target=target,
    )
    consistency_check = _sentinel_consistency_check(values, consistency_names)
    if consistency_check is not None:
        checks.append(consistency_check)
    text = lambda raw: normalize_token(str(raw))

    if family == "2182a":
        try:
            line_frequency = _finite_float(values.get("line_frequency_hz"))
        except (TypeError, ValueError):
            line_frequency = None
        validators = (
            ("configuration.scpi_version", "scpi_version", "positive SCPI version", _finite_float, lambda v: v > 0.0, True, CheckStatus.BLOCKED),
            ("configuration.line_frequency_hz", "line_frequency_hz", "50 or 60 Hz", _finite_float, lambda v: v in {50.0, 60.0}, True, CheckStatus.BLOCKED),
            ("configuration.power_on_setup", "power_on_setup", "RST, PRESET, or SAV0", text, lambda v: v in {"RST", "PRESET", "SAV0"}, True, CheckStatus.BLOCKED),
            ("configuration.sense_function", "sense_function", "VOLT:DC or TEMP", text, lambda v: v in {"VOLT:DC", "TEMP"}, True, CheckStatus.BLOCKED),
            ("configuration.active_channel", "active_channel", "channel 1 or 2", _integer, lambda v: v in {1, 2}, True, CheckStatus.BLOCKED),
            ("configuration.nplc", "nplc", "positive and within the line-frequency limit", _finite_float, lambda v: line_frequency in {50.0, 60.0} and 0.0 < v <= line_frequency, True, CheckStatus.BLOCKED),
            ("configuration.trigger_count", "trigger_count", "integer 1..9999 or infinite sentinel", _finite_float, lambda v: (v.is_integer() and 1.0 <= v <= 9999.0) or v >= 1e37, True, CheckStatus.BLOCKED),
            ("configuration.trigger_delay_s", "trigger_delay_s", "0..999999.999 s", _finite_float, lambda v: 0.0 <= v <= 999999.999, True, CheckStatus.BLOCKED),
            ("configuration.trigger_source", "trigger_source", "IMM, TIM, MAN, BUS, or EXT", text, lambda v: v in {"IMM", "TIM", "MAN", "BUS", "EXT"}, True, CheckStatus.BLOCKED),
            ("configuration.sample_count", "sample_count", "integer 1..1024", _integer, lambda v: 1 <= v <= 1024, True, CheckStatus.BLOCKED),
            ("configuration.continuous_initiation", "continuous_initiation", "0 or 1", _integer, lambda v: v in {0, 1}, True, CheckStatus.BLOCKED),
            ("configuration.data_format", "data_format", "ASC, SRE, or DRE", text, lambda v: v in {"ASC", "SRE", "DRE"}, True, CheckStatus.BLOCKED),
            ("configuration.format_elements", "format_elements", "comma-separated CHAN/READ/TST/UNIT elements", lambda raw: tuple(part.strip() for part in text(raw).split(",")), lambda v: bool(v) and all(part in {"CHAN", "READ", "TST", "UNIT"} for part in v), True, CheckStatus.BLOCKED),
        )
        for check_id, name, expected, parser, predicate, blocks, failure in validators:
            checks.append(
                _check_value(
                    check_id,
                    ReadinessLayer.CONFIGURATION,
                    values,
                    name,
                    expected,
                    parser,
                    predicate,
                    blocks_live=blocks,
                    failure_status=failure,
                )
            )
        try:
            active_channel = _integer(values.get("active_channel"))
        except (TypeError, ValueError):
            active_channel = None
        for channel in (1, 2):
            active_or_unknown = active_channel in {None, channel}
            channel_validators = (
                (f"ch{channel}_range_v", "> 0 V", _finite_float, lambda value: value > 0.0),
                (f"ch{channel}_autorange", "0 or 1", _integer, lambda value: value in {0, 1}),
                (f"ch{channel}_digital_filter", "0 or 1", _integer, lambda value: value in {0, 1}),
                (f"ch{channel}_analog_filter", "0 or 1", _integer, lambda value: value in {0, 1}),
            )
            for name, expected, parser, predicate in channel_validators:
                checks.append(
                    _check_value(
                        f"configuration.{name}",
                        ReadinessLayer.CONFIGURATION,
                        values,
                        name,
                        expected,
                        parser,
                        predicate,
                        blocks_live=active_or_unknown,
                        failure_status=(
                            CheckStatus.BLOCKED
                            if active_or_unknown
                            else CheckStatus.WARN
                        ),
                    )
                )
        checks.extend(_2182a_key_status_checks(values))
        if any(name.startswith("snapshot_end.") for name in values):
            end_values = {
                name: values.get(f"snapshot_end.{name}")
                for name in (
                    "system_autozero",
                    "front_autozero",
                    "line_sync",
                    "nplc",
                    "operation_condition",
                    "measurement_condition",
                    "questionable_condition",
                )
            }
            checks.extend(
                _2182a_key_status_checks(end_values, scope="snapshot_end")
            )
        checks.append(
            CheckResult(
                "profile.measurement_baseline",
                ReadinessLayer.CONFIGURATION,
                CheckStatus.WARN,
                False,
                "sample/device-specific approved profile",
                "not selected",
                "Settings are readable, but range/NPLC/filter are not judged against a sample profile.",
            )
        )

    elif family == "6221":
        checks.extend(
            (
                _check_value(
                    "safety.output_off",
                    ReadinessLayer.ACQUISITION,
                    values,
                    "output_enabled",
                    0,
                    _integer,
                    lambda value: value == 0,
                ),
                _check_value(
                    "configuration.current_range_a",
                    ReadinessLayer.CONFIGURATION,
                    values,
                    "current_range_a",
                    "> 0 A",
                    _finite_float,
                    lambda value: 0.0 < value <= 0.105,
                ),
                _check_value(
                    "configuration.current_range_auto",
                    ReadinessLayer.CONFIGURATION,
                    values,
                    "current_range_auto",
                    "0 or 1",
                    _integer,
                    lambda value: value in {0, 1},
                ),
                _check_value(
                    "safety.compliance_limit",
                    ReadinessLayer.CONFIGURATION,
                    values,
                    "voltage_compliance_v",
                    "> 0 V",
                    _finite_float,
                    lambda value: 0.1 <= value <= 105.0,
                ),
                _check_value(
                    "configuration.analog_filter",
                    ReadinessLayer.CONFIGURATION,
                    values,
                    "analog_filter",
                    "0 or 1",
                    _integer,
                    lambda value: value in {0, 1},
                ),
                _check_value(
                    "configuration.output_response",
                    ReadinessLayer.CONFIGURATION,
                    values,
                    "output_response",
                    "FAST or SLOW",
                    text,
                    lambda value: value in {"FAST", "SLOW"},
                ),
                _check_value(
                    "configuration.triax_inner_shield",
                    ReadinessLayer.CONFIGURATION,
                    values,
                    "triax_inner_shield",
                    "GUAR/GUARD or OLOW",
                    text,
                    lambda value: value in {"GUAR", "GUARD", "OLOW"},
                ),
                _check_value(
                    "configuration.output_low_to_earth",
                    ReadinessLayer.CONFIGURATION,
                    values,
                    "output_low_to_earth",
                    "0 or 1",
                    _integer,
                    lambda value: value in {0, 1},
                ),
            )
        )
        interlock_raw = values.get("interlock_tripped_raw")
        try:
            interlock_value = _integer(interlock_raw)
        except (TypeError, ValueError) as exc:
            checks.append(
                CheckResult(
                    "safety.interlock_ready",
                    ReadinessLayer.ACQUISITION,
                    CheckStatus.BLOCKED,
                    True,
                    "0 open/tripped or 1 closed/asserted",
                    interlock_raw,
                    f"6221 interlock state is invalid: {type(exc).__name__}: {exc}",
                )
            )
        else:
            interlock_valid = interlock_value in {0, 1}
            interlock_ready = interlock_value == 1
            checks.append(
                CheckResult(
                    "safety.interlock_ready",
                    ReadinessLayer.ACQUISITION,
                    (
                        CheckStatus.PASS
                        if interlock_ready
                        else CheckStatus.WARN if interlock_valid else CheckStatus.BLOCKED
                    ),
                    not interlock_valid,
                    "1 closed/asserted before sourcing",
                    interlock_value,
                    "6221 interlock is closed/asserted."
                    if interlock_ready
                    else (
                        "6221 interlock is open/tripped; output remains safely OFF but is not ready for sourcing."
                        if interlock_valid
                        else "6221 interlock state is outside the documented 0/1 domain."
                    ),
                )
            )
        for name, maximum in (
            ("status_byte", 255),
            ("operation_condition", 65535),
            ("measurement_condition", 65535),
            ("questionable_condition", 65535),
        ):
            checks.append(
                _check_value(
                    f"status.{name}",
                    ReadinessLayer.ACQUISITION,
                    values,
                    name,
                    f"integer condition word 0..{maximum}",
                    _integer,
                    lambda value, upper=maximum: 0 <= value <= upper,
                )
            )
        checks.append(
            _check_value(
                "safety.operation_idle",
                ReadinessLayer.ACQUISITION,
                values,
                "operation_condition",
                "bit 10 set (Idle)",
                _integer,
                lambda value: bool(value & (1 << 10)),
            )
        )
        if "snapshot_end.operation_condition" in values:
            checks.extend(
                (
                    _check_value(
                        "status.snapshot_end.operation_condition",
                        ReadinessLayer.ACQUISITION,
                        values,
                        "snapshot_end.operation_condition",
                        "integer condition word 0..65535",
                        _integer,
                        lambda value: 0 <= value <= 65535,
                    ),
                    _check_value(
                        "safety.snapshot_end.operation_idle",
                        ReadinessLayer.ACQUISITION,
                        values,
                        "snapshot_end.operation_condition",
                        "bit 10 set (Idle)",
                        _integer,
                        lambda value: bool(value & (1 << 10)),
                    ),
                )
            )
        measurement = values.get("measurement_condition")
        questionable = values.get("questionable_condition")
        status_byte = values.get("status_byte")
        for check_id, raw, bit, expected, message in (
            ("safety.over_temperature", measurement, 2, "bit 2 clear", "6221 over-temperature condition"),
            ("safety.compliance_active", measurement, 3, "bit 3 clear", "6221 compliance condition"),
            ("calibration.condition.questionable", questionable, 8, "bit 8 clear", "6221 calibration questionable condition"),
        ):
            try:
                active = _bit_is_set(raw, bit)
                status = CheckStatus.BLOCKED if active else CheckStatus.PASS
            except Exception:
                active = None
                status = CheckStatus.UNKNOWN
            checks.append(
                CheckResult(
                    check_id,
                    ReadinessLayer.ACQUISITION,
                    status,
                    True,
                    expected,
                    active,
                    f"{message} is {'active' if active else 'clear'}."
                    if active is not None
                    else f"{message} could not be decoded.",
                )
            )
        try:
            error_pending = _bit_is_set(status_byte, 2)
            error_status = CheckStatus.WARN if error_pending else CheckStatus.PASS
        except Exception:
            error_pending = None
            error_status = CheckStatus.UNKNOWN
        checks.append(
            CheckResult(
                "status.error_pending",
                ReadinessLayer.COMMUNICATION,
                error_status,
                False,
                "status byte bit 2 clear",
                error_pending,
                "Error queue is reported pending; the queue was not consumed."
                if error_pending
                else "No pending error is reported by status byte bit 2.",
            )
        )
        checks.append(
            CheckResult(
                "profile.measurement_baseline",
                ReadinessLayer.CONFIGURATION,
                CheckStatus.WARN,
                False,
                "sample/device-specific current and compliance limits",
                "not selected",
                "Observed source settings are not compared with a sample safety envelope.",
            )
        )

    elif family == "2450":
        source_mode = _decode_2450_source_mode(values.get("source_function", ""))
        checks.extend(
            (
                _check_value(
                    "identity.local_model",
                    ReadinessLayer.IDENTITY,
                    values,
                    "model",
                    target.model,
                    text,
                    lambda value: value == normalize_token(target.model),
                ),
                _check_value(
                    "identity.local_serial",
                    ReadinessLayer.IDENTITY,
                    values,
                    "serial",
                    target.serial,
                    text,
                    lambda value: value == normalize_token(target.serial),
                ),
                _check_value(
                    "identity.local_firmware",
                    ReadinessLayer.IDENTITY,
                    values,
                    "firmware",
                    target.firmware,
                    text,
                    lambda value: value == normalize_token(target.firmware),
                ),
                _check_value(
                    "safety.output_off",
                    ReadinessLayer.ACQUISITION,
                    values,
                    "source_output",
                    0,
                    _integer,
                    lambda value: value == 0,
                ),
                _check_value(
                    "configuration.source_function",
                    ReadinessLayer.CONFIGURATION,
                    values,
                    "source_function",
                    "current or voltage source function",
                    _decode_2450_source_mode,
                    lambda value: value in {"current", "voltage"},
                ),
                _check_value(
                    "configuration.line_frequency_hz",
                    ReadinessLayer.CONFIGURATION,
                    values,
                    "line_frequency_hz",
                    "50 or 60 Hz",
                    _finite_float,
                    lambda value: value in {50.0, 60.0},
                ),
                _check_value(
                    "configuration.terminals",
                    ReadinessLayer.CONFIGURATION,
                    values,
                    "terminals",
                    "front or rear terminal constant",
                    _integer,
                    lambda value: value in {0, 1},
                ),
                _check_value(
                    "configuration.source_off_mode",
                    ReadinessLayer.CONFIGURATION,
                    values,
                    "source_off_mode",
                    "known source-off mode constant",
                    _integer,
                    lambda value: value in {0, 1, 2, 3},
                ),
                _check_value(
                    "configuration.source_level",
                    ReadinessLayer.CONFIGURATION,
                    values,
                    "source_level",
                    "finite source level",
                    _finite_float,
                    lambda _value: True,
                ),
                _check_value(
                    "configuration.source_autorange",
                    ReadinessLayer.CONFIGURATION,
                    values,
                    "source_autorange",
                    "0 or 1",
                    _integer,
                    lambda value: value in {0, 1},
                ),
                _check_value(
                    "configuration.source_range",
                    ReadinessLayer.CONFIGURATION,
                    values,
                    "source_range",
                    "> 0",
                    _finite_float,
                    lambda value: value > 0.0,
                ),
                _check_value(
                    "configuration.measure_function",
                    ReadinessLayer.CONFIGURATION,
                    values,
                    "measure_function",
                    "known measure-function constant",
                    _integer,
                    lambda value: value in {0, 1, 2},
                ),
                _check_value(
                    "configuration.measure_autorange",
                    ReadinessLayer.CONFIGURATION,
                    values,
                    "measure_autorange",
                    "0 or 1",
                    _integer,
                    lambda value: value in {0, 1},
                ),
                _check_value(
                    "configuration.measure_nplc",
                    ReadinessLayer.CONFIGURATION,
                    values,
                    "measure_nplc",
                    "> 0 PLC",
                    _finite_float,
                    lambda value: value > 0.0,
                ),
                _check_value(
                    "configuration.measure_range",
                    ReadinessLayer.CONFIGURATION,
                    values,
                    "measure_range",
                    "> 0",
                    _finite_float,
                    lambda value: value > 0.0,
                ),
                _check_value(
                    "configuration.measure_sense",
                    ReadinessLayer.CONFIGURATION,
                    values,
                    "measure_sense",
                    "2-wire or 4-wire constant",
                    _integer,
                    lambda value: value in {0, 1},
                ),
                _check_value(
                    "configuration.source_readback",
                    ReadinessLayer.CONFIGURATION,
                    values,
                    "source_readback",
                    "0 or 1",
                    _integer,
                    lambda value: value in {0, 1},
                ),
                _check_value(
                    "configuration.measure_filter_enable",
                    ReadinessLayer.CONFIGURATION,
                    values,
                    "measure_filter_enable",
                    "0 or 1",
                    _integer,
                    lambda value: value in {0, 1},
                ),
                _check_value(
                    "configuration.measure_filter_type",
                    ReadinessLayer.CONFIGURATION,
                    values,
                    "measure_filter_type",
                    "non-negative filter type constant",
                    _integer,
                    lambda value: value >= 0,
                ),
                _check_value(
                    "configuration.measure_filter_count",
                    ReadinessLayer.CONFIGURATION,
                    values,
                    "measure_filter_count",
                    "integer 1..100",
                    _integer,
                    lambda value: 1 <= value <= 100,
                ),
                _check_value(
                    "configuration.interlock_enabled",
                    ReadinessLayer.CONFIGURATION,
                    values,
                    "interlock_enabled",
                    "0 or 1",
                    _integer,
                    lambda value: value in {0, 1},
                ),
                _check_value(
                    "safety.interlock_assertion_state",
                    ReadinessLayer.ACQUISITION,
                    values,
                    "interlock_asserted",
                    "0 or 1 (interlock asserted OFF or ON)",
                    _integer,
                    lambda value: value in {0, 1},
                ),
            )
        )
        autozero = _check_value(
            "precision.autozero",
            ReadinessLayer.CONFIGURATION,
            values,
            "measure_autozero",
            1,
            _integer,
            lambda value: value == 1,
            blocks_live=False,
            failure_status=CheckStatus.WARN,
        )
        checks.append(autozero)
        for name, maximum in (
            ("status_condition", 255),
            ("operation_condition", 65535),
            ("questionable_condition", 65535),
        ):
            checks.append(
                _check_value(
                    f"status.{name}",
                    ReadinessLayer.ACQUISITION,
                    values,
                    name,
                    f"integer condition word 0..{maximum}",
                    _integer,
                    lambda value, upper=maximum: 0 <= value <= upper,
                )
            )
        for name, maximum in (
            ("status_condition", 255),
            ("operation_condition", 65535),
            ("questionable_condition", 65535),
        ):
            end_name = f"snapshot_end.{name}"
            if end_name in values:
                checks.append(
                    _check_value(
                        f"status.snapshot_end.{name}",
                        ReadinessLayer.ACQUISITION,
                        values,
                        end_name,
                        f"integer condition word 0..{maximum}",
                        _integer,
                        lambda value, upper=maximum: 0 <= value <= upper,
                    )
                )
        active_tripped_name = {
            "voltage": "current_limit_tripped",
            "current": "voltage_limit_tripped",
        }.get(source_mode)
        active_limit_name = {
            "voltage": "source_current_limit_a",
            "current": "source_voltage_limit_v",
        }.get(source_mode)
        if active_limit_name is not None and active_limit_name in values:
            checks.append(
                _check_value(
                    "configuration.active_limit",
                    ReadinessLayer.CONFIGURATION,
                    values,
                    active_limit_name,
                    "> 0",
                    _finite_float,
                    lambda value: value > 0.0,
                )
            )
        if active_tripped_name is not None and active_tripped_name in values:
            checks.append(
                _check_value(
                    "safety.active_limit_not_reached",
                    ReadinessLayer.ACQUISITION,
                    values,
                    active_tripped_name,
                    0,
                    _integer,
                    lambda value: value == 0,
                )
            )
        if "protection_tripped" in values:
            checks.append(
                _check_value(
                    "safety.protection_not_tripped",
                    ReadinessLayer.ACQUISITION,
                    values,
                    "protection_tripped",
                    0,
                    _integer,
                    lambda value: value == 0,
                )
            )
        if "protection_level_v" in values:
            checks.append(
                _check_value(
                    "configuration.protection_level",
                    ReadinessLayer.CONFIGURATION,
                    values,
                    "protection_level_v",
                    "> 0 V",
                    _finite_float,
                    lambda value: value > 0.0,
                )
            )
        checks.extend(
            (
                CheckResult(
                    "calibration.traceability.asset_record",
                    ReadinessLayer.CONFIGURATION,
                    CheckStatus.UNKNOWN,
                    False,
                    "current external calibration asset record",
                    "not integrated",
                    "2450 calibration validity is not available from the approved TSP snapshot.",
                ),
                CheckResult(
                    "profile.measurement_baseline",
                    ReadinessLayer.CONFIGURATION,
                    CheckStatus.WARN,
                    False,
                    "sample/device-specific source and measure profile",
                    "not selected",
                    "Observed SMU settings are not compared with a sample safety envelope.",
                ),
            )
        )
    else:
        raise ValueError(f"unsupported instrument family: {instrument_family!r}")

    blocking_failure = any(
        check.blocks_live and check.status != CheckStatus.PASS for check in checks
    )
    overall = _aggregate_status(checks)
    if blocking_failure and overall in {CheckStatus.PASS, CheckStatus.WARN}:
        overall = CheckStatus.BLOCKED
    return ReadinessReport(overall, not blocking_failure, tuple(checks))


def target_as_dict(target: DeviceTarget = GPIB6_TARGET) -> dict[str, str]:
    return asdict(target)
