# Keithley 6221 / 2182A Manual Pair Observer 0.6.0

This English Windows GUI lets the operator select any confirmed laboratory 6221 and any confirmed laboratory 2182A as a software pair. Pair selection records operator intent; it does not infer or change physical wiring.

Confirmed choices:

| Role | VISA resources |
|---|---|
| Keithley 6221 current source | `GPIB0::9::INSTR`, `GPIB0::10::INSTR` |
| Keithley 2182A nanovoltmeter | `GPIB0::6::INSTR`, `GPIB0::7::INSTR` |

The GUI has two explicit read buttons:

- **Read Pair Configuration** takes one allowlisted setup snapshot.
- **Read Latest Measurement Snapshot** takes one short state snapshot and reads the 2182A cached latest value with `SENS:DATA:LATEST?`.

There is no automatic VISA refresh. The operator performs all wiring, source setup, output control, acquisition, and shutdown actions. The application has no arbitrary command entry and no reset, clear, trigger, acquisition, configuration-write, or output-control path.

The local `V/I` estimate is shown only when both snapshots complete, the 6221 output is on, programmed current is nonzero, a cached voltage is available, and 6221 Delta mode is not armed. It is not an accuracy pass/fail result.

Start in simulation:

```powershell
& $ProbePython -m instrument_probe.pair_gui
```

The application always starts in `simulate` mode. See `PAIR_GUI_TUTORIAL_zh.md` for the production procedure and safety boundaries.
