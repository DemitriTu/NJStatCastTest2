# Register a Windows Task Scheduler job to run the nightly football scrape at 3:00 AM.
#
# Usage (run once from the repo):
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_nightly_football_task.ps1
#
# Optional:
#   -Time "03:00"
#   -TaskName "NJStatCast Football Nightly Scrape"
#   -NoGameLogs
#
# Requirements:
#   - Run as your Windows user (default) so stored git credentials work for git push.
#   - PC must be on (or asleep with "Wake the computer to run") at the scheduled time.
#   - One-time: py -3 -m playwright install chromium

[CmdletBinding()]
param(
    [string] $Time = "03:00",
    [string] $TaskName = "NJStatCast Football Nightly Scrape",
    [switch] $NoGameLogs
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RunScript = Join-Path $RepoRoot "scripts\run_nightly_football.ps1"

if (-not (Test-Path $RunScript)) {
    throw "Missing script: $RunScript"
}

$argList = "-NoProfile -ExecutionPolicy Bypass -File `"$RunScript`""
if ($NoGameLogs) {
    $argList += " -NoGameLogs"
}

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument $argList `
    -WorkingDirectory $RepoRoot

$trigger = New-ScheduledTaskTrigger -Daily -At $Time

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 12)

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Scrape 2026-27 football team and player caches, commit, and push to GitHub." | Out-Null

Write-Host "Registered scheduled task: $TaskName"
Write-Host "  Time:    daily at $Time"
Write-Host "  Script:  $RunScript"
Write-Host "  Workdir: $RepoRoot"
Write-Host ""
Write-Host "Test manually:"
Write-Host "  powershell -NoProfile -ExecutionPolicy Bypass -File `"$RunScript`" -SkipGitPush"
Write-Host ""
Write-Host "View in Task Scheduler: taskschd.msc"
