$ErrorActionPreference = "Stop"
$DiagnosticPython = "C:\LabAutomation\.venv\Scripts\python.exe"
$DiagnosticDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path

if (-not (Test-Path -LiteralPath $DiagnosticPython)) {
    throw "Shared Python environment not found: $DiagnosticPython"
}

Set-Location -LiteralPath $DiagnosticDirectory
& $DiagnosticPython (Join-Path $DiagnosticDirectory "gpib6_2182a_monitor.py")
$DiagnosticExitCode = $LASTEXITCODE

if ($DiagnosticExitCode -ne 0) {
    Write-Host ""
    Write-Host "The diagnostic monitor exited with code $DiagnosticExitCode. Keep this window and record the message above."
    Read-Host "Press Enter to close"
}

exit $DiagnosticExitCode
