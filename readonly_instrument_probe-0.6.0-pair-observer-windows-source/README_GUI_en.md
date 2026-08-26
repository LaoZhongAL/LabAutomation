# Keithley Read-Only Instrument Scanner 0.6.0

This package provides an English-language Windows GUI for a fixed, confirmed laboratory map:

| VISA resource | Instrument | Command set |
|---|---|---|
| `GPIB0::6::INSTR` | Keithley 2182A | SCPI |
| `GPIB0::7::INSTR` | Keithley 2182A | SCPI |
| `GPIB0::9::INSTR` | Keithley 6221 | SCPI |
| `GPIB0::10::INSTR` | Keithley 6221 | SCPI |
| `GPIB0::25::INSTR` | Keithley 2450 | TSP |
| `GPIB0::26::INSTR` | Keithley 2450 | TSP |

The GUI runs only the field-verified core query allowlists. It has no arbitrary command box and no controls for reset, clear, trigger, acquisition, source configuration, or output changes.

Start it with:

```powershell
& $ProbePython -m instrument_probe.gui
```

The application always starts in `simulate` mode. Select `real` explicitly only after closing LabVIEW and any NI MAX VISA Test Panel.

Version 0.6.0 also includes the English **Manual Pair Observer** for freely selecting one confirmed 6221 and one confirmed 2182A. It performs button-triggered snapshots only and never automatically refreshes VISA:

```powershell
& $ProbePython -m instrument_probe.pair_gui
```

The six-instrument GUI also has an **Open Pair Observer** button. See `PAIR_GUI_README_en.md` and `PAIR_GUI_TUTORIAL_zh.md` for its exact boundaries.

See `GUI_TUTORIAL_en.md` for installation and operation.
