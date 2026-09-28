# Registers (or re-registers) the Task Scheduler task that runs scheduled_run.ps1 for the current
# user. Two triggers, both deduplicated by scheduled_run.ps1's once-per-day guard:
#   - at logon (5-minute delay), so a run happens every day the computer is started;
#   - daily at 09:00, for days the computer is left on or asleep and never logged into again.
# StartWhenAvailable makes a 09:00 run missed while the computer was off or asleep run as soon as
# it's back. Run from the repo:  powershell -ExecutionPolicy Bypass -File scheduling\register_task.ps1

$ErrorActionPreference = "Stop"
$taskName = "reservoir_ca daily pipeline"
$repo = Split-Path -Parent $PSScriptRoot
$script = Join-Path $PSScriptRoot "scheduled_run.ps1"
# Resolved now, because the task's environment may not have the same PATH as this shell.
$uv = (Get-Command uv).Source
$user = "$env:USERDOMAIN\$env:USERNAME"

$action = New-ScheduledTaskAction -Execute "powershell.exe" -WorkingDirectory $repo `
    -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`" -Uv `"$uv`""
$atLogon = New-ScheduledTaskTrigger -AtLogOn -User $user
$atLogon.Delay = "PT5M"
$daily = New-ScheduledTaskTrigger -Daily -At "09:00"
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 4)
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $atLogon, $daily `
    -Settings $settings -Principal $principal -Force `
    -Description "Daily reservoir_ca run: new imagery, volumes, site rebuild, publish to gh-pages." |
    Out-Null
Write-Output "Registered '$taskName' for $user (uv: $uv)."
