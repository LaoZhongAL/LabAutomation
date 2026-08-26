@echo off
setlocal
set "DIAGNOSTIC_PYTHON=C:\LabAutomation\.venv\Scripts\python.exe"
set "DIAGNOSTIC_DIR=%~dp0"

if not exist "%DIAGNOSTIC_PYTHON%" (
  echo Shared Python environment not found:
  echo   %DIAGNOSTIC_PYTHON%
  echo.
  echo Create C:\LabAutomation\.venv or correct this launcher before continuing.
  pause
  exit /b 1
)

cd /d "%DIAGNOSTIC_DIR%"
"%DIAGNOSTIC_PYTHON%" "%DIAGNOSTIC_DIR%gpib6_2182a_monitor.py"
set "DIAGNOSTIC_EXIT=%ERRORLEVEL%"
if not "%DIAGNOSTIC_EXIT%"=="0" (
  echo.
  echo The diagnostic monitor exited with code %DIAGNOSTIC_EXIT%. Keep this window and record the message above.
  pause
)
exit /b %DIAGNOSTIC_EXIT%
