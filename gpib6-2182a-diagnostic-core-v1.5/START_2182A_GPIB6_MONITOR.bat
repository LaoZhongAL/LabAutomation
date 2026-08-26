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
set "MONITOR_EXIT=%ERRORLEVEL%"
if not "%MONITOR_EXIT%"=="0" (
  echo.
  echo The diagnostic monitor exited with code %MONITOR_EXIT%. Keep this window and record the message above.
  pause
)
exit /b %MONITOR_EXIT%
