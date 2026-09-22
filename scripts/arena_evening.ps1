# Evening routine: settle outstanding fills, mark every account to market, then have
# trader-decider write the report and send it on Telegram.
#
# The scheduled task fires at BOTH 19:30 and 20:30 because announcements run an hour later
# during Sydney daylight saving. `arena evening-due` returns non-zero for whichever slot is
# wrong today, and this script then exits without doing anything. No changeover by hand.
$ErrorActionPreference = "Continue"
$venv = "C:\venvs\asx-bot\Scripts"
$repo = "G:\My Drive\asx-bot"
Set-Location $repo
$log = "$repo\data\arena_evening.log"

& "$venv\asxbot.exe" arena evening-due *>> $log
if ($LASTEXITCODE -ne 0) {
    "skipped $(Get-Date -Format 'yyyy-MM-dd HH:mm'): not today's slot" |
        Out-File -Append -Encoding utf8 $log
    exit 0
}

"=== evening report $(Get-Date -Format 'yyyy-MM-dd HH:mm') ===" |
    Out-File -Append -Encoding utf8 $log
& "$venv\asxbot.exe" arena resolve *>> $log
& "$venv\asxbot.exe" arena mark    *>> $log
& "$venv\asxbot.exe" arena report --agent --send *>> $log
