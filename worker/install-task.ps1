# Registers the AutoJobWorker scheduled task: daily at 02:05 (5 minutes after the server's 02:00 pilot), wakes the PC, runs worker.py --once.
# Run once from the folder that holds worker.py: powershell -ExecutionPolicy Bypass -File .\install-task.ps1
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $dir "worker.py"
$action = New-ScheduledTaskAction -Execute "py" -Argument "-3 `"$script`" --once" -WorkingDirectory $dir
$trigger = New-ScheduledTaskTrigger -Daily -At 02:05
$settings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 6)
# Interactive: the browser is headed and needs the signed-in desktop. Locking the screen is fine, never sign out.
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName "AutoJobWorker" -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force
Write-Host "Registered AutoJobWorker (daily 02:05, WakeToRun). Keep the laptop on AC power."
