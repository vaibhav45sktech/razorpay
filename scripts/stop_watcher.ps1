# Stop the passive watcher started by start_watcher.ps1.
$root = Split-Path -Parent $PSScriptRoot
$pidFile = "$root\logs\watcher.pid"
if (-not (Test-Path $pidFile)) { Write-Host "watcher is not running (no PID file)."; exit 0 }
$id = Get-Content $pidFile
$proc = Get-Process -Id $id -ErrorAction SilentlyContinue
if ($proc) { Stop-Process -Id $id -Force; Write-Host "watcher stopped (PID $id)." -ForegroundColor Green }
else { Write-Host "watcher (PID $id) was not running; cleaning up." }
Remove-Item $pidFile -Force
