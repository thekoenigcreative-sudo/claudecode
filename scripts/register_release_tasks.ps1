# Point every ASXBot scheduled task at its release shim (scripts\deploy.py, 25 Sep 2026).
#
# Before: each task ran a launcher straight from this checkout on Google Drive, so a build
#         editing files here changed what the next task start loaded.
# After:  C:\venvs\asx-bot\Scripts\pythonw.exe "%LOCALAPPDATA%\asx-bot\bin\<task>.pyw" - a
#         shim that resolves releases\CURRENT and runs that release's scripts\<task>.pyw with
#         ASXBOT_HOME pointing back at this checkout for config.yaml, .env and data\.
#
# Only each task's ACTION changes. Triggers, settings and the account are kept and read
# back; every definition is saved first to data\task_backups\, so any one can be undone:
#     Register-ScheduledTask -TaskName "<name>" -Force -Xml (Get-Content "<backup>" -Raw)
# A task whose launcher is running now (the Chat, or the IB Gateway launcher) keeps running
# the old code until it is next started; its definition is still re-pointed.
#
# Run once, by hand, in a normal (not elevated) PowerShell, AFTER scripts\deploy.py has
# written the shims:
#     powershell -NoProfile -ExecutionPolicy Bypass -File "G:\My Drive\asx-bot\scripts\register_release_tasks.ps1"
$ErrorActionPreference = "Stop"
$repo = "G:\My Drive\asx-bot"
$pythonw = "C:\venvs\asx-bot\Scripts\pythonw.exe"
$bin = Join-Path $env:LOCALAPPDATA "asx-bot\bin"
$backups = "$repo\data\task_backups"
$tasks = @{
    "ASXBot Arena Warmup"          = "arena_warmup"
    "ASXBot Arena Watchdog"        = "arena_watchdog"
    "ASXBot Arena Evening"         = "arena_evening"
    "ASXBot Chat"                  = "chat"
    "ASXBot IB Gateway"            = "ibgateway"
    "ASXBot IB Gateway Supervisor" = "ibgateway_supervisor"
    "ASXBot IBKR Preflight"        = "ibkr_preflight"
    "ASXBot Filter Cost"           = "arena_filter_cost"
}

if (-not (Test-Path $pythonw)) { throw "not found: $pythonw - nothing was changed" }
foreach ($name in $tasks.Values) {
    if (-not (Test-Path (Join-Path $bin "$name.pyw"))) { throw "shim missing: $bin\$name.pyw - run scripts\deploy.py first; nothing was changed" }
}
New-Item -ItemType Directory -Force $backups | Out-Null

function Describe-Task($t) {
    $trig = ($t.Triggers | ForEach-Object { "$($_.CimClass.CimClassName) $($_.StartBoundary) days=$($_.DaysOfWeek) every=$($_.Repetition.Interval) enabled=$($_.Enabled)" }) -join "; "
    $s = $t.Settings
    $set = "limit=$($s.ExecutionTimeLimit) instances=$($s.MultipleInstances) restart=$($s.RestartCount)x$($s.RestartInterval) whenAvailable=$($s.StartWhenAvailable) hidden=$($s.Hidden)"
    $who = "$($t.Principal.UserId) $($t.Principal.LogonType)"
    return @($trig, $set, $who)
}

foreach ($task in $tasks.Keys) {
    $shim = Join-Path $bin "$($tasks[$task]).pyw"
    $current = Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue
    if ($null -eq $current) { Write-Output "SKIP $task (no such task)"; continue }
    $backup = Join-Path $backups ("{0} {1}.xml" -f $task, (Get-Date -Format "yyyy-MM-dd HHmmss"))
    Export-ScheduledTask -TaskName $task | Out-File -Encoding unicode $backup
    $before = Describe-Task $current
    $action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$shim`"" -WorkingDirectory $repo
    Set-ScheduledTask -TaskName $task -Action $action | Out-Null
    $after = Get-ScheduledTask -TaskName $task
    $afterDesc = Describe-Task $after
    if ($after.Actions.Count -ne 1 -or $after.Actions[0].Execute -ne $pythonw -or $after.Actions[0].Arguments -ne "`"$shim`"") {
        throw "$task : the action did not take - restore from $backup"
    }
    foreach ($i in 0..2) {
        if ($afterDesc[$i] -ne $before[$i]) { throw "$task changed beyond its action ($($before[$i]) -> $($afterDesc[$i])) - restore from $backup" }
    }
    Write-Output ("OK {0,-30} -> {1}  (state {2}; backup {3})" -f $task, $shim, $after.State, (Split-Path $backup -Leaf))
}
Write-Output "Every task now runs releases\CURRENT through its shim; triggers, settings and accounts unchanged."
