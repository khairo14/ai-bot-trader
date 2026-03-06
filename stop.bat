@echo off
setlocal
title AI Bot Trader — Stopping

echo.
echo   AI Bot Trader — Stopping all services...
echo.

cd /d "%~dp0"
docker compose down

echo.
echo   All services stopped.
echo   Your data is preserved in Docker volumes.
echo.
echo   To start again: run launch.bat
echo.
endlocal
