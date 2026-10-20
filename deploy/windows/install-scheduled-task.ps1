# Install the pipeline as a logon-triggered Windows Scheduled Task, so the API
# and its nightly job survive console close and come back after a restart.
# Same durability pattern as study-hall's installer: a task owned by the Task
# Scheduler service has no console to be signalled through, restarts on failure,
# and starts again at logon.
#
#   powershell -ExecutionPolicy Bypass -File deploy\windows\install-scheduled-task.ps1
#   powershell -ExecutionPolicy Bypass -File deploy\windows\install-scheduled-task.ps1 -Uninstall
#
# Runs as the logged-in USER (not SYSTEM), so run-local.ps1 can read the user's
# FRED_API_KEY / EDGAR_USER_AGENT environment variables.
param(
    [switch]$Uninstall,
    [string]$DataDir = (Join-Path $env:USERPROFILE '.stock-data-pipeline\data'),
    [int]$Port = 8000,
    [string]$TaskName = 'StockDataPipeline'
)

$ErrorActionPreference = 'Stop'
$runner = Join-Path $PSScriptRoot 'run-local.ps1'

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task '$TaskName'."
    } else {
        Write-Host "No scheduled task named '$TaskName'."
    }
    return
}

if (-not (Test-Path $runner)) { throw "Runner not found: $runner" }

$argument = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$runner`" -DataDir `"$DataDir`" -Port $Port"
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $argument -WorkingDirectory $PSScriptRoot
$logon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
# Watchdog: also fire every 15 minutes, indefinitely. While the runner is alive
# MultipleInstances IgnoreNew makes these no-ops; if the runner itself was
# killed (2026-10-17: ended with 0xC000013A and nothing started it again until
# a manual start), the next tick brings it back within 15 minutes.
$watchdog = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 15)
# Wake for the nightly run (#63): on 2026-10-19 the PC slept on idle from 19:35
# to 22:08 and the 22:00 run never began. A daily trigger 5 minutes before the
# hour, with WakeToRun below, wakes the machine; IgnoreNew makes the launch a
# no-op while the runner is already up. The hour follows PIPELINE_HOUR (user
# env, default 22 -- the same default run-local.ps1 uses).
$hour = [Environment]::GetEnvironmentVariable('PIPELINE_HOUR', 'User')
if (-not $hour) { $hour = 22 }
$wake = New-ScheduledTaskTrigger -Daily -At ([datetime]::Today.AddHours([int]$hour).AddMinutes(-5))
$trigger = @($logon, $watchdog, $wake)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

# No execution time limit (the 3-day default would kill a long-lived API);
# IgnoreNew stops a second copy stacking on the same port.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -WakeToRun `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings `
    -Description 'Stock-Data-Pipeline API + nightly job on this PC (127.0.0.1). Survives logout; restarts on failure; 15-minute watchdog; wakes the PC before the nightly run.' | Out-Null

Write-Host "Installed scheduled task '$TaskName'."
Write-Host "  runs:      powershell.exe -File $runner"
Write-Host "  data:      $DataDir"
Write-Host "  api:       http://127.0.0.1:$Port"
Write-Host "  log:       $(Join-Path (Split-Path $DataDir -Parent) 'logs\api.log')"
Write-Host ""
Write-Host "Start it now:  Start-ScheduledTask -TaskName $TaskName"
Write-Host "Check it:      python smoke_deploy.py http://127.0.0.1:$Port --allow-empty"
Write-Host "Remove it:     powershell -ExecutionPolicy Bypass -File $PSCommandPath -Uninstall"
