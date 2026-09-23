# Re-point the "ASXBot Arena Evening" scheduled task at the hidden launcher.
#
# Before: powershell.exe -File scripts\arena_evening.ps1 - a visible console window. Closing
#         it would kill the evening routine: no settlement, no marks, no report. The watcher
#         died exactly that way at 13:32 on 23 Sep 2026.
# After:  C:\venvs\asx-bot\Scripts\pythonw.exe scripts\arena_evening.pyw - no window at all.
#         Same steps (evening-due, then resolve, mark, report --agent --send), same repo
#         folder, same log (data\arena_evening.log, plus data\logs\asxbot.log as always).
#
# Only the action changes. The triggers (19:30 and 20:30 Mon-Fri), the settings (45 min
# limit, one instance, restart on failure, start when available, wake to run) and the
# account it runs as are kept exactly as they are. The task's current definition is saved
# first, so this can be undone:
#     Register-ScheduledTask -TaskName "ASXBot Arena Evening" -Force `
#         -Xml (Get-Content "<the backup file this prints>" -Raw)
#
# Run once, by hand, in a normal (not elevated) PowerShell:
#     powershell -NoProfile -ExecutionPolicy Bypass -File "G:\My Drive\asx-bot\scripts\schedule_evening_hidden.ps1"
# It does not run the evening routine. The 19:30 / 20:30 triggers do that.
$ErrorActionPreference = "Stop"
$task = "ASXBot Arena Evening"
$repo = "G:\My Drive\asx-bot"
$pythonw = "C:\venvs\asx-bot\Scripts\pythonw.exe"
$launcher = "$repo\scripts\arena_evening.pyw"
$backups = "$repo\data\task_backups"

foreach ($p in @($pythonw, $launcher)) {
    if (-not (Test-Path $p)) { throw "not found: $p - nothing was changed" }
}
$current = Get-ScheduledTask -TaskName $task
if ($current.State -eq "Running") {
    throw "$task is running now. Changing it would not stop that run, but do this when it is idle - nothing was changed"
}

New-Item -ItemType Directory -Force $backups | Out-Null
$backup = Join-Path $backups ("{0} {1}.xml" -f $task, (Get-Date -Format "yyyy-MM-dd HHmmss"))
Export-ScheduledTask -TaskName $task | Out-File -Encoding unicode $backup
Write-Output "saved the current definition to: $backup"

function Describe-Task($t) {
    $trig = ($t.Triggers | ForEach-Object { "$($_.StartBoundary) days=$($_.DaysOfWeek) enabled=$($_.Enabled)" }) -join "; "
    $s = $t.Settings
    $set = "limit=$($s.ExecutionTimeLimit) instances=$($s.MultipleInstances) restart=$($s.RestartCount)x$($s.RestartInterval) whenAvailable=$($s.StartWhenAvailable) wake=$($s.WakeToRun) battery=$($s.DisallowStartIfOnBatteries)/$($s.StopIfGoingOnBatteries)"
    $who = "$($t.Principal.UserId) $($t.Principal.LogonType)"
    return @($trig, $set, $who)
}
$before = Describe-Task $current

$action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$launcher`"" -WorkingDirectory $repo
Set-ScheduledTask -TaskName $task -Action $action | Out-Null

# Read it back: the artefact, not this script's account of it.
$after = Get-ScheduledTask -TaskName $task
$afterDesc = Describe-Task $after
Write-Output "action now:   $($after.Actions[0].Execute) $($after.Actions[0].Arguments)"
Write-Output "working dir:  $($after.Actions[0].WorkingDirectory)"
Write-Output "triggers:     $($afterDesc[0])"
Write-Output "settings:     $($afterDesc[1])"
Write-Output "runs as:      $($afterDesc[2])"
Write-Output "next run:     $((Get-ScheduledTaskInfo -TaskName $task).NextRunTime)"
Write-Output "undo:         Register-ScheduledTask -TaskName `"$task`" -Force -Xml (Get-Content `"$backup`" -Raw)"
if ($after.Actions.Count -ne 1 -or $after.Actions[0].Execute -ne $pythonw -or $after.Actions[0].Arguments -ne "`"$launcher`"" -or $after.Actions[0].WorkingDirectory -ne $repo) {
    throw "the action did not take - restore from $backup"
}
foreach ($i in 0..2) {
    if ($afterDesc[$i] -ne $before[$i]) {
        throw "the task changed beyond its action ($($before[$i]) -> $($afterDesc[$i])) - restore from $backup"
    }
}
Write-Output "OK: $task now runs the evening routine hidden; triggers, settings and account unchanged."
