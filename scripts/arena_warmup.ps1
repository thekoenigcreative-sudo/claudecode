# Tactic 1 warm-up: watch ASX announcements and run the agents on each one.
# Started by the Windows scheduled task "ASXBot Arena Warmup" at 07:30 Sydney, weekdays.
#
# --until auto stops the watcher just before announcements end: 19:25 normally, 20:25
# during daylight saving. Anything released in those last minutes, or after the close, is
# picked up by the next morning's catch-up and queued for the pre-open.
$ErrorActionPreference = "Continue"
$venv = "C:\venvs\asx-bot\Scripts"
$repo = "G:\My Drive\asx-bot"

# G: is a Google Drive mount that exists only inside Rick's logged-in session. If the PC
# rebooted and nobody logged back in, the repo is not there at all - so leave a trace on a
# local disk rather than failing silently into a log file we could not write either.
if (-not (Test-Path $repo)) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm')  ABORTED (warm-up): $repo is not available. Google Drive is not mounted, which usually means nobody is logged in." |
        Out-File -Append -Encoding utf8 "C:\venvs\asx-bot\task-failures.log"
    exit 1
}
Set-Location $repo
$log = "$repo\data\arena_warmup.log"

# One encoding for the whole file: PowerShell 5.1's `*>>` redirection and Out-File do not
# agree, and mixing them leaves the log unreadable.
function Write-Log { param([string]$Text) $Text | Out-File -Append -Encoding utf8 $log }

Write-Log ""
Write-Log "=== warm-up starting $(Get-Date -Format 'yyyy-MM-dd HH:mm') ==="
Write-Log ((& "$venv\asxbot.exe" arena watch --until auto 2>&1 | Out-String).TrimEnd())
Write-Log "=== warm-up finished $(Get-Date -Format 'yyyy-MM-dd HH:mm') ==="
