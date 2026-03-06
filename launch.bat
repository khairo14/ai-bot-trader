@echo off
setlocal
title AI Bot Trader

echo.
echo   AI Bot Trader v2.5.0
echo   -----------------------------------------
echo.

:: Check Docker is running
docker info >nul 2>&1
if errorlevel 1 (
    echo   [ERROR] Docker Desktop is not running.
    echo          Please start Docker Desktop and try again.
    echo.
    pause
    exit /b 1
)

:: First run: create .env from .env.docker
if not exist "%~dp0.env" (
    echo   First run detected - creating .env from .env.docker...
    copy "%~dp0.env.docker" "%~dp0.env" >nul
    echo.
    echo   IMPORTANT - fill in your API keys before continuing.
    echo.
    echo   Required:
    echo     BINANCE_API_KEY / BINANCE_API_SECRET
    echo     ALPACA_API_KEY  / ALPACA_API_SECRET
    echo     POSTGRES_PASSWORD  (change from default)
    echo     SECRET_KEY         (change from default)
    echo.
    echo   .env has been opened in Notepad.
    echo   Save it, then re-run launch.bat.
    echo.
    start notepad "%~dp0.env"
    pause
    exit /b 0
)

:: Start the full stack
cd /d "%~dp0"

echo   Building images and starting services...
echo   (This may take a few minutes on first run.)
echo.
docker compose up -d --build

if errorlevel 1 (
    echo.
    echo   [ERROR] Docker Compose failed. Check output above.
    pause
    exit /b 1
)

:: Wait for backend to be healthy
echo.
echo   Waiting for backend to be ready (up to 3 minutes)...
set /a TRIES=0
:WAIT_LOOP
  set /a TRIES+=1
  if %TRIES% gtr 36 (
    echo.
    echo   [WARN] Backend not ready after 3 min -- opening anyway.
    goto OPEN_BROWSER
  )
  curl -sf http://localhost:8000/health >nul 2>&1
  if not errorlevel 1 goto OPEN_BROWSER
  <nul set /p=.
  timeout /t 5 /nobreak >nul
  goto WAIT_LOOP

:OPEN_BROWSER
echo.
echo   Opening app in browser...
start http://localhost:3000

echo.
echo   AI Bot Trader is running!
echo.
echo   App:  http://localhost:3000
echo   API:  http://localhost:8000
echo.
echo   To stop:  run stop.bat
echo   Logs:     docker compose logs -f
echo.
endlocal
