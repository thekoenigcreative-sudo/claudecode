# Create the "ASXBot Chat" scheduled task: the Trader's own Telegram chat with Rick
# (src\asxbot\chat.py), started one minute after Rick logs on, with no window (pythonw),
# and kept running: no time limit, one instance, restarted if it fails.
#
# scripts\chat.pyw runs `asxbot chat` and restarts it itself after a crash (60 s, at most
# 5 an hour); the task's own restart-on-failure is the backstop for the launcher. Logs:
# %LOCALAPPDATA%\asx-bot\logs\chat.log.
#
# BEFORE running this, OpenClaw must no longer poll the bot: remove
# channels.telegram.accounts.trader and its binding from ~\.openclaw\openclaw.json. The chat
# refuses to start (exit 10, logged) until then. See docs\chat.md, "Going live".
#
# Run once, by hand, in a normal (not elevated) PowerShell, after Rick agrees:
#     powershell -NoProfile -ExecutionPolicy Bypass -File "G:\My Drive\asx-bot\scripts\register_chat_task.ps1"
# Start it now (instead of waiting for the next logon):
#     schtasks /run /tn "ASXBot Chat"
# Remove it again with:
#     Unregister-ScheduledTask -TaskName "ASXBot Chat" -Confirm:$false
$ErrorActionPreference = "Stop"
$task = "ASXBot Chat"
$repo = "G:\My Drive\asx-bot"
$pythonw = "C:\venvs\asx-bot\Scripts\pythonw.exe"
$script = "$repo\scripts\chat.pyw"

foreach ($p in @($pythonw, $script)) {
    if (-not (Test-Path $p)) { throw "not found: $p - nothing was created" }
}
if (Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue) {
    throw "$task already exists - nothing was changed. Unregister it first to recreate it."
}

$action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$script`"" -WorkingDirectory $repo

# At Rick's logon, plus one minute: G: (Google Drive) is only there inside his session.
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$trigger.Delay = "PT1M"

# The same account and logon type as the other ASXBot tasks: Rick's session, where G: exists.
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -Hidden `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName $task -Action $action -Trigger $trigger -Principal $principal `
    -Settings $settings -Description "The Trader's Telegram chat with Rick (asxbot chat). Answers only Rick; never places an order." | Out-Null

# Read it back.
$t = Get-ScheduledTask -TaskName $task
$tr = $t.Triggers[0]
$s = $t.Settings
Write-Output "task:       $task"
Write-Output "action:     $($t.Actions[0].Execute) $($t.Actions[0].Arguments)"
Write-Output "in folder:  $($t.Actions[0].WorkingDirectory)"
Write-Output "trigger:    at logon of $($tr.UserId), delay $($tr.Delay)"
Write-Output "runs as:    $($t.Principal.UserId) ($($t.Principal.LogonType), $($t.Principal.RunLevel))"
Write-Output "settings:   hidden=$($s.Hidden) instances=$($s.MultipleInstances) time limit=$($s.ExecutionTimeLimit) restart=$($s.RestartCount) x $($s.RestartInterval)"
Write-Output "state:      $($t.State)"
if ($tr.Delay -ne "PT1M" -or $s.ExecutionTimeLimit -ne "PT0S" -or $s.MultipleInstances -ne "IgnoreNew" -or -not $s.Hidden -or $s.RestartCount -ne 3) {
    throw "the task is not what was asked for - check it, or Unregister-ScheduledTask and rerun"
}
Write-Output "OK: $task created. It starts at the next logon; to start it now: schtasks /run /tn `"$task`""
Write-Output "Undo: Unregister-ScheduledTask -TaskName `"$task`" -Confirm:`$false"
