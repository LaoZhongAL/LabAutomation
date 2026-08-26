# English GUI Installation and Operation

## Update the installed source

Open PowerShell in the extracted 0.5.1 source directory. Reuse the already verified virtual environment from the previous production project:

```powershell
$ProbePython = "C:\LabAutomation\readonly_instrument_probe-0.4.0\.venv\Scripts\python.exe"
Test-Path -LiteralPath $ProbePython
```

The result must be `True`. Install the new source from the current directory:

```powershell
& $ProbePython -m pip install --no-deps --no-build-isolation -e .
```

Verify the source and installed metadata:

```powershell
& $ProbePython -c "import instrument_probe, importlib.metadata as m; print('source:', instrument_probe.__version__); print('installed:', m.version('readonly-instrument-probe'))"
```

Both values must be `0.5.1`.

Verify Tk and run the tests:

```powershell
& $ProbePython -c "import tkinter; print('Tk:', tkinter.TkVersion)"
& $ProbePython -m unittest discover -s tests -t . -v
```

All tests must pass before real VISA access.

## Start and test the GUI

```powershell
& $ProbePython -m instrument_probe.gui
```

The GUI always starts in `simulate` mode. Click **Scan Six Instruments** and verify that all six rows pass. Simulation does not load VISA or message real instruments.

## Run a real query-only scan

Before selecting `real`:

- Turn on all six instruments and allow self-tests to finish.
- Keep the confirmed GPIB wiring connected.
- Close the existing LabVIEW control program.
- Close any NI MAX VISA Test Panel; preferably close NI MAX.
- Ensure nobody is changing instrument settings from another interface.

Select `real`, then click **Scan Six Instruments** once. The scan is sequential. Do not launch LabVIEW, open a VISA Test Panel, change front-panel settings, or disconnect GPIB while it is running.

Expected query totals:

| VISA resource | Expected result |
|---|---|
| GPIB6 | Passed, 18/18 |
| GPIB7 | Passed, 18/18 |
| GPIB9 | Passed, 13/13, interlock closed |
| GPIB10 | Passed, 13/13, interlock closed |
| GPIB25 | Passed, 17/17 |
| GPIB26 | Warning, 17/17, interlock tripped |

The known GPIB26 interlock warning is a physical safety-state observation, not a scan failure. The GUI will not clear it, change wiring, or control the output.

Each scan creates a new timestamped directory under `gui_runs`, containing one evidence JSON file per instrument and `gui-scan-summary.json`. Existing evidence is never overwritten.
