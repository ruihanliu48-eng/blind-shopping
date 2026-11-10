# Start the Blind Shopping Assistant locally.
# Usage: right-click -> Run with PowerShell, or:  powershell -ExecutionPolicy Bypass -File .\start.ps1

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$python = "py"
$pyArgs = @("-3.14")
if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    $python = "python"
    $pyArgs = @()
}

if (Test-Path (Join-Path $root ".deps")) {
    $env:PYTHONPATH = Join-Path $root ".deps"
    Write-Host "Using bundled dependencies in .deps" -ForegroundColor Green
} else {
    Write-Host "Installing dependencies (one time)..." -ForegroundColor Yellow
    & $python @pyArgs -m pip install -r (Join-Path $root "requirements.txt")
}

Write-Host "Starting Streamlit on http://localhost:8501 ..." -ForegroundColor Green
& $python @pyArgs -m streamlit run (Join-Path $root "app.py") --server.headless false
