$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$Candidates = @()
if ($env:PROBE_PYTHON) {
    $Candidates += $env:PROBE_PYTHON
}
$Candidates += (Join-Path $PSScriptRoot ".venv\Scripts\python.exe")

$ExistingProjects = Get-ChildItem -LiteralPath "C:\LabAutomation" -Directory -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -like "readonly_instrument_probe*" } |
    Sort-Object Name -Descending
foreach ($Project in $ExistingProjects) {
    $Candidates += (Join-Path $Project.FullName ".venv\Scripts\python.exe")
}

$ProbePython = $Candidates |
    Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) } |
    Select-Object -First 1

if (-not $ProbePython) {
    throw "No existing instrument-probe Python environment was found. Set `$env:PROBE_PYTHON to the full python.exe path."
}

Write-Host "Python: $ProbePython"
& $ProbePython -c "import pyvisa; print('PyVISA:', pyvisa.__version__)"
if ($LASTEXITCODE -ne 0) {
    throw "The selected Python cannot import PyVISA. Use the same .venv that ran the read-only scanner."
}

& $ProbePython (Join-Path $PSScriptRoot "gpib_terminal.py")
exit $LASTEXITCODE
