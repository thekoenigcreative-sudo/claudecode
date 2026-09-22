# Evening routine: settle any outstanding fills, mark every account to market, then have
# trader-decider write the report and send it on Telegram.
# Started by the Windows scheduled task "ASXBot Arena Evening" at 19:30 Sydney.
$ErrorActionPreference = "Continue"
$venv = "C:\venvs\asx-bot\Scripts"
$repo = "G:\My Drive\asx-bot"
Set-Location $repo
$log = "$repo\data\arena_evening.log"
"=== $(Get-Date -Format 'yyyy-MM-dd HH:mm') ===" | Out-File -Append -Encoding utf8 $log
& "$venv\asxbot.exe" arena resolve *>> $log
& "$venv\asxbot.exe" arena mark    *>> $log
& "$venv\asxbot.exe" arena report --agent --send *>> $log
