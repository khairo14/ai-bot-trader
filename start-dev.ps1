# ─────────────────────────────────────────────────────────────────────────────
# AI Bot Trader — Local Development Startup Script (Windows PowerShell)
#
# Starts: uvicorn · Celery worker · Celery beat · Vite dev server
#
# Prerequisites:
#   - backend\.venv already created  (python -m venv .venv)
#   - frontend\node_modules exists    (npm install)
#   - Docker running for PostgreSQL + Redis:  docker-compose up -d db redis
# ─────────────────────────────────────────────────────────────────────────────

$Root    = Split-Path -Parent $MyInvocation.MyCommand.Path
$Backend = Join-Path $Root "backend"
$Frontend = Join-Path $Root "frontend"
$Python   = Join-Path $Backend ".venv\Scripts\python.exe"
$Celery   = Join-Path $Backend ".venv\Scripts\celery.exe"

Write-Host ""
Write-Host "  AI Bot Trader — Dev Startup" -ForegroundColor Cyan
Write-Host "  ─────────────────────────────" -ForegroundColor DarkGray
Write-Host ""

# ── Verify prerequisites ────────────────────────────────────────────────────
if (-not (Test-Path $Python)) {
    Write-Host "  [ERROR] Python venv not found at $Python" -ForegroundColor Red
    Write-Host "          Run: cd backend; python -m venv .venv; .venv\Scripts\pip install -r requirements.txt"
    exit 1
}
if (-not (Test-Path (Join-Path $Frontend "node_modules"))) {
    Write-Host "  [ERROR] node_modules missing. Run: cd frontend; npm install" -ForegroundColor Red
    exit 1
}

# ── Start services ───────────────────────────────────────────────────────────
Write-Host "  Starting Uvicorn (API)..." -ForegroundColor Green
$uvicorn = Start-Process -PassThru -NoNewWindow -FilePath $Python `
    -ArgumentList "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", "8000", "--reload" `
    -WorkingDirectory $Backend

Write-Host "  Starting Celery Worker..." -ForegroundColor Green
$worker = Start-Process -PassThru -NoNewWindow -FilePath $Celery `
    -ArgumentList "-A", "celery_app", "worker", "--loglevel=info", "--concurrency=2" `
    -WorkingDirectory $Backend

Write-Host "  Starting Celery Beat (scheduler)..." -ForegroundColor Green
$beat = Start-Process -PassThru -NoNewWindow -FilePath $Celery `
    -ArgumentList "-A", "celery_app", "beat", "--loglevel=info" `
    -WorkingDirectory $Backend

Write-Host "  Starting Vite (frontend)..." -ForegroundColor Green
$vite = Start-Process -PassThru -NoNewWindow -FilePath "npm.cmd" `
    -ArgumentList "run", "dev" `
    -WorkingDirectory $Frontend

Write-Host ""
Write-Host "  All services started:" -ForegroundColor Cyan
Write-Host "    Frontend  →  http://localhost:5173" -ForegroundColor White
Write-Host "    API       →  http://localhost:8000" -ForegroundColor White
Write-Host "    API Docs  →  http://localhost:8000/docs" -ForegroundColor White
Write-Host ""
Write-Host "  Press Ctrl+C to stop all services." -ForegroundColor DarkGray
Write-Host ""

# ── Wait and clean up on exit ────────────────────────────────────────────────
try {
    while ($true) { Start-Sleep 5 }
} finally {
    Write-Host ""
    Write-Host "  Stopping all services..." -ForegroundColor Yellow
    @($uvicorn, $worker, $beat, $vite) | ForEach-Object {
        if ($_ -and -not $_.HasExited) {
            Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
        }
    }
    Write-Host "  Done." -ForegroundColor Green
}
