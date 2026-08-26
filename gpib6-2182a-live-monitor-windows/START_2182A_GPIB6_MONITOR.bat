@echo off
setlocal
set "MONITOR_PYTHON=C:\LabAutomation\.venv\Scripts\python.exe"
set "MONITOR_DIR=%~dp0"

if not exist "%MONITOR_PYTHON%" (
  echo Shared Python environment not found:
  echo   %MONITOR_PYTHON%
  echo.
  echo Create C:\LabAutomation\.venv or correct this launcher before continuing.
  pause
  exit /b 1
)

cd /d "%MONITOR_DIR%"
"%MONITOR_PYTHON%" "%MONITOR_DIR%gpib6_2182a_monitor.py"
if errorlevel 1 (
  echo.
  echo The monitor closed with an error. Keep this window and record the message above.
  pause
)

