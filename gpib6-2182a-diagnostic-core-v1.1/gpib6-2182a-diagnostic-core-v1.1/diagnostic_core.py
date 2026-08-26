"""Pure diagnostic rules for the fixed GPIB6 Keithley 2182A target.

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


DIAGNOSTIC_SCHEMA_VERSION = 1
PROFILE_ID = "gpib6-2182a-ch1-10mv-nplc5-v1"


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

    def as_dict(self) -> dict[str, object]:
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
            "profile_id": PROFILE_ID,
            "overall": self.overall.value,
            "can_start_live": self.can_start_live,
            "layer_status": layer_status,
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
    except Exception as exc:
        return CheckResult(
            check_id,
            layer,
            CheckStatus.BLOCKED,
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


def _aggregate_status(checks: Sequence[CheckResult]) -> CheckStatus:
    relevant = [check for check in checks if check.status != CheckStatus.NOT_APPLICABLE]
    if any(check.status == CheckStatus.BLOCKED for check in relevant):
        return CheckStatus.BLOCKED
    if any(check.status == CheckStatus.UNKNOWN for check in relevant):
        return CheckStatus.UNKNOWN
    if any(check.status == CheckStatus.WARN for check in relevant):
        return CheckStatus.WARN
    return CheckStatus.PASS


def evaluate_readiness(
    values: Mapping[str, str],
    transcript: Sequence[Mapping[str, object]],
    required_names: Sequence[str],
    *,
    recorder_ready: bool,
    target: DeviceTarget = GPIB6_TARGET,
) -> ReadinessReport:
    checks: list[CheckResult] = []
    transcript_by_name = {
        str(item.get("name")): item for item in transcript if isinstance(item, Mapping)
    }

    checks.append(
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
    )

    elapsed_values: list[float] = []
    for name in required_names:
        item = transcript_by_name.get(name)
        if item is None:
            checks.append(
                CheckResult(
                    f"communication.{name}",
                    ReadinessLayer.COMMUNICATION,
                    CheckStatus.UNKNOWN,
                    True,
                    "successful non-empty response",
                    None,
                    f"No transcript entry exists for required query {name!r}.",
                )
            )
            continue
        response = item.get("response")
        ok = bool(item.get("ok")) and response is not None and bool(str(response).strip())
        checks.append(
            CheckResult(
                f"communication.{name}",
                ReadinessLayer.COMMUNICATION,
                CheckStatus.PASS if ok else CheckStatus.BLOCKED,
                True,
                "successful non-empty response",
                response if response is not None else item.get("error"),
                f"Required query {name!r} completed."
                if ok
                else f"Required query {name!r} failed or returned no data.",
            )
        )
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

    text = lambda raw: normalize_token(str(raw))
    checks.extend(
        (
            _check_value(
                "configuration.scpi_version",
                ReadinessLayer.CONFIGURATION,
                values,
                "scpi_version",
                "1991.0",
                text,
                lambda value: value == "1991.0",
                blocks_live=False,
                failure_status=CheckStatus.WARN,
            ),
            _check_value(
                "configuration.line_frequency_hz",
                ReadinessLayer.CONFIGURATION,
                values,
                "line_frequency_hz",
                50.0,
                _finite_float,
                lambda value: math.isclose(value, 50.0, rel_tol=0.0, abs_tol=1e-9),
            ),
            _check_value(
                "configuration.power_on_setup",
                ReadinessLayer.CONFIGURATION,
                values,
                "power_on_setup",
                "SAV0",
                text,
                lambda value: value == "SAV0",
                blocks_live=False,
                failure_status=CheckStatus.WARN,
            ),
            _check_value(
                "configuration.sense_function",
                ReadinessLayer.ACQUISITION,
                values,
                "sense_function",
                "VOLT:DC",
                text,
                lambda value: value == "VOLT:DC",
            ),
            _check_value(
                "configuration.active_channel",
                ReadinessLayer.ACQUISITION,
                values,
                "active_channel",
                1,
                _integer,
                lambda value: value == 1,
            ),
            _check_value(
                "configuration.nplc",
                ReadinessLayer.ACQUISITION,
                values,
                "nplc",
                5.0,
                _finite_float,
                lambda value: math.isclose(value, 5.0, rel_tol=0.0, abs_tol=1e-9),
            ),
            _check_value(
                "configuration.ch1_range_v",
                ReadinessLayer.ACQUISITION,
                values,
                "ch1_range_v",
                0.01,
                _finite_float,
                lambda value: math.isclose(value, 0.01, rel_tol=0.0, abs_tol=1e-12),
            ),
            _check_value(
                "configuration.ch1_autorange",
                ReadinessLayer.ACQUISITION,
                values,
                "ch1_autorange",
                0,
                _integer,
                lambda value: value == 0,
            ),
            _check_value(
                "configuration.ch1_digital_filter",
                ReadinessLayer.ACQUISITION,
                values,
                "ch1_digital_filter",
                0,
                _integer,
                lambda value: value == 0,
            ),
            _check_value(
                "configuration.ch1_analog_filter",
                ReadinessLayer.ACQUISITION,
                values,
                "ch1_analog_filter",
                0,
                _integer,
                lambda value: value == 0,
            ),
            _check_value(
                "configuration.trigger_count",
                ReadinessLayer.ACQUISITION,
                values,
                "trigger_count",
                "infinite (>= 1e37)",
                _finite_float,
                lambda value: value >= 1e37,
            ),
            _check_value(
                "configuration.trigger_delay_s",
                ReadinessLayer.ACQUISITION,
                values,
                "trigger_delay_s",
                0.0,
                _finite_float,
                lambda value: math.isclose(value, 0.0, rel_tol=0.0, abs_tol=1e-12),
            ),
            _check_value(
                "configuration.trigger_source",
                ReadinessLayer.ACQUISITION,
                values,
                "trigger_source",
                "IMM",
                text,
                lambda value: value == "IMM",
            ),
            _check_value(
                "configuration.sample_count",
                ReadinessLayer.ACQUISITION,
                values,
                "sample_count",
                1,
                _integer,
                lambda value: value == 1,
            ),
            _check_value(
                "configuration.continuous_initiation",
                ReadinessLayer.ACQUISITION,
                values,
                "continuous_initiation",
                1,
                _integer,
                lambda value: value == 1,
            ),
            _check_value(
                "configuration.data_format",
                ReadinessLayer.ACQUISITION,
                values,
                "data_format",
                "ASC",
                text,
                lambda value: value == "ASC",
            ),
            _check_value(
                "configuration.format_elements",
                ReadinessLayer.ACQUISITION,
                values,
                "format_elements",
                "READ",
                text,
                lambda value: value == "READ",
            ),
        )
    )

    ch2_validators = {
        "ch2_range_v": lambda raw: _finite_float(raw) > 0.0,
        "ch2_autorange": lambda raw: _integer(raw) in {0, 1},
        "ch2_digital_filter": lambda raw: _integer(raw) in {0, 1},
        "ch2_analog_filter": lambda raw: _integer(raw) in {0, 1},
    }
    for name, validator in ch2_validators.items():
        raw = values.get(name)
        valid = False
        if raw is not None and str(raw).strip():
            try:
                valid = bool(validator(raw))
            except Exception:
                valid = False
        checks.append(
            CheckResult(
                f"configuration.{name}",
                ReadinessLayer.CONFIGURATION,
                CheckStatus.NOT_APPLICABLE if valid else CheckStatus.WARN,
                False,
                "valid record-only response; CH2 is not the active GPIB6 live channel",
                raw,
                (
                    f"{name} is preserved as valid evidence but does not gate CH1 live observation."
                    if valid
                    else f"{name} is invalid record-only evidence; CH1 live remains non-blocking."
                ),
            )
        )

    blocking_failure = any(
        check.blocks_live and check.status != CheckStatus.PASS for check in checks
    )
    overall = _aggregate_status(checks)
    if blocking_failure and overall in {CheckStatus.PASS, CheckStatus.WARN}:
        overall = CheckStatus.BLOCKED
    return ReadinessReport(overall, not blocking_failure, tuple(checks))


def target_as_dict(target: DeviceTarget = GPIB6_TARGET) -> dict[str, str]:
    return asdict(target)
