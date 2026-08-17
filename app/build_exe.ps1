# build_exe.ps1 - build the standalone MadihsonNSFW Toolset exe on Windows.
# Output: app\dist\MadihsonNSFW Toolset\MadihsonNSFW Toolset.exe
#
# THIS IS A WRAPPER. The build itself lives in tools\build_app.py, which the CI
# matrix also calls - PyInstaller cannot cross-compile, so Linux and macOS
# builds happen on their own runners, and one shared recipe is what stops the
# three drifting apart. Anything you want changed about the build goes THERE,
# not here. All this adds is the venv's Python.
#
# KEEP THIS FILE PURE ASCII. PowerShell 5.1 reads .ps1 as ANSI, so a UTF-8 dash
# or arrow here becomes mojibake and breaks string parsing ("missing terminator").
$app = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $app
$python = Join-Path $app ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Host "app venv not found: $python"
    exit 1
}

# The RUNNING-APP GUARD lives in build_app.py so every platform gets the same
# refusal, but Get-Process gives a better message here: it can name the PID.
$running = @(Get-Process -Name "MadihsonNSFW Toolset" -ErrorAction SilentlyContinue)
if ($running.Count -gt 0) {
    Write-Host "REFUSING TO BUILD: 'MadihsonNSFW Toolset' is running (PID $($running[0].Id))."
    Write-Host "Close the app first, then run this again."
    exit 1
}

& $python (Join-Path $root "tools\build_app.py") @args
exit $LASTEXITCODE
