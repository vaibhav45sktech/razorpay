# Is the watcher running, and what has it said lately?
$root = Split-Path -Parent $PSScriptRoot
$pidFile = "$root\logs\watcher.pid"
if ((Test-Path $pidFile) -and (Get-Process -Id (Get-Content $pidFile) -ErrorAction SilentlyContinue)) {
    Write-Host "watcher RUNNING (PID $(Get-Content $pidFile))" -ForegroundColor Green
} else {
    Write-Host "watcher NOT running" -ForegroundColor Yellow
}
if (Test-Path "$root\logs\watcher.log") {
    Write-Host "--- last 15 log lines ---"
    Get-Content "$root\logs\watcher.log" -Tail 15
}
