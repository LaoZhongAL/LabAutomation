@echo off
setlocal
cd /d "%~dp0"

if defined PROBE_PYTHON set "PAIR_GUI_PYTHON=%PROBE_PYTHON%"
if not defined PAIR_GUI_PYTHON set "PAIR_GUI_PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%PAIR_GUI_PYTHON%" set "PAIR_GUI_PYTHON=C:\LabAutomation\readonly_instrument_probe-0.5.1-english-gui-windows-source\.venv\Scripts\python.exe"

if not exist "%PAIR_GUI_PYTHON%" (
  echo No verified Python environment was found.
  echo.
  echo Checked the local .venv, the PROBE_PYTHON environment variable,
  echo and the laboratory's previously verified 0.5.1 environment.
  echo Follow PAIR_GUI_TUTORIAL_zh.md before real VISA access.
  pause
  exit /b 1
)

echo Using Python: %PAIR_GUI_PYTHON%
"%PAIR_GUI_PYTHON%" -m instrument_probe.pair_gui
if errorlevel 1 (
  echo.
  echo The Pair Observer closed with an error. Keep this window and record the message above.
  pause
)
