# build_exe.ps1 - build the standalone MadihsonNSFW Toolset exe (PyInstaller onedir).
# Output: app\dist\MadihsonNSFW Toolset\MadihsonNSFW Toolset.exe
# Your local config.json is copied next to the exe for testing; do NOT ship it -
# a fresh user gets defaults (library folder next to the exe) on first run.
#
# KEEP THIS FILE PURE ASCII. PowerShell 5.1 reads .ps1 as ANSI, so a UTF-8 dash
# or arrow here becomes mojibake and breaks string parsing ("missing terminator").
$app = Split-Path -Parent $MyInvocation.MyCommand.Path
$exe = "$app\dist\MadihsonNSFW Toolset\MadihsonNSFW Toolset.exe"

# RUNNING-APP GUARD. A running exe cannot be replaced on Windows, and
# PyInstaller does not fail politely about it - it dies part way through and
# leaves the OLD binary in place. That is how two rebuilds were silently
# skipped (docs\app-shell.md). Check before spending minutes on a build.
$running = @(Get-Process -Name "MadihsonNSFW Toolset" -ErrorAction SilentlyContinue)
if ($running.Count -gt 0) {
    Write-Host "REFUSING TO BUILD: 'MadihsonNSFW Toolset' is running (PID $($running[0].Id))."
    Write-Host "Close the app first, then run this again."
    exit 1
}
if (Test-Path $exe) {
    # Catches holders that are not the app itself: an antivirus scan, an
    # Explorer preview, a debugger, a half-dead process with no window.
    try {
        $stream = [System.IO.File]::Open($exe, 'Open', 'ReadWrite', 'None')
        $stream.Close()
    } catch {
        Write-Host "REFUSING TO BUILD: another process is holding the exe:"
        Write-Host "  $exe"
        Write-Host "Close whatever has it open, then run this again."
        exit 1
    }
}

# Pack the Blender add-on into the app FIRST, straight from its source folder.
# Doing it here rather than by hand is the point: the exe can never ship an
# add-on older than the source it was built from, because there is no separate
# step to forget. See tools\pack_addon.py.
& "$app\.venv\Scripts\python.exe" "$app\..\tools\pack_addon.py"
if ($LASTEXITCODE -ne 0) {
    Write-Host "REFUSING TO BUILD: could not pack the Blender add-on."
    exit 1
}

# The version being built. This is the release name too - the updater's
# manifest is stamped with it (UPDATER_PLAN.md).
$ver = "unknown"
$hit = @(Select-String -Path "$app\version.py" -Pattern '^APP_VERSION\s*=\s*"([^"]+)"' -ErrorAction SilentlyContinue)
if ($hit.Count -gt 0) { $ver = $hit[0].Matches[0].Groups[1].Value }
Write-Host "Building MadihsonNSFW Toolset $ver"

# CHANGELOG.md is DATA, not code, so PyInstaller does not pick it up by
# following imports - it has to be named. It is what the "What's New" tab
# shows, and a build without it would open that tab on an apology.
# --icon burns it into the EXE (Explorer, the task bar, the Alt-Tab card);
# --add-data ships the same file so Qt can set it as the WINDOW icon at run
# time. Both are needed - neither one alone covers the other's surface.
# zstandard is imported inside a try/except in blendsize.py so the app still
# runs without it - which means a build that silently dropped it would look
# fine and refuse nearly every real .blend, since Blender compresses by
# default. Named explicitly, including the C backend, so that cannot happen.
& "$app\.venv\Scripts\pyinstaller.exe" --noconfirm --clean --windowed `
    --name "MadihsonNSFW Toolset" `
    --icon "$app\assets\app_icon.ico" `
    --hidden-import zstandard `
    --hidden-import zstandard.backend_c `
    --add-data "$app\CHANGELOG.md;." `
    --add-data "$app\assets\app_icon.ico;." `
    --distpath "$app\dist" --workpath "$app\build" --specpath "$app\build" `
    "$app\main.py"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

# ...and a copy BESIDE the exe, which is the one the tab prefers. That is what
# makes it possible to correct a typo in the notes without a rebuild.
Copy-Item "$app\CHANGELOG.md" "$app\dist\MadihsonNSFW Toolset\CHANGELOG.md" -Force

if (Test-Path "$app\config.json") {
    Copy-Item "$app\config.json" "$app\dist\MadihsonNSFW Toolset\config.json" -Force
    Write-Host "Copied local config.json next to the exe (testing convenience)."
}
Write-Host "Built: $exe  (version $ver)"
Write-Host "NOTE: the exe name changed - re-point 'Toolset App' in the add-on preferences."
