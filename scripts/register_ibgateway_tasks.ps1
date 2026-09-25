# Create (or re-create) the three IB Gateway tasks. Rick asked for them on 25 Sep 2026.
#
#   "ASXBot IB Gateway"             scripts\ibgateway.pyw: IB Gateway through IBC, kept
#                                    running (no time limit, one instance). No trigger of its
#                                    own: the supervisor starts it with schtasks /run.
#   "ASXBot IB Gateway Supervisor"  scripts\ibgateway_supervisor.pyw: one check, at logon
#                                    and every 2 minutes, all day, every day.
#   "ASXBot IBKR Preflight"         scripts\ibkr_preflight.pyw: `asxbot ibkr check` at 07:20
#                                    Monday-Friday; a Trader chat message only if it fails.
#
# Each task runs its release shim, %LOCALAPPDATA%\asx-bot\bin\<task>.pyw, which runs that
# script from the deployed release (releases\CURRENT), never this checkout (docs\releases.md;
# 26 Sep 2026: until then this script pointed the tasks at the checkout). Run
# scripts\deploy.py first: it writes the shims.
#
# 26 Sep 2026, as on the live tasks: the working folder is %LOCALAPPDATA%\asx-bot, so a task
# can start while Google Drive is not mounted; and every trigger time is stripped of its UTC
# offset. New-ScheduledTaskTrigger writes today's offset (+10:00), and Task Scheduler then
# fires at that fixed UTC instant - from 5 Oct (daylight saving) 07:20 would come at 08:20.
# Without the offset the time is local and follows daylight saving.
#
# All three run as Rick, in his session (Gateway's window belongs on his desktop), with no
# console (pythonw). Logs:
# %LOCALAPPDATA%\asx-bot\logs\ibgateway.log, ibgateway_supervisor.log, ibkr_preflight.log.
#
# Run in a normal (not elevated) PowerShell:
#     powershell -NoProfile -ExecutionPolicy Bypass -File "G:\My Drive\asx-bot\scripts\register_ibgateway_tasks.ps1"
# Remove them again with:
#     "ASXBot IB Gateway Supervisor","ASXBot IBKR Preflight","ASXBot IB Gateway" | % { Unregister-ScheduledTask -TaskName $_ -Confirm:$false }
$ErrorActionPreference = "Stop"
$pythonw = "C:\venvs\asx-bot\Scripts\pythonw.exe"
$work = Join-Path $env:LOCALAPPDATA "asx-bot"         # working folder: a local disk, not Drive
$bin = Join-Path $env:LOCALAPPDATA "asx-bot\bin"   # the release shims (scripts\deploy.py)
$launcher = "$bin\ibgateway.pyw"
$supervisor = "$bin\ibgateway_supervisor.pyw"
$preflight = "$bin\ibkr_preflight.pyw"
$offset = '([+-]\d\d:\d\d|Z)$'   # a UTC offset at the end of a trigger's StartBoundary

foreach ($p in @($pythonw, $work, $launcher, $supervisor, $preflight, "C:\IBC\IBC.jar")) {
    if (-not (Test-Path $p)) { throw "not found: $p - run scripts\deploy.py first if it is a shim; nothing was created" }
}
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

function Replace-Task($name, $action, $trigger, $settings, $description) {
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
    }
    if ($null -eq $trigger) {
        Register-ScheduledTask -TaskName $name -Action $action -Principal $principal `
            -Settings $settings -Description $description | Out-Null
    } else {
        Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Principal $principal `
            -Settings $settings -Description $description | Out-Null
    }
}

# 1. The launcher: long-running, never stopped by a time limit, one at a time.
#    Replacing this task while its launcher runs would stop Gateway, so it is left alone if
#    it is running (re-run this script when Gateway is down to change it).
$lt = Get-ScheduledTask -TaskName "ASXBot IB Gateway" -ErrorAction SilentlyContinue
if ($lt -and $lt.State -eq "Running") {
    Write-Output "ASXBot IB Gateway is running (Gateway is up through it): left as it is."
} else {
    Replace-Task "ASXBot IB Gateway" `
        (New-ScheduledTaskAction -Execute $pythonw -Argument "`"$launcher`"" -WorkingDirectory $work) `
        $null `
        (New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -Hidden -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)) `
        "IB Gateway through IBC (live, read-only API). Started by the ASXBot IB Gateway Supervisor task; never places an order."
}

# 2. The supervisor: at logon (after 1 minute) and every 2 minutes from now on.
$atLogon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$atLogon.Delay = "PT1M"
$every2 = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 2)
$every2.StartBoundary = $every2.StartBoundary -replace $offset, ''   # local time (see top)
Replace-Task "ASXBot IB Gateway Supervisor" `
    (New-ScheduledTaskAction -Execute $pythonw -Argument "`"$supervisor`"" -WorkingDirectory $work) `
    @($atLogon, $every2) `
    (New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -Hidden -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 4)) `
    "Every 2 minutes: is IB Gateway up and linked to IBKR? If not, restarts it through IBC and tells Rick on the Trader chat if his phone approval is needed."

# 3. The pre-flight: 07:20 on weekdays (it skips ASX holidays itself).
$weekdays0720 = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At "07:20"
$weekdays0720.StartBoundary = $weekdays0720.StartBoundary -replace $offset, ''   # local time (see top)
Replace-Task "ASXBot IBKR Preflight" `
    (New-ScheduledTaskAction -Execute $pythonw -Argument "`"$preflight`"" -WorkingDirectory $work) `
    $weekdays0720 `
    (New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -Hidden -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 5)) `
    "07:20 weekdays: asxbot ibkr check; messages Rick on the Trader chat only if it fails."

# Read them back.
foreach ($name in @("ASXBot IB Gateway", "ASXBot IB Gateway Supervisor", "ASXBot IBKR Preflight")) {
    $t = Get-ScheduledTask -TaskName $name
    $s = $t.Settings
    Write-Output "$name"
    Write-Output "  action:   $($t.Actions[0].Execute) $($t.Actions[0].Arguments) (in $($t.Actions[0].WorkingDirectory))"
    foreach ($tr in $t.Triggers) {
        Write-Output "  trigger:  $($tr.CimClass.CimClassName) start=$($tr.StartBoundary) delay=$($tr.Delay) every=$($tr.Repetition.Interval) for=$($tr.Repetition.Duration) days=$($tr.DaysOfWeek)"
        if ($tr.StartBoundary -match $offset) { throw "$name : a trigger still carries a UTC offset ($($tr.StartBoundary)) - check it" }
    }
    Write-Output "  runs as:  $($t.Principal.UserId) ($($t.Principal.LogonType)); hidden=$($s.Hidden) instances=$($s.MultipleInstances) limit=$($s.ExecutionTimeLimit) state=$($t.State)"
}
$sv = Get-ScheduledTask -TaskName "ASXBot IB Gateway Supervisor"
if ($sv.Triggers.Count -ne 2 -or $sv.Triggers[1].Repetition.Interval -ne "PT2M") {
    throw "the supervisor's triggers are not what was asked for - check them"
}
Write-Output "OK: the IB Gateway tasks are in place."
