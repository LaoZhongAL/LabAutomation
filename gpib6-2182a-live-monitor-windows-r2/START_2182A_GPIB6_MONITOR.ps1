$ErrorActionPreference = "Stop"
$MonitorPython = "C:\LabAutomation\.venv\Scripts\python.exe"
$MonitorDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path

if (-not (Test-Path -LiteralPath $MonitorPython)) {
    throw "Shared Python environment not found: $MonitorPython"
}

Set-Location -LiteralPath $MonitorDirectory
& $MonitorPython (Join-Path $MonitorDirectory "gpib6_2182a_monitor.py")

