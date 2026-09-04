# Start the CampusPool passive watcher as a hidden background process.
#
#   .\scripts\start_watcher.ps1            # start (idempotent: no-op if already running)
#   .\scripts\start_watcher.ps1 -Model qwen2.5:1.5b-instruct -PollSeconds 15
#
# The process is detached from this terminal: closing the window, running
# other commands, or logging out of the shell does not stop it. Stop it with
# .\scripts\stop_watcher.ps1; check it with .\scripts\watcher_status.ps1.
# Logs: logs\watcher.log   PID file: logs\watcher.pid
param(
    [string]$Model = $(if ($env:WATCHER_MODEL) { $env:WATCHER_MODEL } else { "qwen2.5:1.5b-instruct" }),
    [double]$PollSeconds = $(if ($env:WATCHER_POLL_SECONDS) { [double]$env:WATCHER_POLL_SECONDS } else { 15 })
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
New-Item -ItemType Directory -Force -Path "$root\logs" | Out-Null
$pidFile = "$root\logs\watcher.pid"

# 1. Already running?
if (Test-Path $pidFile) {
    $old = Get-Content $pidFile -ErrorAction SilentlyContinue
    if ($old -and (Get-Process -Id $old -ErrorAction SilentlyContinue)) {
        Write-Host "watcher already running (PID $old). Use .\scripts\stop_watcher.ps1 first." -ForegroundColor Yellow
        exit 0
    }
    Remove-Item $pidFile -Force
}

# 2. Python from the project venv
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "No .venv found at $root\.venv - create it and pip install -r requirements.txt first." }

# 3. Ollama reachable, and the small model present (pull once if not)
try { $null = Invoke-RestMethod -Uri "http://localhost:11434/api/tags" -TimeoutSec 5 }
catch { Write-Host "Ollama is not reachable on :11434 - the watcher will still run and use templated text until it is." -ForegroundColor Yellow }
$have = (& ollama list 2>$null) -match [regex]::Escape($Model)
if (-not $have) {
    Write-Host "Pulling $Model (one-time, ~1 GB)..." -ForegroundColor Cyan
    & ollama pull $Model
}

# 4. Launch detached + hidden, with its own log
$env:WATCHER_MODEL = $Model
$env:WATCHER_POLL_SECONDS = "$PollSeconds"
$log = "$root\logs\watcher.log"
$p = Start-Process -FilePath $python -ArgumentList "-m", "backend.watcher" `
        -WorkingDirectory $root -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $log -RedirectStandardError "$root\logs\watcher.err.log"
Set-Content -Path $pidFile -Value $p.Id
Start-Sleep -Seconds 2
if (Get-Process -Id $p.Id -ErrorAction SilentlyContinue) {
    Write-Host "watcher started (PID $($p.Id)), model=$Model, poll=${PollSeconds}s" -ForegroundColor Green
    Write-Host "  log:    $log"
    Write-Host "  status: .\scripts\watcher_status.ps1    stop: .\scripts\stop_watcher.ps1"
} else {
    Write-Host "watcher exited immediately - see logs\watcher.err.log" -ForegroundColor Red
    Get-Content "$root\logs\watcher.err.log" -Tail 20
    exit 1
}
