# Standard Terminal Inputs — Environment and Standard-Resistor Work

This sheet is for the standalone special GPIB terminal. Enter one line at a time. Do not paste an entire block into the prompt.

The terminal grammar is:

```text
QUERY <VISA-resource> <SCPI-query-or-TSP-print>
WRITE <VISA-resource> <SCPI-or-TSP-state-change>
```

The host opens the VISA resource first. Only the final SCPI/TSP message is transmitted to that addressed instrument. For example:

```text
QUERY GPIB0::9::INSTR *IDN?
```

means approximately:

```python
instrument = resource_manager.open_resource("GPIB0::9::INSTR")
response = instrument.query("*IDN?")
```

The 2182A commands below follow the official [Keithley 2182A User's Manual](https://download.tek.com/manual/2182A-900-01C_July_2022_User.pdf). The 6221 and Delta commands follow the official [Keithley 6220/6221 User's Manual](https://download.tek.com/manual/622x-900-01%20%28C%20-%20Oct%202008%29%28User%29.pdf).

## 1. Local terminal commands

These do not send an instrument message:

```text
HELP
MAP
STATUS
LIST
TIMEOUT 3000
CALC-R 1.25E-3 2.5E-6
LOCK-WRITES
EXIT
```

`CALC-R` calculates `R = V / I` locally. Values must be in volts and amperes. The example returns `500 ohm`; it does not contact an instrument.

## 2. Confirmed laboratory address map

| VISA resource | Model | Serial number | Intended role |
|---|---:|---:|---|
| `GPIB0::6::INSTR` | 2182A | 1340129 | Nanovoltmeter candidate A |
| `GPIB0::7::INSTR` | 2182A | 4510267 | Nanovoltmeter candidate B |
| `GPIB0::9::INSTR` | 6221 | 4533811 | Current-source candidate A |
| `GPIB0::10::INSTR` | 6221 | 4581062 | Current-source candidate B |
| `GPIB0::25::INSTR` | 2450 | 04584128 | SMU, TSP mode |
| `GPIB0::26::INSTR` | 2450 | 04464720 | SMU, TSP mode |

## 3. First demonstration: identity only

These six inputs are the recommended first live demonstration. They do not change settings:

```text
QUERY GPIB0::6::INSTR *IDN?
QUERY GPIB0::7::INSTR *IDN?
QUERY GPIB0::9::INSTR *IDN?
QUERY GPIB0::10::INSTR *IDN?
QUERY GPIB0::25::INSTR *IDN?
QUERY GPIB0::26::INSTR *IDN?
```

Stop immediately if the returned model or serial number does not match the table.

## 4. Query-only environment record

### 4.1 Model 2182A — use resource 6 or 7

Replace `<2182A_RESOURCE>` with exactly `GPIB0::6::INSTR` or `GPIB0::7::INSTR`. The bracketed template is not itself a valid command.

```text
QUERY <2182A_RESOURCE> *IDN?
QUERY <2182A_RESOURCE> SYST:VERS?
QUERY <2182A_RESOURCE> SYST:LFREQUENCY?
QUERY <2182A_RESOURCE> SYST:POSETUP?
QUERY <2182A_RESOURCE> SENS:FUNC?
QUERY <2182A_RESOURCE> SENS:CHAN?
QUERY <2182A_RESOURCE> SENS:VOLT:DC:NPLC?
QUERY <2182A_RESOURCE> SENS:VOLT:DC:CHAN1:RANG?
QUERY <2182A_RESOURCE> SENS:VOLT:DC:CHAN1:RANG:AUTO?
QUERY <2182A_RESOURCE> SENS:VOLT:DC:CHAN2:RANG?
QUERY <2182A_RESOURCE> SENS:VOLT:DC:CHAN2:RANG:AUTO?
QUERY <2182A_RESOURCE> SENS:VOLT:DC:CHAN1:DFILTER?
QUERY <2182A_RESOURCE> SENS:VOLT:DC:CHAN1:LPASS?
QUERY <2182A_RESOURCE> SENS:VOLT:DC:CHAN2:DFILTER?
QUERY <2182A_RESOURCE> SENS:VOLT:DC:CHAN2:LPASS?
QUERY <2182A_RESOURCE> TRIG:COUNT?
QUERY <2182A_RESOURCE> TRIG:DELAY?
QUERY <2182A_RESOURCE> TRIG:SOURCE?
```

These are configuration queries. `SENS:DATA:LATEST?` is different: it returns the most recent reading and can be stale after a configuration change.

```text
QUERY <2182A_RESOURCE> SENS:DATA:LATEST?
```

`SENS:DATA:FRESH?` initiates a fresh reading. The terminal therefore asks for `SEND ACTIVE_QUERY` before transmitting it:

```text
QUERY <2182A_RESOURCE> SENS:DATA:FRESH?
```

### 4.2 Model 6221 — use resource 9 or 10

Replace `<6221_RESOURCE>` with exactly `GPIB0::9::INSTR` or `GPIB0::10::INSTR`.

```text
QUERY <6221_RESOURCE> *IDN?
QUERY <6221_RESOURCE> SYST:VERS?
QUERY <6221_RESOURCE> SYST:POSETUP?
QUERY <6221_RESOURCE> OUTP?
QUERY <6221_RESOURCE> OUTP:LTEARTH?
QUERY <6221_RESOURCE> OUTP:ISHIELD?
QUERY <6221_RESOURCE> OUTP:RESPONSE?
QUERY <6221_RESOURCE> OUTP:INTERLOCK:TRIPPED?
QUERY <6221_RESOURCE> SOUR:CURR?
QUERY <6221_RESOURCE> SOUR:CURR:RANG:AUTO?
QUERY <6221_RESOURCE> SOUR:CURR:RANG?
QUERY <6221_RESOURCE> SOUR:CURR:COMP?
QUERY <6221_RESOURCE> SOUR:CURR:FILT?
QUERY <6221_RESOURCE> SOUR:DELT:NVPRESENT?
QUERY <6221_RESOURCE> SOUR:DELT:HIGH?
QUERY <6221_RESOURCE> SOUR:DELT:LOW?
QUERY <6221_RESOURCE> SOUR:DELT:DELAY?
QUERY <6221_RESOURCE> SOUR:DELT:COUNT?
QUERY <6221_RESOURCE> SOUR:DELT:CSWITCH?
QUERY <6221_RESOURCE> SOUR:DELT:ARM?
```

Important interpretation:

- `OUTP?` returning `0` means source output is off.
- `OUTP:INTERLOCK:TRIPPED?` returning `1` on the 6221 means the interlock is closed/ready; `0` means open/tripped.
- `SOUR:DELT:NVPRESENT?` returning `1` means the 6221 has detected the required 2182/2182A serial link; `0` means the Delta pairing is not ready.

### 4.3 Model 2450 in TSP mode — optional environment confirmation

```text
QUERY GPIB0::25::INSTR *IDN?
QUERY GPIB0::25::INSTR print(localnode.model)
QUERY GPIB0::25::INSTR print(localnode.serialno)
QUERY GPIB0::25::INSTR print(localnode.version)
QUERY GPIB0::25::INSTR print(localnode.linefreq)
QUERY GPIB0::25::INSTR print(smu.source.output)
QUERY GPIB0::25::INSTR print(smu.source.func)
QUERY GPIB0::25::INSTR print(smu.source.level)
QUERY GPIB0::25::INSTR print(smu.source.range)
QUERY GPIB0::25::INSTR print(smu.measure.func)
QUERY GPIB0::25::INSTR print(smu.measure.range)
QUERY GPIB0::25::INSTR print(smu.measure.nplc)
QUERY GPIB0::25::INSTR print(smu.measure.sense)
QUERY GPIB0::25::INSTR print(smu.measure.terminals)
QUERY GPIB0::25::INSTR print(smu.interlock.tripped)
```

Use the same messages with resource 26 for the second 2450. On the 2450, the earlier live baseline showed `smu.OFF` for normal/not-tripped and `smu.ON` for asserted/tripped.

## 5. Standard-resistor test: query-only/manual-setup mode

Use this mode while the resistor value, power rating, safe current, and compliance are not yet approved. The program does not configure or energize either instrument.

### 5.1 Record before connecting or energizing

```text
Resistor identification: ____________________
Nominal resistance: ____________________ ohm
Tolerance: ____________________ %
Maximum power: ____________________ W
Maximum voltage: ____________________ V
Maximum current: ____________________ A
Approved test current: ____________________ A
Approved compliance: ____________________ V
2182A resource: ____________________
6221 resource: ____________________
Wiring: 2-wire / 4-wire
6221 output observed OFF before wiring: yes / no
```

If any safety-critical field is unknown, remain query-only and do not unlock writes.

### 5.2 After manual front-panel setup

1. Query the 6221 and confirm the actual current, compliance, and output state:

   ```text
   QUERY <6221_RESOURCE> OUTP?
   QUERY <6221_RESOURCE> SOUR:CURR?
   QUERY <6221_RESOURCE> SOUR:CURR:RANG?
   QUERY <6221_RESOURCE> SOUR:CURR:COMP?
   QUERY <6221_RESOURCE> OUTP:INTERLOCK:TRIPPED?
   ```

2. Query the 2182A measurement configuration:

   ```text
   QUERY <2182A_RESOURCE> SENS:FUNC?
   QUERY <2182A_RESOURCE> SENS:CHAN?
   QUERY <2182A_RESOURCE> SENS:VOLT:DC:NPLC?
   QUERY <2182A_RESOURCE> SENS:VOLT:DC:CHAN1:RANG?
   QUERY <2182A_RESOURCE> SENS:VOLT:DC:CHAN1:RANG:AUTO?
   ```

3. To obtain a fresh voltage reading, enter the query and then the requested confirmation:

   ```text
   QUERY <2182A_RESOURCE> SENS:DATA:FRESH?
   SEND ACTIVE_QUERY
   ```

4. Use the actual returned voltage and the actual queried current, including signs:

   ```text
   CALC-R <MEASURED_VOLTAGE_V> <ACTUAL_CURRENT_A>
   ```

For precision work, one reading is not a complete resistance measurement. Reverse current and average repeated readings to reduce thermal EMF, or use the verified Delta configuration when the required serial/trigger connections are present.

## 6. Reviewed write mode — not for an unknown resistor

The following entries are templates, not approved values. Do not transmit them until a responsible person has approved the wiring, resistance/rating, current, range, and compliance. Replace every `<...>` field before entering the line.

Unlock sequence:

```text
UNLOCK-WRITES I_UNDERSTAND_WRITES_CAN_CHANGE_INSTRUMENTS
```

Each ordinary write then asks for `SEND`; each high-risk write asks for `SEND HIGH_RISK`.

### 6.1 Put the selected 6221 output off first

```text
WRITE <6221_RESOURCE> OUTP OFF
SEND
QUERY <6221_RESOURCE> OUTP?
```

### 6.2 Configure the 2182A voltage measurement

```text
WRITE <2182A_RESOURCE> SENS:FUNC "VOLT:DC"
WRITE <2182A_RESOURCE> SENS:CHAN 1
WRITE <2182A_RESOURCE> SENS:VOLT:DC:NPLC <APPROVED_NPLC_0.01_TO_50>
WRITE <2182A_RESOURCE> SENS:VOLT:DC:CHAN1:RANG:AUTO ON
```

Read back every value after writing:

```text
QUERY <2182A_RESOURCE> SENS:FUNC?
QUERY <2182A_RESOURCE> SENS:CHAN?
QUERY <2182A_RESOURCE> SENS:VOLT:DC:NPLC?
QUERY <2182A_RESOURCE> SENS:VOLT:DC:CHAN1:RANG:AUTO?
```

### 6.3 Configure the selected 6221 while output remains off

```text
WRITE <6221_RESOURCE> SOUR:CURR:RANG:AUTO ON
WRITE <6221_RESOURCE> SOUR:CURR:COMP <APPROVED_COMPLIANCE_V>
WRITE <6221_RESOURCE> SOUR:CURR <APPROVED_CURRENT_A>
```

These are high-risk writes and require `SEND HIGH_RISK`. Read back before enabling output:

```text
QUERY <6221_RESOURCE> OUTP?
QUERY <6221_RESOURCE> SOUR:CURR?
QUERY <6221_RESOURCE> SOUR:CURR:RANG:AUTO?
QUERY <6221_RESOURCE> SOUR:CURR:RANG?
QUERY <6221_RESOURCE> SOUR:CURR:COMP?
QUERY <6221_RESOURCE> OUTP:INTERLOCK:TRIPPED?
```

### 6.4 Output on and emergency/off command

Only after the physical circuit and all readback values are approved:

```text
WRITE <6221_RESOURCE> OUTP ON
SEND HIGH_RISK
```

To turn it off through the terminal:

```text
WRITE <6221_RESOURCE> OUTP OFF
SEND
QUERY <6221_RESOURCE> OUTP?
LOCK-WRITES
```

The physical OUTPUT OFF control and power/interlock procedures remain the primary safety response; software is not an emergency stop.

## 7. 6221 + 2182A True Delta mode — advanced only

Do not arm Delta merely to demonstrate the terminal. True Delta requires the 6221-to-2182A RS-232 link and Trigger Link in addition to the GPIB connection. The 6221 reports the calculated reading because it controls the 2182A over the serial link.

Check readiness first:

```text
QUERY <6221_RESOURCE> OUTP?
QUERY <6221_RESOURCE> SOUR:DELT:NVPRESENT?
QUERY <6221_RESOURCE> SOUR:DELT:ARM?
```

Continue only if output is off, the approved pair is physically connected, and `NVPRESENT?` returns `1`. Reviewed templates:

```text
WRITE <6221_RESOURCE> SOUR:DELT:HIGH <APPROVED_POSITIVE_CURRENT_A>
WRITE <6221_RESOURCE> SOUR:DELT:LOW <APPROVED_NEGATIVE_CURRENT_A>
WRITE <6221_RESOURCE> SOUR:DELT:DELAY <APPROVED_DELAY_S>
WRITE <6221_RESOURCE> SOUR:DELT:COUNT <APPROVED_FINITE_COUNT>
WRITE <6221_RESOURCE> SOUR:DELT:CSWITCH <ON_OR_OFF>
WRITE <6221_RESOURCE> SOUR:DELT:ARM
WRITE <6221_RESOURCE> INIT:IMM
```

All of these are high-risk writes. Read the latest result from the 6221 only after the run produces data:

```text
QUERY <6221_RESOURCE> SENS:DATA:LATEST?
```

Un-arm/abort and force output off:

```text
WRITE <6221_RESOURCE> SOUR:SWE:ABOR
WRITE <6221_RESOURCE> OUTP OFF
QUERY <6221_RESOURCE> OUTP?
LOCK-WRITES
```

## 8. Diagnostic/status queries that consume information

These queries can consume or clear queue/status information and therefore require active-query confirmation. Use them only during fault diagnosis and save the log:

```text
QUERY <RESOURCE> SYST:ERR?
QUERY <RESOURCE> *ESR?
```

Do not use `*RST`, `*CLS`, `READ?`, `MEAS?`, `INIT`, or trigger commands as exploratory inputs. They can reset state, clear evidence, initiate acquisition, or change the test sequence.

## 9. End-of-session checklist

1. Confirm both 6221 outputs with `OUTP?`; expected result is `0` unless an approved experiment is intentionally still running.
2. Enter `LOCK-WRITES`.
3. Enter `EXIT`.
4. Preserve the new file in `terminal_logs` with the wiring sheet and GUI baseline ZIP.
5. Record any timeout or model/serial mismatch; do not repeat commands blindly.
