"""Exact query allowlists.

Only commands listed here can reach a real instrument.  The catalog deliberately
excludes queries that trigger acquisition or consume event/error state.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class QuerySpec:
    name: str
    command: str
    group: str
    scope: str = "core"
    note: str = ""


@dataclass(frozen=True)
class Profile:
    key: str
    model: str
    command_set: str
    queries: tuple[QuerySpec, ...]
    simulated: dict[str, str]


def q(name: str, command: str, group: str, scope: str = "core", note: str = "") -> QuerySpec:
    return QuerySpec(name, command, group, scope, note)


Q6221 = (
    q("identity", "*IDN?", "identity"),
    q("scpi_version", "SYST:VERS?", "system"),
    q("analog_board_serial", "SYST:ABOARD:SNUMBER?", "system", "full"),
    q("analog_board_revision", "SYST:ABOARD:REVISION?", "system", "full"),
    q("digital_board_serial", "SYST:DBOARD:SNUMBER?", "system", "full"),
    q("digital_board_revision", "SYST:DBOARD:REVISION?", "system", "full"),
    q("power_on_setup", "SYST:POSETUP?", "system"),
    q("lan_address", "SYST:COMM:ETH:ADDR?", "communication", "full"),
    q("lan_mask", "SYST:COMM:ETH:MASK?", "communication", "full"),
    q("lan_gateway", "SYST:COMM:ETH:GATEWAY?", "communication", "full"),
    q("lan_dhcp", "SYST:COMM:ETH:DHCP?", "communication", "full"),
    q("output_enabled", "OUTP?", "output"),
    q("output_low_to_earth", "OUTP:LTEARTH?", "output"),
    q("triax_inner_shield", "OUTP:ISHIELD?", "output"),
    q("output_response", "OUTP:RESPONSE?", "output"),
    q("interlock_closed", "OUTP:INTERLOCK:TRIPPED?", "output"),
    q("current_level_a", "SOUR:CURR?", "source"),
    q("current_range_auto", "SOUR:CURR:RANG:AUTO?", "source"),
    q("current_range_a", "SOUR:CURR:RANG?", "source"),
    q("voltage_compliance_v", "SOUR:CURR:COMP?", "source"),
    q("analog_filter", "SOUR:CURR:FILT?", "source"),
    q("sweep_start_a", "SOUR:CURR:START?", "sweep", "full"),
    q("sweep_stop_a", "SOUR:CURR:STOP?", "sweep", "full"),
    q("sweep_step_a", "SOUR:CURR:STEP?", "sweep", "full"),
    q("sweep_center_a", "SOUR:CURR:CENTER?", "sweep", "full"),
    q("sweep_span_a", "SOUR:CURR:SPAN?", "sweep", "full"),
    q("source_delay_s", "SOUR:CURR:DELAY?", "sweep", "full"),
    q("sweep_spacing", "SOUR:CURR:SWE:SPACING?", "sweep", "full"),
    q("sweep_points", "SOUR:CURR:SWE:POINTS?", "sweep", "full"),
    q("sweep_ranging", "SOUR:CURR:SWE:RANGING?", "sweep", "full"),
    q("sweep_count", "SOUR:CURR:SWE:COUNT?", "sweep", "full"),
    q("sweep_abort_on_compliance", "SOUR:CURR:SWE:CABORT?", "sweep", "full"),
    q("delta_nv_present", "SOUR:DELTA:NVPRESENT?", "delta", "full"),
    q("delta_high_a", "SOUR:DELTA:HIGH?", "delta", "full"),
    q("delta_low_a", "SOUR:DELTA:LOW?", "delta", "full"),
    q("delta_delay_s", "SOUR:DELTA:DELAY?", "delta", "full"),
    q("delta_count", "SOUR:DELTA:COUNT?", "delta", "full"),
    q("delta_cold_switch", "SOUR:DELTA:CSWITCH?", "delta", "full"),
    q("delta_armed", "SOUR:DELTA:ARM?", "delta", "full"),
    q("pulse_delta_high_a", "SOUR:PDELTA:HIGH?", "pulse_delta", "full"),
    q("pulse_delta_low_a", "SOUR:PDELTA:LOW?", "pulse_delta", "full"),
    q("pulse_delta_width_s", "SOUR:PDELTA:WIDTH?", "pulse_delta", "full"),
    q("pulse_delta_source_delay_s", "SOUR:PDELTA:SDELAY?", "pulse_delta", "full"),
    q("pulse_delta_count", "SOUR:PDELTA:COUNT?", "pulse_delta", "full"),
    q("pulse_delta_armed", "SOUR:PDELTA:ARM?", "pulse_delta", "full"),
    q("wave_function", "SOUR:WAVE:FUNC?", "wave", "full"),
    q("wave_duty_cycle_pct", "SOUR:WAVE:DCYCLE?", "wave", "full"),
    q("wave_amplitude_a", "SOUR:WAVE:AMPL?", "wave", "full"),
    q("wave_frequency_hz", "SOUR:WAVE:FREQ?", "wave", "full"),
    q("wave_offset_a", "SOUR:WAVE:OFFSET?", "wave", "full"),
    q("wave_duration_s", "SOUR:WAVE:DURATION:TIME?", "wave", "full"),
    q("wave_duration_cycles", "SOUR:WAVE:DURATION:CYCLES?", "wave", "full"),
    q("format_data", "FORM:DATA?", "format", "full"),
    q("format_elements", "FORM:ELEM?", "format", "full"),
    q("trace_capacity", "TRACE:POINTS?", "buffer", "full"),
    q("trace_actual", "TRACE:ACTUAL?", "buffer", "full"),
    q("trace_type", "TRACE:TYPE?", "buffer", "full"),
    q("trigger_source", "TRIG:SOURCE?", "trigger", "full"),
)


Q2182A = (
    q("identity", "*IDN?", "identity"),
    q("scpi_version", "SYST:VERS?", "system"),
    q("line_frequency_hz", "SYST:LFREQUENCY?", "system"),
    q("power_on_setup", "SYST:POSETUP?", "system"),
    q("gpib_address", "SYST:COMM:GPIB:ADDR?", "communication", "full"),
    q("auto_zero", "SYST:AZERO?", "system", "full"),
    q("front_auto_zero", "SYST:FAZERO?", "system", "full"),
    q("line_sync", "SYST:LSYNC?", "system", "full"),
    q("beeper", "SYST:BEEPER?", "system", "full"),
    q("key_click", "SYST:KCLICK?", "system", "full"),
    q("sense_function", "SENS:FUNC?", "sense"),
    q("active_channel", "SENS:CHAN?", "sense"),
    q("nplc", "SENS:VOLT:DC:NPLC?", "sense"),
    q("digits", "SENS:VOLT:DC:DIGITS?", "sense", "full"),
    q("ch1_range_v", "SENS:VOLT:DC:CHAN1:RANG?", "sense"),
    q("ch1_autorange", "SENS:VOLT:DC:CHAN1:RANG:AUTO?", "sense"),
    q("ch2_range_v", "SENS:VOLT:DC:CHAN2:RANG?", "sense"),
    q("ch2_autorange", "SENS:VOLT:DC:CHAN2:RANG:AUTO?", "sense"),
    q("ch1_digital_filter", "SENS:VOLT:DC:CHAN1:DFILTER?", "filter"),
    q("ch1_filter_count", "SENS:VOLT:DC:CHAN1:DFILTER:COUNT?", "filter", "full"),
    q("ch1_filter_type", "SENS:VOLT:DC:CHAN1:DFILTER:TCONTROL?", "filter", "full"),
    q("ch1_filter_window_pct", "SENS:VOLT:DC:CHAN1:DFILTER:WINDOW?", "filter", "full"),
    q("ch1_analog_filter", "SENS:VOLT:DC:CHAN1:LPASS?", "filter"),
    q("ch2_digital_filter", "SENS:VOLT:DC:CHAN2:DFILTER?", "filter"),
    q("ch2_filter_count", "SENS:VOLT:DC:CHAN2:DFILTER:COUNT?", "filter", "full"),
    q("ch2_filter_type", "SENS:VOLT:DC:CHAN2:DFILTER:TCONTROL?", "filter", "full"),
    q("ch2_filter_window_pct", "SENS:VOLT:DC:CHAN2:DFILTER:WINDOW?", "filter", "full"),
    q("ch2_analog_filter", "SENS:VOLT:DC:CHAN2:LPASS?", "filter"),
    q("ch1_relative", "SENS:VOLT:DC:CHAN1:REFERENCE?", "relative", "full"),
    q("ch1_relative_enabled", "SENS:VOLT:DC:CHAN1:REFERENCE:STATE?", "relative", "full"),
    q("ch2_relative", "SENS:VOLT:DC:CHAN2:REFERENCE?", "relative", "full"),
    q("ch2_relative_enabled", "SENS:VOLT:DC:CHAN2:REFERENCE:STATE?", "relative", "full"),
    q("latest_cached_reading", "SENS:DATA:LATEST?", "reading", "full", "Returns the cached latest value; does not initiate a measurement."),
    q("trigger_count", "TRIG:COUNT?", "trigger"),
    q("trigger_delay_s", "TRIG:DELAY?", "trigger"),
    q("trigger_delay_auto", "TRIG:DELAY:AUTO?", "trigger", "full"),
    q("trigger_source", "TRIG:SOURCE?", "trigger"),
    q("trigger_timer_s", "TRIG:TIMER?", "trigger", "full"),
    q("display_enabled", "DISP:ENABLE?", "display", "full"),
    q("format_data", "FORM:DATA?", "format", "full"),
    q("format_elements", "FORM:ELEM?", "format", "full"),
    q("analog_output_enabled", "OUTP?", "analog_output", "full"),
    q("analog_output_gain", "OUTP:GAIN?", "analog_output", "full"),
    q("analog_output_offset", "OUTP:OFFSET?", "analog_output", "full"),
)


Q2450_SCPI = (
    q("identity", "*IDN?", "identity"),
    q("scpi_version", "SYST:VERS?", "system"),
    q("line_frequency_hz", "SYST:LFREQUENCY?", "system"),
    q("instrument_time", "SYST:TIME? 1", "system", "full"),
    q("power_on_setup", "SYST:POSETUP?", "system", "full"),
    q("gpib_address", "SYST:GPIB:ADDR?", "communication", "full"),
    q("lan_mac", "SYST:COMM:LAN:MACADDRESS?", "communication", "full"),
    q("lan_configuration", "SYST:COMM:LAN:CONFIGURE?", "communication", "full"),
    q("source_function", "SOUR:FUNC?", "source"),
    q("output_enabled", "OUTP?", "output"),
    q("interlock_asserted", "OUTP:INTERLOCK:TRIPPED?", "output"),
    q("terminals", "ROUT:TERMINALS?", "routing"),
    q("measure_function", "SENS:FUNC?", "sense"),
    q("source_current_a", "SOUR:CURR?", "source"),
    q("source_current_range_a", "SOUR:CURR:RANG?", "source"),
    q("source_current_autorange", "SOUR:CURR:RANG:AUTO?", "source"),
    q("source_current_voltage_limit_v", "SOUR:CURR:VLIMIT?", "limit"),
    q("source_current_limit_tripped", "SOUR:CURR:VLIMIT:TRIPPED?", "limit"),
    q("source_voltage_v", "SOUR:VOLT?", "source"),
    q("source_voltage_range_v", "SOUR:VOLT:RANG?", "source"),
    q("source_voltage_autorange", "SOUR:VOLT:RANG:AUTO?", "source"),
    q("source_voltage_current_limit_a", "SOUR:VOLT:ILIMIT?", "limit"),
    q("source_voltage_limit_tripped", "SOUR:VOLT:ILIMIT:TRIPPED?", "limit"),
    q("overvoltage_protection", "SOUR:VOLT:PROTECTION?", "limit", "full"),
    q("overvoltage_protection_tripped", "SOUR:VOLT:PROTECTION:TRIPPED?", "limit", "full"),
    q("source_current_off_mode", "OUTP:CURR:SMODE?", "output", "full"),
    q("source_voltage_off_mode", "OUTP:VOLT:SMODE?", "output", "full"),
    q("source_current_high_capacitance", "SOUR:CURR:HIGH:CAPACITANCE?", "source", "full"),
    q("source_current_readback", "SOUR:CURR:READ:BACK?", "source", "full"),
    q("source_voltage_readback", "SOUR:VOLT:READ:BACK?", "source", "full"),
    q("measure_count", "SENS:COUNT?", "sense", "full"),
    q("measure_current_nplc", "SENS:CURR:NPLC?", "sense"),
    q("measure_current_range_a", "SENS:CURR:RANG?", "sense"),
    q("measure_current_autorange", "SENS:CURR:RANG:AUTO?", "sense"),
    q("measure_voltage_nplc", "SENS:VOLT:NPLC?", "sense"),
    q("measure_voltage_range_v", "SENS:VOLT:RANG?", "sense"),
    q("measure_voltage_autorange", "SENS:VOLT:RANG:AUTO?", "sense"),
    q("measure_resistance_nplc", "SENS:RES:NPLC?", "sense", "full"),
    q("measure_resistance_range_ohm", "SENS:RES:RANG?", "sense", "full"),
    q("measure_resistance_autorange", "SENS:RES:RANG:AUTO?", "sense", "full"),
    q("measure_remote_sense_current", "SENS:CURR:RSENSE?", "sense", "full"),
    q("measure_remote_sense_voltage", "SENS:VOLT:RSENSE?", "sense", "full"),
    q("measure_current_filter", "SENS:CURR:AVERAGE?", "filter", "full"),
    q("measure_current_filter_count", "SENS:CURR:AVERAGE:COUNT?", "filter", "full"),
    q("measure_voltage_filter", "SENS:VOLT:AVERAGE?", "filter", "full"),
    q("measure_voltage_filter_count", "SENS:VOLT:AVERAGE:COUNT?", "filter", "full"),
    q("trigger_state", "TRIG:STATE?", "trigger", "full"),
    q("trigger_blocks", "TRIG:BLOCK:LIST?", "trigger", "full"),
    q("buffer1_count", 'TRACE:ACTUAL? "defbuffer1"', "buffer", "full"),
    q("buffer2_count", 'TRACE:ACTUAL? "defbuffer2"', "buffer", "full"),
    q("format_data", "FORM:DATA?", "format", "full"),
)


Q2450_TSP = (
    q("identity", "*IDN?", "identity"),
    q("model", "print(localnode.model)", "identity"),
    q("serial", "print(localnode.serialno)", "identity"),
    q("firmware", "print(localnode.version)", "identity"),
    q("line_frequency_hz", "print(localnode.linefreq)", "system"),
    q("source_output", "print(smu.source.output)", "output"),
    q("source_function", "print(smu.source.func)", "source"),
    q("source_level", "print(smu.source.level)", "source"),
    q("source_range", "print(smu.source.range)", "source"),
    q("source_autorange", "print(smu.source.autorange)", "source"),
    q("measure_function", "print(smu.measure.func)", "sense"),
    q("measure_range", "print(smu.measure.range)", "sense"),
    q("measure_autorange", "print(smu.measure.autorange)", "sense"),
    q("measure_nplc", "print(smu.measure.nplc)", "sense"),
    q("measure_sense", "print(smu.measure.sense)", "sense"),
    q("measure_terminals", "print(smu.measure.terminals)", "routing"),
    q("interlock", "print(smu.interlock.tripped)", "output"),
    q("source_readback", "print(smu.source.readback)", "source", "full"),
    q("source_off_mode", "print(smu.source.offmode)", "output", "full"),
    q("source_high_capacitance", "print(smu.source.highc)", "source", "full"),
    q("overvoltage_protection", "print(smu.source.protect.level)", "limit", "full"),
    q("overvoltage_protection_tripped", "print(smu.source.protect.tripped)", "limit", "full"),
    q("current_limit", "print(smu.source.ilimit.level)", "limit", "full"),
    q("voltage_limit", "print(smu.source.vlimit.level)", "limit", "full"),
    q("buffer1_count", "print(defbuffer1.n)", "buffer", "full"),
    q("buffer1_capacity", "print(defbuffer1.capacity)", "buffer", "full"),
    q("buffer1_fill_mode", "print(defbuffer1.fillmode)", "buffer", "full"),
    q("display_light_state", "print(display.lightstate)", "display", "full"),
    q("lan_ip_configuration", "print(lan.ipconfig())", "communication", "full"),
    q("lan_mac", "print(lan.macaddress)", "communication", "full"),
)


def _sim(identity: str, queries: tuple[QuerySpec, ...], overrides: dict[str, str]) -> dict[str, str]:
    values = {item.command: overrides.get(item.name, "0") for item in queries}
    values["*IDN?"] = identity
    return values


PROFILES = {
    "6221": Profile(
        "6221", "Keithley 6221", "SCPI", Q6221,
        _sim("KEITHLEY INSTRUMENTS INC.,MODEL 6221,SIM6221001,D04", Q6221, {
            "scpi_version": "1999.0", "power_on_setup": "RST", "output_enabled": "0",
            "current_level_a": "1.000000E-06", "current_range_a": "2.000000E-06",
            "current_range_auto": "1", "voltage_compliance_v": "10.0000",
            "analog_filter": "0", "output_response": "FAST", "interlock_closed": "1",
            "triax_inner_shield": "OLOW", "output_low_to_earth": "0", "wave_function": "SIN",
            "wave_frequency_hz": "1.000000E+03", "gpib_address": "8",
        }),
    ),
    "2182a": Profile(
        "2182a", "Keithley 2182A", "SCPI", Q2182A,
        _sim("KEITHLEY INSTRUMENTS INC.,MODEL 2182A,SIM2182001,C11", Q2182A, {
            "scpi_version": "1999.0", "line_frequency_hz": "50", "power_on_setup": "RST",
            "sense_function": '"VOLT:DC"', "active_channel": "1", "nplc": "1.000000",
            "ch1_range_v": "1.000000E-01", "ch2_range_v": "1.000000E+00",
            "ch1_autorange": "1", "ch2_autorange": "1", "latest_cached_reading": "2.500000E-07",
            "trigger_count": "1", "trigger_source": "IMM",
        }),
    ),
    "2450-scpi": Profile(
        "2450-scpi", "Keithley 2450", "SCPI", Q2450_SCPI,
        _sim("KEITHLEY INSTRUMENTS,MODEL 2450,SIM2450001,1.7.18", Q2450_SCPI, {
            "scpi_version": "1996.0", "line_frequency_hz": "50", "source_function": "VOLT",
            "measure_function": '"CURR"', "output_enabled": "0", "interlock_asserted": "0",
            "terminals": "FRON", "source_voltage_v": "0.000000E+00",
            "source_voltage_range_v": "2.000000E+00", "source_voltage_autorange": "1",
            "source_voltage_current_limit_a": "1.000000E-03", "measure_current_nplc": "1.000000",
            "measure_current_range_a": "1.000000E-03", "measure_current_autorange": "1",
            "gpib_address": "25",
        }),
    ),
    "2450-tsp": Profile(
        "2450-tsp", "Keithley 2450", "TSP", Q2450_TSP,
        _sim("KEITHLEY INSTRUMENTS,MODEL 2450,SIM2450001,1.7.18", Q2450_TSP, {
            "model": "2450", "serial": "SIM2450001", "firmware": "1.7.18",
            "line_frequency_hz": "50", "source_output": "0", "source_function": "1",
            "source_level": "0", "source_range": "2", "source_autorange": "1",
            "measure_function": "0", "measure_range": "0.001", "measure_autorange": "1",
            "measure_nplc": "1", "measure_sense": "0", "measure_terminals": "0",
        }),
    ),
}


def queries_for(profile: Profile, scope: str) -> tuple[QuerySpec, ...]:
    if scope == "identity":
        return tuple(item for item in profile.queries if item.name == "identity")
    if scope == "full":
        return profile.queries
    return tuple(item for item in profile.queries if item.scope == "core")


def identity_matches(profile: Profile, response: str) -> bool:
    upper = response.upper()
    expected = {
        "6221": "6221",
        "2182a": "2182A",
        "2450-scpi": "2450",
        "2450-tsp": "2450",
    }
    return expected[profile.key] in upper
