# AI Bot Trader v2.5.0 - Docker Bundle Launcher (PowerShell)

# Resolve script root reliably whether run directly or via launch.bat
$Root = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
if (-not $Root) { $Root = (Get-Location).Path }

Set-Location $Root

Write-Host ""
Write-Host "  AI Bot Trader v2.5.0" -ForegroundColor Cyan
Write-Host "  -----------------------------------------" -ForegroundColor DarkGray
Write-Host ""

# Check Docker is running
try {
    docker info *>&1 | Out-Null
} catch { }
if ($LASTEXITCODE -ne 0) {
    Write-Host "  [ERROR] Docker Desktop is not running." -ForegroundColor Red
    Write-Host "          Please start Docker Desktop and try again."
    Read-Host "Press Enter to exit"
    exit 1
}

# First run: create .env from .env.docker
$envFile   = Join-Path $Root ".env"
$envDocker = Join-Path $Root ".env.docker"

if (-not (Test-Path $envFile)) {
    Write-Host "  First run detected - creating .env from .env.docker..." -ForegroundColor Yellow
    Copy-Item $envDocker $envFile
    Write-Host ""
    Write-Host "  IMPORTANT - fill in your API keys before continuing." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  Required:" -ForegroundColor Yellow
    Write-Host "    BINANCE_API_KEY / BINANCE_API_SECRET" -ForegroundColor Yellow
    Write-Host "    ALPACA_API_KEY  / ALPACA_API_SECRET" -ForegroundColor Yellow
    Write-Host "    POSTGRES_PASSWORD  (change from default)" -ForegroundColor Yellow
    Write-Host "    SECRET_KEY         (change from default)" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  .env has been opened in Notepad. Save it, then re-run launch.bat." -ForegroundColor Yellow
    Write-Host ""
    Start-Process notepad $envFile
    Read-Host "Press Enter to exit"
    exit 0
}

# Start the full stack
Write-Host "  Building images and starting services..." -ForegroundColor Green
Write-Host "  (This may take a few minutes on first run.)" -ForegroundColor DarkGray
Write-Host ""

docker compose up -d --build

if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "  [ERROR] Docker Compose failed. Check output above." -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}

# Wait for backend to be healthy (poll /health every 5s, max 3 min)
Write-Host ""
Write-Host "  Waiting for backend to be ready (up to 3 minutes)..." -ForegroundColor DarkGray
$tries = 0
$ready = $false
while ($tries -lt 36) {
    $tries++
    try {
        $r = Invoke-WebRequest -Uri "http://localhost:8000/health" -UseBasicParsing -TimeoutSec 3 -ErrorAction Stop
        if ($r.StatusCode -eq 200) { $ready = $true; break }
    } catch { }
    Write-Host -NoNewline "."
    Start-Sleep -Seconds 5
}
Write-Host ""
if (-not $ready) {
    Write-Host "  [WARN] Backend not ready after 3 min - opening anyway." -ForegroundColor Yellow
}

# Open app in browser
Write-Host "  Opening app in browser..." -ForegroundColor Green
Start-Process "http://localhost:3000"

Write-Host ""
Write-Host "  AI Bot Trader is running!" -ForegroundColor Cyan
Write-Host ""
Write-Host "  App:  http://localhost:3000" -ForegroundColor Cyan
Write-Host "  API:  http://localhost:8000" -ForegroundColor Cyan
Write-Host ""
Write-Host "  To stop:  .\stop.ps1  or  stop.bat" -ForegroundColor DarkGray
Write-Host "  Logs:     docker compose logs -f" -ForegroundColor DarkGray
Write-Host ""
