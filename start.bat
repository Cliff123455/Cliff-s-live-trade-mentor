@echo off
setlocal enabledelayedexpansion
title ScalpBot // CliffClaw
cd /d "%~dp0"
color 0A

echo.
echo  ==========================================
echo   SCALPBOT // CLIFFCLAW  ^|  Launcher
echo  ==========================================
echo.

:: ── .env check ────────────────────────────────────────────────────────────
if not exist ".env" (
  color 0C
  echo  [ERROR] .env file not found!
  echo  Copy .env.example to .env and fill in your API keys.
  echo.
  pause
  exit /b 1
)
echo  [OK] .env found

:: ── Python check ──────────────────────────────────────────────────────────
py --version >nul 2>&1
if errorlevel 1 (
  color 0C
  echo  [ERROR] Python not found. Install from https://python.org
  pause
  exit /b 1
)
for /f "tokens=2" %%v in ('py --version 2^>^&1') do echo  [OK] Python %%v

:: ── Docker Desktop ─────────────────────────────────────────────────────────
:: Check if docker CLI is reachable (Docker Desktop must be running)
docker info >nul 2>&1
if not errorlevel 1 goto :docker_ready

:: Docker CLI exists but daemon is not running — try to start Docker Desktop
docker --version >nul 2>&1
if errorlevel 1 (
  echo  [WARN] Docker not installed. Skipping Redis auto-start.
  echo         Install Docker Desktop from https://docker.com or run Redis manually.
  goto :after_redis
)

echo  [INFO] Starting Docker Desktop (this may take up to 60 seconds)...
set "DD_PATH="
if exist "C:\Program Files\Docker\Docker\Docker Desktop.exe" (
  set "DD_PATH=C:\Program Files\Docker\Docker\Docker Desktop.exe"
)
if exist "%LOCALAPPDATA%\Docker\Docker Desktop.exe" (
  set "DD_PATH=%LOCALAPPDATA%\Docker\Docker Desktop.exe"
)
if defined DD_PATH (
  start "" "!DD_PATH!"
) else (
  echo  [WARN] Cannot find Docker Desktop.exe. Start Docker manually then rerun.
  goto :after_redis
)

:: Wait up to 90 seconds for Docker daemon to become responsive
set /a tries=0
:wait_docker
set /a tries+=1
if !tries! gtr 18 (
  echo  [ERROR] Docker did not start in time. Open Docker Desktop manually and rerun.
  pause
  exit /b 1
)
timeout /t 5 /nobreak >nul
docker info >nul 2>&1
if errorlevel 1 (
  echo  [INFO] Waiting for Docker... (!tries!/18^)
  goto :wait_docker
)

:docker_ready
echo  [OK] Docker is running

:: ── Redis ─────────────────────────────────────────────────────────────────
echo  Starting Redis...
docker compose up -d redis >nul 2>&1
if errorlevel 1 (
  docker start scalpbot-redis >nul 2>&1
  if errorlevel 1 (
    docker run -d --name scalpbot-redis -p 6379:6379 redis:7-alpine >nul 2>&1
  )
)
timeout /t 2 /nobreak >nul
echo  [OK] Redis ready

:after_redis

:: ── Virtual environment ───────────────────────────────────────────────────
if not exist "venv\Scripts\activate.bat" (
  echo  Creating venv...
  py -m venv venv
  if errorlevel 1 ( echo  [ERROR] venv failed & pause & exit /b 1 )
)
call venv\Scripts\activate.bat
echo  [OK] venv active

:: ── Dependencies ──────────────────────────────────────────────────────────
echo  Checking dependencies...
pip install -r requirements.txt -q --disable-pip-version-check 2>nul
echo  [OK] Dependencies ready

:: ── Open browser in background (non-blocking) ─────────────────────────────
echo.
echo  ============================================
echo   Dashboard: http://127.0.0.1:5000
echo   Click [Launch Agents] once it opens.
echo   Press Ctrl+C here to stop.
echo  ============================================
echo.

:: Launch browser after 2s delay, completely in background
start "" powershell -WindowStyle Hidden -Command "Start-Sleep 2; Start-Process 'http://127.0.0.1:5000'"

:: ── Start Flask UI ────────────────────────────────────────────────────────
python -c "from ui.app import start_ui; start_ui()"

echo.
echo  ScalpBot stopped.
pause
