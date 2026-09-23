# Weekly: what each screen filter threw away, measured against what those stocks did.
# Started by the Windows scheduled task "ASXBot Filter Cost", Sundays 18:00 Sydney.
#
# This REPORTS. It changes nothing. No threshold moves because of a number in this report;
# a rule that looks expensive earns a dated change in config.yaml, made deliberately.
$ErrorActionPreference = "Continue"
$venv = "C:\venvs\asx-bot\Scripts"
$repo = "G:\My Drive\asx-bot"

# G: is a Google Drive mount that exists only inside Rick's logged-in session.
if (-not (Test-Path $repo)) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm')  ABORTED (filter cost): $repo is not available. Google Drive is not mounted, which usually means nobody is logged in." |
        Out-File -Append -Encoding utf8 "C:\venvs\asx-bot\task-failures.log"
    exit 1
}
Set-Location $repo
$log = "$repo\data\arena_filter_cost.log"

function Write-Log { param([string]$Text) $Text | Out-File -Append -Encoding utf8 $log }

Write-Log ""
Write-Log "=== filter cost starting $(Get-Date -Format 'yyyy-MM-dd HH:mm') ==="
Write-Log ((& "$venv\asxbot.exe" arena filter-cost --weeks 8 --send 2>&1 | Out-String).TrimEnd())
Write-Log "=== filter cost finished $(Get-Date -Format 'yyyy-MM-dd HH:mm') ==="
