$ErrorActionPreference = "Stop"
$MonitorPython = "C:\LabAutomation\.venv\Scripts\python.exe"
$MonitorDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path

if (-not (Test-Path -LiteralPath $MonitorPython)) {
    throw "Shared Python environment not found: $MonitorPython"
}

Set-Location -LiteralPath $MonitorDirectory
& $MonitorPython (Join-Path $MonitorDirectory "gpib6_2182a_monitor.py")
$MonitorExitCode = $LASTEXITCODE

if ($MonitorExitCode -ne 0) {
    Write-Host ""
    Write-Host "The diagnostic monitor exited with code $MonitorExitCode. Keep this window and record the message above."
    Read-Host "Press Enter to close"
}

exit $MonitorExitCode
