"""Reusable, query-only diagnostic profiles resolved by exact instrument model.

This module is deliberately independent from the existing GPIB6 monitor.  It
contains no VISA dependency, no discovery code, no generic write API, and no
instrument state changes.  A request is accepted only when the complete string
is present in the selected target's exact, phase-specific allowlist.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Callable, Mapping


class UnsafeReadTransaction(ValueError):
    """Raised when a request is not an exact approved read transaction."""


class CommandSet(str, Enum):
    SCPI = "SCPI"
    TSP = "TSP"


@dataclass(frozen=True)
class InstrumentTarget:
    key: str
    label: str
    resource: str
    vendor: str
    model: str
    serial: str
    firmware: str
    profile_key: str | None
    live_supported: bool = False


@dataclass(frozen=True)
class QuerySpec:
    name: str
    command: str
    group: str
    label: str
    condition: str | None = None


@dataclass(frozen=True)
class SummaryRow:
    key: str
    label: str
    value: str
    source_names: tuple[str, ...] = ()


SummaryBuilder = Callable[
    [InstrumentTarget, Mapping[str, object]],
    tuple[SummaryRow, ...],
]


@dataclass(frozen=True)
class DiagnosticProfile:
    key: str
    model: str
    command_set: CommandSet
    diagnostic_queries: tuple[QuerySpec, ...]
    summary_builder: SummaryBuilder
    live_query: QuerySpec | None = None
    snapshot_end_names: tuple[str, ...] = ()
    consistency_names: tuple[str, ...] = ()

    @property
    def diagnostic_commands(self) -> tuple[str, ...]:
        return tuple(spec.command for spec in self.diagnostic_queries)

    def summary_rows(
        self,
        target: InstrumentTarget,
        values: Mapping[str, object],
    ) -> tuple[SummaryRow, ...]:
        if target.profile_key != self.key:
            raise ValueError(
                f"target {target.key!r} does not use profile {self.key!r}"
            )
        return self.summary_builder(target, values)


def _q(
    name: str,
    command: str,
    group: str,
    label: str,
    condition: str | None = None,
) -> QuerySpec:
    return QuerySpec(name, command, group, label, condition)


# The status condition registers are real-time, non-consuming reads.  Their
# corresponding event-register queries remain forbidden because those clear on
# read.  FETCh? is declared separately as a live transaction.
Q2182A = (
    _q("identity", "*IDN?", "identity", "Exact identity"),
    _q("scpi_version", "SYST:VERS?", "system", "SCPI version"),
    _q("line_frequency_hz", "SYST:LFREQUENCY?", "system", "Line frequency"),
    _q("power_on_setup", "SYST:POSETUP?", "system", "Power-on setup"),
    _q("system_autozero", "SYST:AZERO?", "precision", "System autozero"),
    _q("front_autozero", "SYST:FAZERO?", "precision", "Front autozero"),
    _q("line_sync", "SYST:LSYNC?", "precision", "Line synchronization"),
    _q("sense_function", "SENS:FUNC?", "sense", "Sense function"),
    _q("active_channel", "SENS:CHAN?", "sense", "Active channel"),
    _q("nplc", "SENS:VOLT:DC:NPLC?", "sense", "Integration (NPLC)"),
    _q("ch1_range_v", "SENS:VOLT:DC:CHAN1:RANG?", "sense", "CH1 range"),
    _q(
        "ch1_autorange",
        "SENS:VOLT:DC:CHAN1:RANG:AUTO?",
        "sense",
        "CH1 autorange",
    ),
    _q("ch2_range_v", "SENS:VOLT:DC:CHAN2:RANG?", "sense", "CH2 range"),
    _q(
        "ch2_autorange",
        "SENS:VOLT:DC:CHAN2:RANG:AUTO?",
        "sense",
        "CH2 autorange",
    ),
    _q(
        "ch1_digital_filter",
        "SENS:VOLT:DC:CHAN1:DFILTER?",
        "filter",
        "CH1 digital filter",
    ),
    _q(
        "ch1_analog_filter",
        "SENS:VOLT:DC:CHAN1:LPASS?",
        "filter",
        "CH1 analog low-pass",
    ),
    _q(
        "ch2_digital_filter",
        "SENS:VOLT:DC:CHAN2:DFILTER?",
        "filter",
        "CH2 digital filter",
    ),
    _q(
        "ch2_analog_filter",
        "SENS:VOLT:DC:CHAN2:LPASS?",
        "filter",
        "CH2 analog low-pass",
    ),
    _q("trigger_count", "TRIG:COUNT?", "trigger", "Trigger count"),
    _q("trigger_delay_s", "TRIG:DELAY?", "trigger", "Trigger delay"),
    _q("trigger_source", "TRIG:SOURCE?", "trigger", "Trigger source"),
    _q("sample_count", "SAMP:COUN?", "trigger", "Sample count"),
    _q("continuous_initiation", "INIT:CONT?", "trigger", "Continuous initiation"),
    _q("operation_condition", "STAT:OPER:COND?", "status", "Operation condition"),
    _q("measurement_condition", "STAT:MEAS:COND?", "status", "Measurement condition"),
    _q("questionable_condition", "STAT:QUES:COND?", "status", "Questionable condition"),
    _q("data_format", "FORM:DATA?", "format", "Data format"),
    _q("format_elements", "FORM:ELEM?", "format", "Format elements"),
)


# Conservative 6221 set: no DC-current setpoint query is included because its
# exact field command is not yet approved for this diagnostic core.  Condition
# registers are non-consuming; event registers and error queues remain absent.
Q6221 = (
    _q("identity", "*IDN?", "identity", "Exact identity"),
    _q("output_enabled", "OUTP?", "output", "Output enabled"),
    _q(
        "interlock_tripped_raw",
        "OUTP:INTERLOCK:TRIPPED?",
        "output",
        "Interlock raw state",
    ),
    _q("operation_condition", "STAT:OPER:COND?", "status", "Operation condition"),
    _q("measurement_condition", "STAT:MEAS:COND?", "status", "Measurement condition"),
    _q("questionable_condition", "STAT:QUES:COND?", "status", "Questionable condition"),
    _q("status_byte", "*STB?", "status", "Status byte"),
    _q("current_range_a", "SOUR:CURR:RANG?", "source", "Current range"),
    _q(
        "current_range_auto",
        "SOUR:CURR:RANG:AUTO?",
        "source",
        "Current autorange",
    ),
    _q(
        "voltage_compliance_v",
        "SOUR:CURR:COMP?",
        "source",
        "Voltage compliance",
    ),
    _q("analog_filter", "SOUR:CURR:FILT?", "filter", "Analog filter"),
    _q("output_response", "OUTP:RESPONSE?", "output", "Output response"),
    _q("triax_inner_shield", "OUTP:ISHIELD?", "output", "Triax inner shield"),
    _q("output_low_to_earth", "OUTP:LTEARTH?", "output", "Output low to earth"),
)


# TSP requests are intentionally limited to simple print(attribute.path)
# expressions.  No call expression, assignment, read(), event register, status
# event, or arbitrary TSP statement is part of this profile.
Q2450 = (
    _q("identity", "*IDN?", "identity", "Exact identity"),
    _q("model", "print(localnode.model)", "identity", "Model"),
    _q("serial", "print(localnode.serialno)", "identity", "Serial number"),
    _q("firmware", "print(localnode.version)", "identity", "Firmware"),
    _q("line_frequency_hz", "print(localnode.linefreq)", "system", "Line frequency"),
    _q("terminals", "print(smu.terminals)", "routing", "Terminals"),
    _q("source_output", "print(smu.source.output)", "output", "Source output"),
    _q("source_off_mode", "print(smu.source.offmode)", "output", "Source off mode"),
    _q("source_function", "print(smu.source.func)", "source", "Source function"),
    _q("source_level", "print(smu.source.level)", "source", "Source level"),
    _q("source_autorange", "print(smu.source.autorange)", "source", "Source autorange"),
    _q("source_range", "print(smu.source.range)", "source", "Source range"),
    _q("measure_function", "print(smu.measure.func)", "sense", "Measure function"),
    _q("measure_sense", "print(smu.measure.sense)", "sense", "Configured sense mode"),
    _q("measure_autorange", "print(smu.measure.autorange)", "sense", "Measure autorange"),
    _q("measure_range", "print(smu.measure.range)", "sense", "Measure range"),
    _q("measure_nplc", "print(smu.measure.nplc)", "sense", "Measure NPLC"),
    _q(
        "measure_autozero",
        "print(smu.measure.autozero.enable)",
        "sense",
        "Measure autozero",
    ),
    _q("source_readback", "print(smu.source.readback)", "source", "Source readback"),
    _q(
        "measure_filter_enable",
        "print(smu.measure.filter.enable)",
        "filter",
        "Measure filter enabled",
    ),
    _q(
        "measure_filter_type",
        "print(smu.measure.filter.type)",
        "filter",
        "Measure filter type",
    ),
    _q(
        "measure_filter_count",
        "print(smu.measure.filter.count)",
        "filter",
        "Measure filter count",
    ),
    _q(
        "source_current_limit_a",
        "print(smu.source.ilimit.level)",
        "limit",
        "Source current limit",
        "source_voltage",
    ),
    _q(
        "current_limit_tripped",
        "print(smu.source.ilimit.tripped)",
        "limit",
        "Current limit reached",
        "source_voltage",
    ),
    _q(
        "source_voltage_limit_v",
        "print(smu.source.vlimit.level)",
        "limit",
        "Source voltage limit",
        "source_current",
    ),
    _q(
        "voltage_limit_tripped",
        "print(smu.source.vlimit.tripped)",
        "limit",
        "Voltage limit reached",
        "source_current",
    ),
    _q(
        "protection_level_v",
        "print(smu.source.protect.level)",
        "limit",
        "Overvoltage protection",
    ),
    _q(
        "protection_tripped",
        "print(smu.source.protect.tripped)",
        "limit",
        "Overvoltage protection reached",
    ),
    _q(
        "interlock_enabled",
        "print(smu.interlock.enable)",
        "output",
        "Interlock enabled",
    ),
    _q(
        "interlock_asserted",
        "print(smu.interlock.tripped)",
        "output",
        "Interlock asserted",
    ),
    _q(
        "status_condition",
        "print(status.condition)",
        "status",
        "Status byte condition",
    ),
    _q(
        "operation_condition",
        "print(status.operation.condition)",
        "status",
        "Operation condition",
    ),
    _q(
        "questionable_condition",
        "print(status.questionable.condition)",
        "status",
        "Questionable condition",
    ),
)


def _text(values: Mapping[str, object], name: str) -> str:
    value = values.get(name)
    if value is None:
        return "—"
    rendered = str(value).strip()
    return rendered if rendered else "—"


def _on_off(raw: str) -> str:
    normalized = raw.strip().upper()
    if normalized in {"0", "OFF", "SMU.OFF"}:
        return "OFF"
    if normalized in {"1", "ON", "SMU.ON"}:
        return "ON"
    return raw


def _autorange(raw: str) -> str:
    state = _on_off(raw)
    return f"autorange {state}"


def _integration(nplc_raw: str, line_hz_raw: str) -> str:
    if nplc_raw == "—":
        return "—"
    try:
        nplc = float(nplc_raw)
        line_hz = float(line_hz_raw)
        seconds = nplc / line_hz
        if not all(math.isfinite(item) for item in (nplc, line_hz, seconds)) or line_hz <= 0:
            raise ValueError
    except (TypeError, ValueError, ZeroDivisionError):
        return f"{nplc_raw} NPLC; integration time unavailable"
    return f"{nplc_raw} NPLC (~{seconds:.6g} s at {line_hz_raw} Hz)"


def _summary_2182a(
    target: InstrumentTarget,
    values: Mapping[str, object],
) -> tuple[SummaryRow, ...]:
    channel = _text(values, "active_channel")
    if channel in {"1", "+1", "1.0"}:
        channel_number = "1"
    elif channel in {"2", "+2", "2.0"}:
        channel_number = "2"
    else:
        channel_number = "?"
    if channel_number in {"1", "2"}:
        range_name = f"ch{channel_number}_range_v"
        auto_name = f"ch{channel_number}_autorange"
        range_value = (
            f"CH{channel_number}: {_text(values, range_name)} V; "
            f"{_autorange(_text(values, auto_name))}"
        )
        filter_value = (
            f"CH{channel_number}: digital "
            f"{_on_off(_text(values, f'ch{channel_number}_digital_filter'))}; "
            f"analog low-pass "
            f"{_on_off(_text(values, f'ch{channel_number}_analog_filter'))}"
        )
        range_sources = ("active_channel", range_name, auto_name)
        filter_sources = (
            "active_channel",
            f"ch{channel_number}_digital_filter",
            f"ch{channel_number}_analog_filter",
        )
    else:
        range_value = (
            f"active channel {channel}; CH1 {_text(values, 'ch1_range_v')} V; "
            f"CH2 {_text(values, 'ch2_range_v')} V"
        )
        filter_value = "active channel unavailable"
        range_sources = (
            "active_channel",
            "ch1_range_v",
            "ch1_autorange",
            "ch2_range_v",
            "ch2_autorange",
        )
        filter_sources = (
            "active_channel",
            "ch1_digital_filter",
            "ch1_analog_filter",
            "ch2_digital_filter",
            "ch2_analog_filter",
        )
    return (
        SummaryRow("identity", "Exact identity", _text(values, "identity"), ("identity",)),
        SummaryRow("accuracy_noise", "Accuracy / noise", "not characterized"),
        SummaryRow(
            "function_channel",
            "Function / active channel",
            f"{_text(values, 'sense_function')} / CH{channel}",
            ("sense_function", "active_channel"),
        ),
        SummaryRow("range", "Active range", range_value, range_sources),
        SummaryRow(
            "integration",
            "NPLC / integration",
            _integration(_text(values, "nplc"), _text(values, "line_frequency_hz")),
            ("nplc", "line_frequency_hz"),
        ),
        SummaryRow("filter", "Filter", filter_value, filter_sources),
        SummaryRow(
            "zero_sync",
            "Autozero / line sync",
            (
                f"system {_on_off(_text(values, 'system_autozero'))}; "
                f"front {_on_off(_text(values, 'front_autozero'))}; "
                f"LSYNC {_on_off(_text(values, 'line_sync'))}"
            ),
            ("system_autozero", "front_autozero", "line_sync"),
        ),
        SummaryRow("remote_sense", "Remote sense", "N/A"),
        SummaryRow("compliance", "Compliance", "N/A"),
        SummaryRow(
            "trigger",
            "Trigger model",
            (
                f"source {_text(values, 'trigger_source')}; "
                f"count {_text(values, 'trigger_count')}; "
                f"delay {_text(values, 'trigger_delay_s')} s; "
                f"continuous {_on_off(_text(values, 'continuous_initiation'))}"
            ),
            (
                "trigger_source",
                "trigger_count",
                "trigger_delay_s",
                "continuous_initiation",
            ),
        ),
        SummaryRow(
            "status_conditions",
            "Condition registers",
            (
                f"OPER {_text(values, 'operation_condition')}; "
                f"MEAS {_text(values, 'measurement_condition')}; "
                f"QUES {_text(values, 'questionable_condition')}"
            ),
            (
                "operation_condition",
                "measurement_condition",
                "questionable_condition",
            ),
        ),
    )


def _summary_6221(
    target: InstrumentTarget,
    values: Mapping[str, object],
) -> tuple[SummaryRow, ...]:
    output = _on_off(_text(values, "output_enabled"))
    interlock_raw = _text(values, "interlock_tripped_raw")
    interlock = {
        "0": "open/tripped",
        "1": "closed/asserted",
    }.get(interlock_raw, "unknown")
    if output == "OFF":
        compliance_state = "N/A (output OFF)"
    else:
        try:
            condition = int(float(_text(values, "measurement_condition")))
            compliance_state = "ON" if condition & (1 << 3) else "OFF"
        except (TypeError, ValueError):
            compliance_state = "unknown"
    return (
        SummaryRow("identity", "Exact identity", _text(values, "identity"), ("identity",)),
        SummaryRow("accuracy_noise", "Accuracy / noise", "not characterized"),
        SummaryRow(
            "output",
            "Output / interlock",
            (
                f"output {output}; interlock {interlock} (raw {interlock_raw})"
            ),
            ("output_enabled", "interlock_tripped_raw"),
        ),
        SummaryRow(
            "range",
            "Current range",
            (
                f"{_text(values, 'current_range_a')} A; "
                f"{_autorange(_text(values, 'current_range_auto'))}"
            ),
            ("current_range_a", "current_range_auto"),
        ),
        SummaryRow("integration", "NPLC / integration", "N/A"),
        SummaryRow(
            "filter",
            "Analog filter",
            _on_off(_text(values, "analog_filter")),
            ("analog_filter",),
        ),
        SummaryRow("remote_sense", "Remote sense", "N/A"),
        SummaryRow(
            "compliance",
            "Voltage compliance",
            (
                f"limit {_text(values, 'voltage_compliance_v')} V; "
                f"active {compliance_state}"
            ),
            ("voltage_compliance_v", "measurement_condition", "output_enabled"),
        ),
        SummaryRow(
            "output_path",
            "Response / guard / low-earth",
            (
                f"response {_text(values, 'output_response')}; "
                f"inner shield {_text(values, 'triax_inner_shield')}; "
                f"low-earth {_on_off(_text(values, 'output_low_to_earth'))}"
            ),
            ("output_response", "triax_inner_shield", "output_low_to_earth"),
        ),
        SummaryRow(
            "status_conditions",
            "Condition registers",
            (
                f"OPER {_text(values, 'operation_condition')}; "
                f"MEAS {_text(values, 'measurement_condition')}; "
                f"QUES {_text(values, 'questionable_condition')}; "
                f"STB {_text(values, 'status_byte')}"
            ),
            (
                "operation_condition",
                "measurement_condition",
                "questionable_condition",
                "status_byte",
            ),
        ),
    )


def _source_mode(raw: str) -> str | None:
    normalized = raw.strip().upper()
    if normalized == "0":
        return "current"
    if normalized == "1":
        return "voltage"
    return None


def _summary_2450(
    target: InstrumentTarget,
    values: Mapping[str, object],
) -> tuple[SummaryRow, ...]:
    output = _on_off(_text(values, "source_output"))
    configured_sense = _text(values, "measure_sense")
    if output == "OFF":
        sense = f"2-wire effective while output OFF; configured {configured_sense}"
    else:
        sense = f"configured {configured_sense}"

    source_function = _text(values, "source_function")
    mode = _source_mode(source_function)
    if mode == "voltage":
        compliance = (
            f"current limit {_text(values, 'source_current_limit_a')} A; "
            f"reached {_on_off(_text(values, 'current_limit_tripped'))}"
        )
        compliance_sources = (
            "source_function",
            "source_current_limit_a",
            "current_limit_tripped",
        )
    elif mode == "current":
        compliance = (
            f"voltage limit {_text(values, 'source_voltage_limit_v')} V; "
            f"reached {_on_off(_text(values, 'voltage_limit_tripped'))}"
        )
        compliance_sources = (
            "source_function",
            "source_voltage_limit_v",
            "voltage_limit_tripped",
        )
    else:
        compliance = (
            f"source function {source_function}; current limit "
            f"{_text(values, 'source_current_limit_a')} A; voltage limit "
            f"{_text(values, 'source_voltage_limit_v')} V"
        )
        compliance_sources = (
            "source_function",
            "source_current_limit_a",
            "source_voltage_limit_v",
        )

    identity = _text(values, "identity")
    if identity == "—":
        identity = (
            f"{_text(values, 'model')} / {_text(values, 'serial')} / "
            f"{_text(values, 'firmware')}"
        )
    return (
        SummaryRow(
            "identity",
            "Exact identity",
            identity,
            ("identity", "model", "serial", "firmware"),
        ),
        SummaryRow("accuracy_noise", "Accuracy / noise", "not characterized"),
        SummaryRow(
            "output",
            "Source output / interlock",
            (
                f"{output}; off mode {_text(values, 'source_off_mode')}; "
                f"interlock enabled {_on_off(_text(values, 'interlock_enabled'))}; "
                f"interlock asserted {_on_off(_text(values, 'interlock_asserted'))}"
            ),
            (
                "source_output",
                "source_off_mode",
                "interlock_enabled",
                "interlock_asserted",
            ),
        ),
        SummaryRow(
            "source",
            "Source function / level",
            (
                f"function {source_function}; level {_text(values, 'source_level')}; "
                f"readback {_on_off(_text(values, 'source_readback'))}"
            ),
            ("source_function", "source_level", "source_readback"),
        ),
        SummaryRow(
            "range",
            "Source / measure range",
            (
                f"source {_text(values, 'source_range')} "
                f"({_autorange(_text(values, 'source_autorange'))}); "
                f"measure {_text(values, 'measure_function')} range "
                f"{_text(values, 'measure_range')} "
                f"({_autorange(_text(values, 'measure_autorange'))})"
            ),
            (
                "source_range",
                "source_autorange",
                "measure_function",
                "measure_range",
                "measure_autorange",
            ),
        ),
        SummaryRow(
            "integration",
            "NPLC / integration",
            _integration(
                _text(values, "measure_nplc"),
                _text(values, "line_frequency_hz"),
            ),
            ("measure_nplc", "line_frequency_hz"),
        ),
        SummaryRow(
            "filter",
            "Measure filter",
            (
                f"{_on_off(_text(values, 'measure_filter_enable'))}; "
                f"type {_text(values, 'measure_filter_type')}; "
                f"count {_text(values, 'measure_filter_count')}"
            ),
            (
                "measure_filter_enable",
                "measure_filter_type",
                "measure_filter_count",
            ),
        ),
        SummaryRow(
            "remote_sense",
            "Remote sense",
            sense,
            ("source_output", "measure_sense"),
        ),
        SummaryRow("compliance", "Conditional compliance", compliance, compliance_sources),
        SummaryRow(
            "routing_autozero",
            "Terminals / autozero",
            (
                f"terminals {_text(values, 'terminals')}; "
                f"autozero {_on_off(_text(values, 'measure_autozero'))}"
            ),
            ("terminals", "measure_autozero"),
        ),
        SummaryRow(
            "status_conditions",
            "Condition registers",
            (
                f"STB {_text(values, 'status_condition')}; "
                f"OPER {_text(values, 'operation_condition')}; "
                f"QUES {_text(values, 'questionable_condition')}"
            ),
            (
                "status_condition",
                "operation_condition",
                "questionable_condition",
            ),
        ),
    )


PROFILES = MappingProxyType(
    {
        "2182a": DiagnosticProfile(
            key="2182a",
            model="2182A",
            command_set=CommandSet.SCPI,
            diagnostic_queries=Q2182A,
            live_query=_q("live_voltage", "FETCh?", "live", "Latest voltage"),
            summary_builder=_summary_2182a,
            snapshot_end_names=(
                "identity",
                "sense_function",
                "active_channel",
                "nplc",
                "system_autozero",
                "front_autozero",
                "line_sync",
                "operation_condition",
                "measurement_condition",
                "questionable_condition",
            ),
            consistency_names=(
                "identity",
                "sense_function",
                "active_channel",
                "nplc",
                "system_autozero",
                "front_autozero",
                "line_sync",
            ),
        ),
        "6221": DiagnosticProfile(
            key="6221",
            model="6221",
            command_set=CommandSet.SCPI,
            diagnostic_queries=Q6221,
            live_query=None,
            summary_builder=_summary_6221,
            snapshot_end_names=(
                "identity",
                "output_enabled",
                "operation_condition",
                "current_range_a",
                "current_range_auto",
                "voltage_compliance_v",
                "analog_filter",
            ),
            consistency_names=(
                "identity",
                "output_enabled",
                "current_range_a",
                "current_range_auto",
                "voltage_compliance_v",
                "analog_filter",
            ),
        ),
        "2450": DiagnosticProfile(
            key="2450",
            model="2450",
            command_set=CommandSet.TSP,
            diagnostic_queries=Q2450,
            live_query=None,
            summary_builder=_summary_2450,
            snapshot_end_names=(
                "identity",
                "source_output",
                "source_function",
                "source_level",
                "measure_function",
                "terminals",
                "measure_nplc",
                "status_condition",
                "operation_condition",
                "questionable_condition",
            ),
            consistency_names=(
                "identity",
                "source_output",
                "source_function",
                "source_level",
                "measure_function",
                "terminals",
                "measure_nplc",
            ),
        ),
    }
)


# Historical/known asset metadata is used for simulate fixtures and explicit
# Live approval only.  It is not the production inventory and does not limit
# how many real instruments may be discovered.
KNOWN_ASSETS = MappingProxyType(
    {
        "2182a-gpib6": InstrumentTarget(
            key="2182a-gpib6",
            label="Keithley 2182A · GPIB6 · S/N 1340129",
            resource="GPIB0::6::INSTR",
            vendor="KEITHLEY INSTRUMENTS INC.",
            model="2182A",
            serial="1340129",
            firmware="C02 /A02",
            profile_key="2182a",
            live_supported=True,
        ),
        "2182a-gpib7": InstrumentTarget(
            key="2182a-gpib7",
            label="Keithley 2182A · GPIB7 · S/N 4510267",
            resource="GPIB0::7::INSTR",
            vendor="KEITHLEY INSTRUMENTS INC.",
            model="2182A",
            serial="4510267",
            firmware="C08/B01",
            profile_key="2182a",
            live_supported=False,
        ),
        "6221-gpib9": InstrumentTarget(
            key="6221-gpib9",
            label="Keithley 6221 · GPIB9 · S/N 4533811",
            resource="GPIB0::9::INSTR",
            vendor="KEITHLEY INSTRUMENTS INC.",
            model="6221",
            serial="4533811",
            firmware="D04 /700x",
            profile_key="6221",
            live_supported=False,
        ),
        "6221-gpib10": InstrumentTarget(
            key="6221-gpib10",
            label="Keithley 6221 · GPIB10 · S/N 4581062",
            resource="GPIB0::10::INSTR",
            vendor="KEITHLEY INSTRUMENTS INC.",
            model="6221",
            serial="4581062",
            firmware="D04 /700x",
            profile_key="6221",
            live_supported=False,
        ),
        "2450-gpib25": InstrumentTarget(
            key="2450-gpib25",
            label="Keithley 2450 · GPIB25 · S/N 04584128",
            resource="GPIB0::25::INSTR",
            vendor="KEITHLEY INSTRUMENTS",
            model="2450",
            serial="04584128",
            firmware="1.7.12b",
            profile_key="2450",
            live_supported=False,
        ),
    }
)

# Backward-compatible alias for v1.2 pre-inventory tests and callers.  New GUI
# code must source real choices from an InventorySnapshot, not this mapping.
TARGETS = KNOWN_ASSETS

TARGET_ORDER = (
    "2182a-gpib6",
    "2182a-gpib7",
    "6221-gpib9",
    "6221-gpib10",
    "2450-gpib25",
)


_TSP_SIMPLE_PRINT = re.compile(
    r"print\([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+\)",
    flags=re.ASCII,
)
_TSP_NONCONSUMING_STATUS_PATHS = frozenset(
    {
        "status.condition",
        "status.operation.condition",
        "status.questionable.condition",
    }
)
_SCPI_BLOCKED_EXACT = frozenset({"*ESR?", "READ?", "MEAS?", "SYST:ERR?"})


def target_for(target_key: str) -> InstrumentTarget:
    try:
        return TARGETS[target_key]
    except KeyError as exc:
        raise KeyError(f"unknown fixed instrument target: {target_key!r}") from exc


def profile_key_for_model(
    model: str,
    *,
    command_set_ack: str | None = None,
) -> str | None:
    """Resolve a reusable profile from an exact normalized model field.

    A 2450 identity does not reveal whether its active language is TSP or SCPI,
    so the TSP profile is unavailable until the command set is independently
    confirmed.  No probing command is sent to infer the language.
    """

    normalized = str(model).strip().upper()
    if normalized == "2182A":
        return "2182a"
    if normalized == "6221":
        return "6221"
    if normalized == "2450" and str(command_set_ack or "").strip().upper() == "TSP":
        return "2450"
    return None


def profile_for_key(profile_key: str) -> DiagnosticProfile:
    try:
        return PROFILES[profile_key]
    except KeyError as exc:
        raise KeyError(f"unknown diagnostic profile: {profile_key!r}") from exc


def profile_for_model(
    model: str,
    *,
    command_set_ack: str | None = None,
) -> DiagnosticProfile | None:
    key = profile_key_for_model(model, command_set_ack=command_set_ack)
    return None if key is None else profile_for_key(key)


def profile_for_target(target_key: str) -> DiagnosticProfile:
    target = target_for(target_key)
    if target.profile_key is None:
        raise KeyError(f"target has no approved diagnostic profile: {target_key!r}")
    return profile_for_key(target.profile_key)


def diagnostic_queries_for_profile(profile_key: str) -> tuple[QuerySpec, ...]:
    return profile_for_key(profile_key).diagnostic_queries


def diagnostic_queries_for_target(target_key: str) -> tuple[QuerySpec, ...]:
    return profile_for_target(target_key).diagnostic_queries


def query_is_applicable(spec: QuerySpec, values: Mapping[str, object]) -> bool:
    """Return whether a conditional read belongs to the observed source mode."""

    if spec.condition is None:
        return True
    mode = _source_mode(_text(values, "source_function"))
    return spec.condition == f"source_{mode}" if mode is not None else False


def live_query_for_target(target_key: str) -> QuerySpec | None:
    target = target_for(target_key)
    profile = profile_for_target(target_key)
    if not target.live_supported:
        return None
    return profile.live_query


def allowed_transactions_for_target(
    target_key: str,
    *,
    phase: str = "diagnostic",
) -> tuple[str, ...]:
    profile = profile_for_target(target_key)
    if phase == "diagnostic":
        return profile.diagnostic_commands
    if phase == "live":
        live_query = live_query_for_target(target_key)
        return () if live_query is None else ("*IDN?", live_query.command)
    raise ValueError(f"unsupported read phase: {phase!r}")


def _validate_scpi_syntax(command: str) -> None:
    upper = command.upper()
    compact = upper.replace(" ", "")
    if command.count("?") != 1 or not command.endswith("?"):
        raise UnsafeReadTransaction("SCPI transaction must be one query ending in '?'")
    if compact in _SCPI_BLOCKED_EXACT:
        raise UnsafeReadTransaction(f"state-consuming SCPI query is forbidden: {command!r}")
    if compact.startswith("SYST:ERR"):
        raise UnsafeReadTransaction("SCPI error-queue queries are forbidden")
    if compact.startswith("READ?") or compact.startswith("MEAS?"):
        raise UnsafeReadTransaction("SCPI acquisition queries are forbidden")
    if ":EVEN?" in compact or ":EVENT?" in compact:
        raise UnsafeReadTransaction("SCPI event-register queries are forbidden")


def _validate_tsp_syntax(command: str) -> None:
    if command == "*IDN?":
        return
    if "=" in command:
        raise UnsafeReadTransaction("TSP assignment is forbidden")
    if _TSP_SIMPLE_PRINT.fullmatch(command) is None:
        raise UnsafeReadTransaction(
            "TSP reads must be exact simple print(attribute.path) expressions"
        )
    path = command[len("print(") : -1].lower()
    if path.startswith("smu.measure.read"):
        raise UnsafeReadTransaction("TSP measure.read transactions are forbidden")
    if (
        (path.startswith("status.") and path not in _TSP_NONCONSUMING_STATUS_PATHS)
        or path.startswith("event.")
        or ".event" in path
    ):
        raise UnsafeReadTransaction("TSP event/status-event reads are forbidden")


def _validate_protocol_syntax(profile: DiagnosticProfile, command: str) -> None:
    if not isinstance(command, str) or not command:
        raise UnsafeReadTransaction("read transaction must be a non-empty string")
    if command != command.strip():
        raise UnsafeReadTransaction("leading/trailing whitespace is forbidden")
    if any(character in command for character in (";", "\n", "\r")):
        raise UnsafeReadTransaction("compound or multi-line transactions are forbidden")
    if profile.command_set == CommandSet.SCPI:
        _validate_scpi_syntax(command)
    elif profile.command_set == CommandSet.TSP:
        _validate_tsp_syntax(command)
    else:  # Defensive if another enum/value is introduced incorrectly.
        raise UnsafeReadTransaction(f"unsupported command set: {profile.command_set!r}")


def validate_read_transaction(
    target_key: str,
    command: str,
    *,
    phase: str = "diagnostic",
) -> str:
    """Validate one complete request against one target and one phase.

    No prefix matching or model fallback is performed.  The return value is the
    unchanged request, convenient for a transport that exposes only query().
    """

    profile = profile_for_target(target_key)
    allowed = allowed_transactions_for_target(target_key, phase=phase)
    if command not in allowed:
        raise UnsafeReadTransaction(
            f"request is not in the exact {phase} allowlist for {target_key!r}: {command!r}"
        )
    _validate_protocol_syntax(profile, command)
    return command


def validate_profile_read_transaction(
    profile_key: str,
    command: str,
    *,
    phase: str = "diagnostic",
    live_approved: bool = False,
) -> str:
    """Validate a complete request against a reusable model profile.

    Live is a separate asset approval.  Merely resolving model 2182A never
    grants ``FETCh?`` to a newly discovered instrument.
    """

    profile = profile_for_key(profile_key)
    if phase == "diagnostic":
        allowed = profile.diagnostic_commands
    elif phase == "live" and live_approved and profile.live_query is not None:
        allowed = ("*IDN?", profile.live_query.command)
    elif phase == "live":
        allowed = ()
    else:
        raise ValueError(f"unsupported read phase: {phase!r}")
    if command not in allowed:
        raise UnsafeReadTransaction(
            f"request is not in the exact {phase} allowlist for profile "
            f"{profile_key!r}: {command!r}"
        )
    _validate_protocol_syntax(profile, command)
    return command


def summary_rows_for_target(
    target_key: str,
    values: Mapping[str, object],
) -> tuple[SummaryRow, ...]:
    target = target_for(target_key)
    profile = profile_for_target(target_key)
    return profile.summary_rows(target, values)


def summary_rows_for_profile(
    profile_key: str,
    target: InstrumentTarget,
    values: Mapping[str, object],
) -> tuple[SummaryRow, ...]:
    profile = profile_for_key(profile_key)
    if target.profile_key != profile_key:
        raise ValueError(
            f"target {target.key!r} does not use profile {profile_key!r}"
        )
    return profile.summary_rows(target, values)


def _validate_registry() -> None:
    if tuple(TARGETS) != TARGET_ORDER:
        raise RuntimeError("fixed target registry order differs from TARGET_ORDER")
    resources = [target.resource for target in TARGETS.values()]
    if len(resources) != len(set(resources)):
        raise RuntimeError("fixed target resources must be unique")
    for target in KNOWN_ASSETS.values():
        if target.profile_key not in PROFILES:
            raise RuntimeError(f"target has unknown profile: {target.key!r}")
        profile = PROFILES[target.profile_key]
        if target.model != profile.model:
            raise RuntimeError(f"target/profile model mismatch: {target.key!r}")
        if target.live_supported and profile.live_query is None:
            raise RuntimeError(f"live target has no approved live query: {target.key!r}")
    for profile in PROFILES.values():
        names = [spec.name for spec in profile.diagnostic_queries]
        commands = [spec.command for spec in profile.diagnostic_queries]
        if len(names) != len(set(names)):
            raise RuntimeError(f"duplicate query names in profile {profile.key!r}")
        if len(commands) != len(set(commands)):
            raise RuntimeError(f"duplicate query commands in profile {profile.key!r}")
        declared_names = set(names)
        if not set(profile.snapshot_end_names).issubset(declared_names):
            raise RuntimeError(
                f"snapshot end names are not declared by profile {profile.key!r}"
            )
        if not set(profile.consistency_names).issubset(profile.snapshot_end_names):
            raise RuntimeError(
                f"consistency names are not end reads for profile {profile.key!r}"
            )
        for spec in profile.diagnostic_queries:
            if spec.condition not in {None, "source_voltage", "source_current"}:
                raise RuntimeError(
                    f"unsupported query condition in profile {profile.key!r}: "
                    f"{spec.condition!r}"
                )
            _validate_protocol_syntax(profile, spec.command)
        if profile.live_query is not None:
            _validate_protocol_syntax(profile, profile.live_query.command)


_validate_registry()


__all__ = (
    "CommandSet",
    "DiagnosticProfile",
    "InstrumentTarget",
    "KNOWN_ASSETS",
    "PROFILES",
    "Q2182A",
    "Q2450",
    "Q6221",
    "QuerySpec",
    "SummaryRow",
    "TARGETS",
    "TARGET_ORDER",
    "UnsafeReadTransaction",
    "allowed_transactions_for_target",
    "diagnostic_queries_for_target",
    "diagnostic_queries_for_profile",
    "live_query_for_target",
    "profile_for_target",
    "profile_for_key",
    "profile_for_model",
    "profile_key_for_model",
    "query_is_applicable",
    "summary_rows_for_target",
    "summary_rows_for_profile",
    "target_for",
    "validate_read_transaction",
    "validate_profile_read_transaction",
)
