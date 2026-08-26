# Keithley Read-Only Instrument Scanner 0.5.1

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

See `GUI_TUTORIAL_en.md` for installation and operation.
