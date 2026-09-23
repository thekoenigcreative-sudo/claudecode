# Create the "ASXBot Arena Watchdog" scheduled task: one check of the watcher every
# 5 minutes, Monday to Friday, 07:30 to 20:30, with no window (pythonw).
#
# It alerts on Telegram (the arena bot) once when the watcher is down or its log has gone
# silent, and once when it is back. See src\asxbot\arena\watchdog.py.
#
# Why 20:30 and not 19:30: the watcher stops at 19:25 now but at 20:25 during daylight
# saving (from 4 Oct 2026). The watchdog works out each day's hours itself - trading day,
# 07:33 until the watcher's stop time - and does nothing outside them, so running the
# task until 20:30 all year costs nothing and covers both. Holidays are skipped the same
# way.
#
# Run once, by hand, in a normal (not elevated) PowerShell, after Rick agrees:
#     powershell -NoProfile -ExecutionPolicy Bypass -File "G:\My Drive\asx-bot\scripts\schedule_watchdog.ps1"
# Remove it again with:
#     Unregister-ScheduledTask -TaskName "ASXBot Arena Watchdog" -Confirm:$false
$ErrorActionPreference = "Stop"
$task = "ASXBot Arena Watchdog"
$repo = "G:\My Drive\asx-bot"
$pythonw = "C:\venvs\asx-bot\Scripts\pythonw.exe"
$script = "$repo\scripts\arena_watchdog.pyw"

foreach ($p in @($pythonw, $script)) {
    if (-not (Test-Path $p)) { throw "not found: $p - nothing was created" }
}
if (Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue) {
    throw "$task already exists - nothing was changed. Unregister it first to recreate it."
}

$action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$script`"" -WorkingDirectory $repo

# A weekly trigger cannot take a repetition directly in PowerShell 5.1, so borrow one.
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At "07:30"
$trigger.Repetition = (New-ScheduledTaskTrigger -Once -At "07:30" `
        -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Hours 13)).Repetition

# The same account and logon type as the other ASXBot tasks: Rick's session, where G: exists.
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 4)

Register-ScheduledTask -TaskName $task -Action $action -Trigger $trigger -Principal $principal `
    -Settings $settings -Description "Checks every 5 minutes that the arena watcher is running; alerts Rick on Telegram if not. Never starts or stops anything." | Out-Null

# Read it back.
$t = Get-ScheduledTask -TaskName $task
$tr = $t.Triggers[0]
Write-Output "action:     $($t.Actions[0].Execute) $($t.Actions[0].Arguments)"
Write-Output "trigger:    from $($tr.StartBoundary), days=$($tr.DaysOfWeek) (62 = Mon-Fri), every $($tr.Repetition.Interval) for $($tr.Repetition.Duration)"
Write-Output "runs as:    $($t.Principal.UserId) ($($t.Principal.LogonType))"
Write-Output "next run:   $((Get-ScheduledTaskInfo -TaskName $task).NextRunTime)"
if ($tr.Repetition.Interval -ne "PT5M" -or $tr.Repetition.Duration -ne "PT13H" -or $tr.DaysOfWeek -ne 62) {
    throw "the trigger is not what was asked for - check it, or Unregister-ScheduledTask and rerun"
}
Write-Output "OK: $task created. It does nothing before 07:33 or after the watcher's stop time."
