"""Deterministic, simulation-only fault scenarios for GPIB6 diagnostics."""

from __future__ import annotations

import math
import random
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Iterable


SIMULATION_SEED = 2182


class FaultAction(str, Enum):
    RETURN = "return"
    TIMEOUT = "timeout"
    DELAY = "delay"
    DISCONNECT = "disconnect"


@dataclass(frozen=True)
class FaultRule:
    rule_id: str
    phase: str
    command: str
    occurrence: int
    action: FaultAction
    value: object = None


@dataclass(frozen=True)
class FaultScenario:
    scenario_id: str
    description: str
    rules: tuple[FaultRule, ...] = ()
    fail_csv_open: bool = False
    fail_csv_write_after_samples: int | None = None
    fail_event_write_after: int | None = None
    fail_config_session_close: bool = False


def _rule(
    rule_id: str,
    phase: str,
    command: str,
    occurrence: int,
    action: FaultAction,
    value: object = None,
) -> FaultRule:
    return FaultRule(rule_id, phase, command, occurrence, action, value)


FAULT_SCENARIOS: dict[str, FaultScenario] = {
    "nominal": FaultScenario("nominal", "Healthy deterministic simulation."),
    "wrong_identity": FaultScenario(
        "wrong_identity",
        "Return the known GPIB7 2182A identity at the fixed GPIB6 target.",
        (
            _rule(
                "wrong_identity.config",
                "config",
                "*IDN?",
                1,
                FaultAction.RETURN,
                "KEITHLEY INSTRUMENTS INC.,MODEL 2182A,4510267,C08/B01",
            ),
        ),
    ),
    "malformed_identity": FaultScenario(
        "malformed_identity",
        "Return a malformed identity with a serial substring instead of an exact field.",
        (
            _rule(
                "malformed_identity.config",
                "config",
                "*IDN?",
                1,
                FaultAction.RETURN,
                "MODEL 2182A,X1340129X",
            ),
        ),
    ),
    "6221_output_on": FaultScenario(
        "6221_output_on",
        "Return an enabled 6221 output readback.",
        (_rule("6221_output_on.config", "config", "OUTP?", 1, FaultAction.RETURN, "1"),),
    ),
    "6221_interlock_invalid": FaultScenario(
        "6221_interlock_invalid",
        "Return a 6221 interlock state outside the documented 0/1 domain.",
        (
            _rule(
                "6221_interlock_invalid.config",
                "config",
                "OUTP:INTERLOCK:TRIPPED?",
                1,
                FaultAction.RETURN,
                "2",
            ),
        ),
    ),
    "6221_over_temperature": FaultScenario(
        "6221_over_temperature",
        "Set the 6221 measurement-condition over-temperature bit.",
        (
            _rule(
                "6221_over_temperature.config",
                "config",
                "STAT:MEAS:COND?",
                1,
                FaultAction.RETURN,
                "4",
            ),
        ),
    ),
    "6221_compliance_active": FaultScenario(
        "6221_compliance_active",
        "Set the 6221 measurement-condition compliance bit.",
        (
            _rule(
                "6221_compliance_active.config",
                "config",
                "STAT:MEAS:COND?",
                1,
                FaultAction.RETURN,
                "8",
            ),
        ),
    ),
    "6221_calibration_questionable": FaultScenario(
        "6221_calibration_questionable",
        "Set the 6221 questionable-condition calibration bit.",
        (
            _rule(
                "6221_calibration_questionable.config",
                "config",
                "STAT:QUES:COND?",
                1,
                FaultAction.RETURN,
                "256",
            ),
        ),
    ),
    "6221_invalid_response": FaultScenario(
        "6221_invalid_response",
        "Return an invalid 6221 output-response token.",
        (
            _rule(
                "6221_invalid_response.config",
                "config",
                "OUTP:RESPONSE?",
                1,
                FaultAction.RETURN,
                "garbage",
            ),
        ),
    ),
    "6221_configuration_timeout": FaultScenario(
        "6221_configuration_timeout",
        "Raise a timeout while reading the 6221 operation condition.",
        (
            _rule(
                "6221_configuration_timeout.config",
                "config",
                "STAT:OPER:COND?",
                1,
                FaultAction.TIMEOUT,
            ),
        ),
    ),
    "2450_output_on": FaultScenario(
        "2450_output_on",
        "Return an enabled 2450 source-output readback.",
        (
            _rule(
                "2450_output_on.config",
                "config",
                "print(smu.source.output)",
                1,
                FaultAction.RETURN,
                "1",
            ),
        ),
    ),
    "2450_invalid_source_mode": FaultScenario(
        "2450_invalid_source_mode",
        "Return an invalid 2450 source-function token.",
        (
            _rule(
                "2450_invalid_source_mode.config",
                "config",
                "print(smu.source.func)",
                1,
                FaultAction.RETURN,
                "garbage",
            ),
        ),
    ),
    "2450_active_limit": FaultScenario(
        "2450_active_limit",
        "Return an active 2450 current-limit readback for voltage-source mode.",
        (
            _rule(
                "2450_active_limit.config",
                "config",
                "print(smu.source.ilimit.tripped)",
                1,
                FaultAction.RETURN,
                "1",
            ),
        ),
    ),
    "2450_protection_tripped": FaultScenario(
        "2450_protection_tripped",
        "Return a tripped 2450 overvoltage-protection readback.",
        (
            _rule(
                "2450_protection_tripped.config",
                "config",
                "print(smu.source.protect.tripped)",
                1,
                FaultAction.RETURN,
                "1",
            ),
        ),
    ),
    "2450_interlock_invalid": FaultScenario(
        "2450_interlock_invalid",
        "Return a 2450 interlock assertion state outside the documented 0/1 domain.",
        (
            _rule(
                "2450_interlock_invalid.config",
                "config",
                "print(smu.interlock.tripped)",
                1,
                FaultAction.RETURN,
                "2",
            ),
        ),
    ),
    "2450_invalid_response": FaultScenario(
        "2450_invalid_response",
        "Return a non-numeric 2450 source-level response.",
        (
            _rule(
                "2450_invalid_response.config",
                "config",
                "print(smu.source.level)",
                1,
                FaultAction.RETURN,
                "garbage",
            ),
        ),
    ),
    "2450_configuration_timeout": FaultScenario(
        "2450_configuration_timeout",
        "Raise a timeout while reading the 2450 source level.",
        (
            _rule(
                "2450_configuration_timeout.config",
                "config",
                "print(smu.source.level)",
                1,
                FaultAction.TIMEOUT,
            ),
        ),
    ),
    "configuration_missing": FaultScenario(
        "configuration_missing",
        "Return an empty active-channel response.",
        (
            _rule(
                "configuration_missing.channel",
                "config",
                "SENS:CHAN?",
                1,
                FaultAction.RETURN,
                "",
            ),
        ),
    ),
    "configuration_drift": FaultScenario(
        "configuration_drift",
        "Return different active channels at the begin and end snapshots.",
        (
            _rule(
                "configuration_drift.begin_channel",
                "config",
                "SENS:CHAN?",
                1,
                FaultAction.RETURN,
                "2",
            ),
        ),
    ),
    "configuration_timeout": FaultScenario(
        "configuration_timeout",
        "Raise a timeout while reading NPLC.",
        (
            _rule(
                "configuration_timeout.nplc",
                "config",
                "SENS:VOLT:DC:NPLC?",
                1,
                FaultAction.TIMEOUT,
            ),
        ),
    ),
    "configuration_slow": FaultScenario(
        "configuration_slow",
        "Delay one NPLC response beyond the diagnostic warning threshold.",
        (
            _rule(
                "configuration_slow.nplc",
                "config",
                "SENS:VOLT:DC:NPLC?",
                1,
                FaultAction.DELAY,
                0.65,
            ),
        ),
    ),
    "configuration_close_failure": FaultScenario(
        "configuration_close_failure",
        "Fail while closing the simulated diagnostic session after all queries.",
        fail_config_session_close=True,
    ),
    "fetch_timeout": FaultScenario(
        "fetch_timeout",
        "Raise a timeout on the third live FETCh?.",
        (_rule("fetch_timeout.third", "stream", "FETCh?", 3, FaultAction.TIMEOUT),),
    ),
    "fetch_slow": FaultScenario(
        "fetch_slow",
        "Delay the second live FETCh? beyond the 0.25 s polling interval.",
        (_rule("fetch_slow.second", "stream", "FETCh?", 2, FaultAction.DELAY, 0.35),),
    ),
    "fetch_malformed": FaultScenario(
        "fetch_malformed",
        "Return a non-numeric second live response.",
        (_rule("fetch_malformed.second", "stream", "FETCh?", 2, FaultAction.RETURN, "not-a-number"),),
    ),
    "fetch_nan": FaultScenario(
        "fetch_nan",
        "Return NaN on the second live response.",
        (_rule("fetch_nan.second", "stream", "FETCh?", 2, FaultAction.RETURN, "+NAN"),),
    ),
    "fetch_inf": FaultScenario(
        "fetch_inf",
        "Return infinity on the second live response.",
        (_rule("fetch_inf.second", "stream", "FETCh?", 2, FaultAction.RETURN, "+INF"),),
    ),
    "fetch_overrange": FaultScenario(
        "fetch_overrange",
        "Return a 9.9e37-style overrange sentinel on the second live response.",
        (_rule("fetch_overrange.second", "stream", "FETCh?", 2, FaultAction.RETURN, "+9.9E37"),),
    ),
    "disconnect_after_3": FaultScenario(
        "disconnect_after_3",
        "Disconnect on the third live FETCh?.",
        (_rule("disconnect_after_3.third", "stream", "FETCh?", 3, FaultAction.DISCONNECT),),
    ),
    "csv_open_failure": FaultScenario(
        "csv_open_failure",
        "Fail before opening the CSV; no live session may be opened.",
        fail_csv_open=True,
    ),
    "csv_write_failure": FaultScenario(
        "csv_write_failure",
        "Fail before writing the third voltage sample.",
        fail_csv_write_after_samples=2,
    ),
    "event_write_failure": FaultScenario(
        "event_write_failure",
        "Fail the event journal after three committed events.",
        fail_event_write_after=3,
    ),
}


FAULT_SCENARIO_NAMES = tuple(FAULT_SCENARIOS)


class SimulationContext:
    """Shared deterministic state for configuration and live simulated sessions."""

    def __init__(
        self,
        scenario_id: str,
        allowed_queries: Iterable[str],
        *,
        seed: int = SIMULATION_SEED,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        try:
            self.scenario = FAULT_SCENARIOS[scenario_id]
        except KeyError as exc:
            raise ValueError(f"unknown fault scenario: {scenario_id!r}") from exc
        self.allowed_queries = frozenset(allowed_queries)
        for rule in self.scenario.rules:
            if rule.command not in self.allowed_queries:
                raise ValueError(
                    f"fault rule {rule.rule_id!r} targets non-allow-listed command {rule.command!r}"
                )
            if rule.phase not in {"config", "stream"}:
                raise ValueError(f"unsupported fault phase in {rule.rule_id!r}: {rule.phase!r}")
            if rule.occurrence < 1:
                raise ValueError(f"fault occurrence must be positive in {rule.rule_id!r}")
        self.seed = seed
        self.random = random.Random(seed)
        self.sleep = sleep
        self._lock = threading.Lock()
        self._counts: dict[tuple[str, str], int] = {}
        self.query_history: list[dict[str, object]] = []
        self.consumed_rule_ids: list[str] = []
        self.fetch_index = 0
        self.disconnected = False

    @property
    def scenario_id(self) -> str:
        return self.scenario.scenario_id

    @property
    def event_write_fail_after(self) -> int | None:
        return self.scenario.fail_event_write_after

    def should_fail_csv_open(self) -> bool:
        return self.scenario.fail_csv_open

    def should_fail_config_session_close(self) -> bool:
        if not self.scenario.fail_config_session_close:
            return False
        rule_id = "configuration_close_failure.close"
        with self._lock:
            if rule_id not in self.consumed_rule_ids:
                self.consumed_rule_ids.append(rule_id)
        return True

    def should_fail_csv_write(self, completed_sample_count: int) -> bool:
        threshold = self.scenario.fail_csv_write_after_samples
        return threshold is not None and completed_sample_count >= threshold

    def execute_query(self, phase: str, command: str, nominal: Callable[[], str]) -> str:
        if command not in self.allowed_queries:
            raise ValueError(f"simulation blocked non-allow-listed command: {command!r}")
        with self._lock:
            if self.disconnected:
                raise ConnectionError("simulated session remains disconnected")
            key = (phase, command)
            occurrence = self._counts.get(key, 0) + 1
            self._counts[key] = occurrence
            self.query_history.append(
                {"phase": phase, "command": command, "occurrence": occurrence}
            )
            matched = next(
                (
                    rule
                    for rule in self.scenario.rules
                    if rule.phase == phase
                    and rule.command == command
                    and rule.occurrence == occurrence
                ),
                None,
            )
            if matched is not None:
                self.consumed_rule_ids.append(matched.rule_id)

        if matched is not None:
            if matched.action == FaultAction.DELAY:
                self.sleep(float(matched.value))
            elif matched.action == FaultAction.RETURN:
                return str(matched.value)
            elif matched.action == FaultAction.TIMEOUT:
                raise TimeoutError(f"simulated timeout: {matched.rule_id}")
            elif matched.action == FaultAction.DISCONNECT:
                with self._lock:
                    self.disconnected = True
                raise ConnectionError(f"simulated disconnect: {matched.rule_id}")
        return nominal()

    def next_voltage(self) -> str:
        with self._lock:
            self.fetch_index += 1
            elapsed = self.fetch_index * 0.25
            voltage = 1.2e-7 + 4e-8 * math.sin(elapsed / 3.0) + self.random.gauss(0.0, 5e-9)
        return f"{voltage:+.8E}"


def scenario_descriptions() -> tuple[tuple[str, str], ...]:
    return tuple((name, FAULT_SCENARIOS[name].description) for name in FAULT_SCENARIO_NAMES)
