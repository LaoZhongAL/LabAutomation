# Release Notes 0.6.0

## Added

- English `Keithley 6221 / 2182A Manual Pair Observer` GUI.
- Free operator selection of any confirmed laboratory 6221 and any confirmed laboratory 2182A.
- Separate button-triggered configuration and latest-measurement snapshots.
- No automatic VISA refresh or polling.
- Local-only optional resistor metadata and guarded `V/I` estimate.
- A new timestamped `pair-observer.json` evidence file for every click.
- Partial evidence preservation if either VISA session or query sequence fails.
- Standalone `START_PAIR_GUI.bat` launcher and `instrument-pair-observer` entry point.

## Safety properties

- Fixed NI MAX address choices only.
- Exact query allowlists only; no arbitrary command entry.
- `*IDN?` is the first query to each instrument.
- Stop after the first I/O error for that instrument.
- No reset, clear, trigger, acquisition-start, configuration write, output control, event queue, or error queue command.
- `SENS:DATA:LATEST?` returns the 2182A cached last reading and does not trigger a new measurement.
- A simple `V/I` estimate is suppressed while 6221 Delta mode is armed.

## Corrected

- Corrected the 2450 TSP interlock interpretation in the six-instrument GUI: `smu.OFF` means the interlock signal is not asserted; `smu.ON` means it is asserted. The 0.5.1 GUI displayed this relationship in reverse.

## Verification

- 36 unit and safety tests pass.
- All four selectable 6221/2182A pair combinations pass deterministic simulation.
- Query counts: configuration 21 (6221) + 18 (2182A); measurement 10 (6221) + 9 (2182A).
- Real core address, identity, serial, firmware, and original core-query evidence remain based on the 2026-08-19 production run.
- The newly added Delta-state queries and `SENS:DATA:LATEST?` have been checked against the official command manuals and simulated locally, but still require their first production-instrument observation. The observer stops and saves partial evidence if any firmware rejects or times out on a query.
