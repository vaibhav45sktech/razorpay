# OPTIONAL: make the watcher start automatically every time you log in.
#   .\scripts\install_watcher_autostart.ps1        # install
#   .\scripts\install_watcher_autostart.ps1 -Remove
param([switch]$Remove)
$root = Split-Path -Parent $PSScriptRoot
$task = "CampusPoolWatcher"
if ($Remove) {
    Unregister-ScheduledTask -TaskName $task -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "removed scheduled task $task"; exit 0
}
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$root\scripts\start_watcher.ps1`""
$trigger = New-ScheduledTaskTrigger -AtLogOn
Register-ScheduledTask -TaskName $task -Action $action -Trigger $trigger -Force | Out-Null
Write-Host "installed: '$task' will start the watcher at every login." -ForegroundColor Green
