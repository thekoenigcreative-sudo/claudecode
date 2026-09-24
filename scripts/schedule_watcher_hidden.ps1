# Re-point the "ASXBot Arena Warmup" scheduled task at the hidden launcher.
#
# Before: powershell.exe -File scripts\arena_warmup.ps1 - a visible console window. On
#         23 Sep 2026 that window was closed at 13:32 and the watcher died with it
#         (Last Result 0xC000013A, console closed / Ctrl+C).
# After:  C:\venvs\asx-bot\Scripts\pythonw.exe scripts\arena_warmup.pyw - no window at all.
#         Same command (asxbot arena watch --until auto), same repo folder, same log
#         (arena_warmup.log and asxbot.log - since 24 Sep 2026 in the local folder
#         %LOCALAPPDATA%\asx-bot\logs, not on Google Drive; copied to data\logs each evening).
#
# Only the action changes. The triggers (07:30 Mon-Fri), the settings (13 h limit, one
# instance, start when available) and the account it runs as are kept exactly as they are.
# The task's current definition is saved first, so this can be undone:
#     Register-ScheduledTask -TaskName "ASXBot Arena Warmup" -Force `
#         -Xml (Get-Content "<the backup file this prints>" -Raw)
#
# Run once, by hand, in a normal (not elevated) PowerShell, after Rick agrees:
#     powershell -NoProfile -ExecutionPolicy Bypass -File "G:\My Drive\asx-bot\scripts\schedule_watcher_hidden.ps1"
# It does not start the watcher. The 07:30 trigger does that.
$ErrorActionPreference = "Stop"
$task = "ASXBot Arena Warmup"
$repo = "G:\My Drive\asx-bot"
$pythonw = "C:\venvs\asx-bot\Scripts\pythonw.exe"
$launcher = "$repo\scripts\arena_warmup.pyw"
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

$triggersBefore = ($current.Triggers | ForEach-Object { "$($_.StartBoundary) days=$($_.DaysOfWeek) enabled=$($_.Enabled)" }) -join "; "

$action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$launcher`"" -WorkingDirectory $repo
Set-ScheduledTask -TaskName $task -Action $action | Out-Null

# Read it back: the artefact, not this script's account of it.
$after = Get-ScheduledTask -TaskName $task
$triggersAfter = ($after.Triggers | ForEach-Object { "$($_.StartBoundary) days=$($_.DaysOfWeek) enabled=$($_.Enabled)" }) -join "; "
Write-Output "action now:   $($after.Actions[0].Execute) $($after.Actions[0].Arguments)"
Write-Output "working dir:  $($after.Actions[0].WorkingDirectory)"
Write-Output "triggers:     $triggersAfter"
Write-Output "time limit:   $($after.Settings.ExecutionTimeLimit); instances: $($after.Settings.MultipleInstances)"
Write-Output "next run:     $((Get-ScheduledTaskInfo -TaskName $task).NextRunTime)"
if ($after.Actions[0].Execute -ne $pythonw -or $after.Actions[0].Arguments -ne "`"$launcher`"") {
    throw "the action did not take - restore from $backup"
}
if ($triggersAfter -ne $triggersBefore) {
    throw "the triggers changed ($triggersBefore -> $triggersAfter) - restore from $backup"
}
Write-Output "OK: $task now starts the watcher hidden; triggers unchanged."
