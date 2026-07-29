# Local nightly run — the authoritative one.
#
# Register with Task Scheduler to run at 00:30 local (UTC+3), which is ~17:30 New York:
# an hour and a half after the close, enough for end-of-day data to settle.
#
#   Register-ScheduledTask -TaskName "qms-nightly" -Action (New-ScheduledTaskAction `
#       -Execute "powershell.exe" `
#       -Argument "-NoProfile -ExecutionPolicy Bypass -File C:\Users\User\repos\scanner\scripts\nightly.ps1") `
#       -Trigger (New-ScheduledTaskTrigger -Daily -At 00:30) `
#       -Settings (New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable)
#
# -WakeToRun wakes a sleeping machine; -StartWhenAvailable catches up after a miss.
#
# This does NOT publish to GitHub Pages. The Actions workflow does that from its own data,
# because the report is ~7 MB and committing it nightly would bloat the repo. Open the
# local file directly, or run `uv run qms publish` and serve `site/` yourself.

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$LogDir   = Join-Path $RepoRoot "data\logs"
$LogFile  = Join-Path $LogDir ("nightly-{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# Task Scheduler starts with the machine environment, which may predate the uv install.
$env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
            [System.Environment]::GetEnvironmentVariable("Path", "User")

function Write-Log {
    param([string] $Message)
    $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Write-Output $line
    Add-Content -Path $LogFile -Value $line -Encoding utf8
}

Write-Log "=== qms nightly starting ==="
Set-Location $RepoRoot

try {
    # No --allow-stale on purpose. If the quality gate fails, the correct outcome is a
    # loud failure and no report, not a normal-looking watchlist built on old prices.
    $output = & uv run qms nightly 2>&1
    $exit = $LASTEXITCODE

    foreach ($line in $output) { Write-Log $line }

    if ($exit -ne 0) {
        Write-Log "FAILED with exit code $exit"
        Write-Log "If this is the data-quality gate, read the message before overriding it."
        exit $exit
    }

    Write-Log "=== finished; report in out\ ==="
}
catch {
    Write-Log "ERROR: $_"
    exit 1
}
