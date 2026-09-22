# Tactic 1 warm-up: watch ASX announcements and run the agents on each one.
# Started by the Windows scheduled task "ASXBot Arena Warmup" at 07:30 Sydney, weekdays.
#
# --until auto stops the watcher just before announcements end: 19:25 normally, 20:25
# during daylight saving. Anything released in those last minutes, or after the close, is
# picked up by the next morning's catch-up and queued for the pre-open.
$ErrorActionPreference = "Continue"
$venv = "C:\venvs\asx-bot\Scripts"
$repo = "G:\My Drive\asx-bot"
Set-Location $repo
"=== warm-up starting $(Get-Date -Format 'yyyy-MM-dd HH:mm') ===" |
    Out-File -Append -Encoding utf8 "$repo\data\arena_warmup.log"
& "$venv\asxbot.exe" arena watch --until auto *>> "$repo\data\arena_warmup.log"
