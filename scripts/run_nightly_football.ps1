# Nightly 2026-27 football team + player scrape, then commit and push cache files.
#
# Usage (manual test):
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_nightly_football.ps1
#
# Optional:
#   -Season "2026-2027"
#   -NoGameLogs          # faster player scrape (season totals only)
#   -SkipGitPush         # scrape only, no commit/push
#   -GitRemote "origin"
#
# One-time setup:
#   py -3 -m pip install -r requirements.txt
#   py -3 -m playwright install chromium
#   git push   (once, to store GitHub credentials in Windows Credential Manager)
#
# Schedule at 3:00 AM:
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_nightly_football_task.ps1

[CmdletBinding()]
param(
    [string] $Season = "2026-2027",
    [switch] $NoGameLogs,
    [switch] $SkipGitPush,
    [string] $GitRemote = "origin"
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $RepoRoot

$LogDir = Join-Path $RepoRoot "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogFile = Join-Path $LogDir ("nightly_football_{0:yyyy-MM-dd}.log" -f (Get-Date))

function Write-Log {
    param([string] $Message)
    $line = "[{0:yyyy-MM-dd HH:mm:ss}] {1}" -f (Get-Date), $Message
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
    Write-Host $line
}

function Get-PythonExe {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        return @{ Exe = "py"; Args = @("-3") }
    }
    if (Get-Command python -ErrorAction SilentlyContinue) {
        return @{ Exe = "python"; Args = @() }
    }
    throw "Python not found. Install Python 3 and ensure 'py' or 'python' is on PATH."
}

function Invoke-LoggedCommand {
    param(
        [string] $Label,
        [string] $Exe,
        [string[]] $InitialArgs,
        [string[]] $CommandArgs
    )

    $quoted = ($CommandArgs | ForEach-Object {
        if ($_ -match '\s') { """$_""" } else { $_ }
    }) -join " "
    Write-Log "START $Label : $Exe $($InitialArgs -join ' ') $quoted"

    $output = & $Exe @InitialArgs @CommandArgs 2>&1
    $exitCode = $LASTEXITCODE
    if ($null -ne $output) {
        $output | ForEach-Object { Add-Content -Path $LogFile -Value $_ -Encoding UTF8 }
        $output | ForEach-Object { Write-Host $_ }
    }

    if ($exitCode -ne 0) {
        throw "$Label failed with exit code $exitCode"
    }
    Write-Log "DONE $Label"
}

$python = Get-PythonExe
$teamsCache = "football_data_cache_$Season.json"
$playerCache = "football_player_data_cache_$Season.json"

Write-Log "=== Nightly football scrape ($Season) ==="
Write-Log "Repo: $RepoRoot"
Write-Log "Log: $LogFile"

try {
    Invoke-LoggedCommand -Label "team scraper" -Exe $python.Exe -InitialArgs $python.Args -CommandArgs @(
        "scraper.py",
        "--sport", "football",
        "--season", $Season
    )

    $playerArgs = @(
        "football_player_scraper.py",
        "--season", $Season,
        "--teams-cache", $teamsCache
    )
    if ($NoGameLogs) {
        $playerArgs += "--no-games"
    }
    Invoke-LoggedCommand -Label "player scraper" -Exe $python.Exe -InitialArgs $python.Args -CommandArgs $playerArgs

    if ($SkipGitPush) {
        Write-Log "SkipGitPush set; leaving git unchanged."
        Write-Log "=== Completed successfully (no git push) ==="
        exit 0
    }

    foreach ($file in @($teamsCache, $playerCache)) {
        if (-not (Test-Path (Join-Path $RepoRoot $file))) {
            throw "Expected cache file missing after scrape: $file"
        }
    }

    Write-Log "START git add"
    & git add -- $teamsCache $playerCache 2>&1 | Tee-Object -FilePath $LogFile -Append
    if ($LASTEXITCODE -ne 0) { throw "git add failed with exit code $LASTEXITCODE" }

    $status = git status --porcelain -- $teamsCache $playerCache
    if (-not $status) {
        Write-Log "No cache changes to commit."
        Write-Log "=== Completed successfully (nothing to push) ==="
        exit 0
    }

    $commitMsg = "chore: nightly football $Season scrape"
    Write-Log "START git commit"
    & git commit -m $commitMsg 2>&1 | Tee-Object -FilePath $LogFile -Append
    if ($LASTEXITCODE -ne 0) { throw "git commit failed with exit code $LASTEXITCODE" }

    Write-Log "START git push $GitRemote"
    & git push $GitRemote HEAD 2>&1 | Tee-Object -FilePath $LogFile -Append
    if ($LASTEXITCODE -ne 0) { throw "git push failed with exit code $LASTEXITCODE" }

    Write-Log "=== Completed successfully ==="
    exit 0
}
catch {
    Write-Log "ERROR: $($_.Exception.Message)"
    exit 1
}
