# Evening routine: settle outstanding fills, mark every account to market, then have
# trader-decider write the report and send it on Telegram.
#
# The scheduled task fires at BOTH 19:30 and 20:30 because announcements run an hour later
# during Sydney daylight saving. `arena evening-due` returns non-zero for whichever slot is
# wrong today, and this script then exits without doing anything. No changeover by hand.
$ErrorActionPreference = "Continue"
$venv = "C:\venvs\asx-bot\Scripts"
$repo = "G:\My Drive\asx-bot"

# G: is a Google Drive mount that exists only inside Rick's logged-in session. If the PC
# rebooted and nobody logged back in, the repo is not there at all - so leave a trace on a
# local disk rather than failing silently into a log file we could not write either.
if (-not (Test-Path $repo)) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm')  ABORTED (evening): $repo is not available. Google Drive is not mounted, which usually means nobody is logged in." |
        Out-File -Append -Encoding utf8 "C:\venvs\asx-bot\task-failures.log"
    exit 1
}
Set-Location $repo
$log = "$repo\data\arena_evening.log"

# Every write goes through here, so the log stays one encoding. PowerShell 5.1's `*>>`
# redirection and Out-File do not agree, and mixing them makes the file unreadable.
function Write-Log { param([string]$Text) $Text | Out-File -Append -Encoding utf8 $log }
function Invoke-Step {
    param([string[]]$Arguments)
    Write-Log "--- asxbot $($Arguments -join ' ') ---"
    Write-Log ((& "$venv\asxbot.exe" @Arguments 2>&1 | Out-String).TrimEnd())
}

$stamp = Get-Date -Format 'yyyy-MM-dd HH:mm'
& "$venv\asxbot.exe" arena evening-due 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Log "skipped ${stamp}: not today's evening slot"
    exit 0
}

Write-Log ""
Write-Log "=== evening report $stamp ==="
Invoke-Step @('arena', 'resolve')
Invoke-Step @('arena', 'mark')
Invoke-Step @('arena', 'report', '--agent', '--send')
