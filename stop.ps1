# ─────────────────────────────────────────────────────────────────────────────
# AI Bot Trader — Stop all Docker services
# ─────────────────────────────────────────────────────────────────────────────
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

Write-Host ""
Write-Host "  AI Bot Trader — Stopping all services..." -ForegroundColor Yellow
Write-Host ""

docker compose down

Write-Host ""
Write-Host "  All services stopped." -ForegroundColor Green
Write-Host "  Your data is preserved in Docker volumes."
Write-Host ""
Write-Host "  To start again: .\launch.ps1"
Write-Host ""
