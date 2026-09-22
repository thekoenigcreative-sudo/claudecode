# Tactic 1 warm-up: watch ASX announcements and run the agents on each one.
# Started by the Windows scheduled task "ASXBot Arena Warmup" at 07:30 Sydney.
# Exits at 19:25 so tomorrow's task starts a fresh process.
$ErrorActionPreference = "Continue"
$venv = "C:\venvs\asx-bot\Scripts"
$repo = "G:\My Drive\asx-bot"
Set-Location $repo
& "$venv\asxbot.exe" arena watch --until 19:25 *>> "$repo\data\arena_warmup.log"
