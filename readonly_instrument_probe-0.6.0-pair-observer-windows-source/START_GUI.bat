@echo off
setlocal
cd /d "%~dp0"
set "PROBE_GUI_PYTHON=%~dp0.venv\Scripts\python.exe"

if not exist "%PROBE_GUI_PYTHON%" (
  echo Python environment not found:
  echo   %PROBE_GUI_PYTHON%
  echo.
  echo Create or copy the project virtual environment before starting the GUI.
  pause
  exit /b 1
)

"%PROBE_GUI_PYTHON%" -m instrument_probe.gui
if errorlevel 1 (
  echo.
  echo The GUI closed with an error. Keep this window and record the message above.
  pause
)
