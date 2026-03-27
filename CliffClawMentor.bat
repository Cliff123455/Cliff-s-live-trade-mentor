@echo off
:: This wrapper ensures the terminal window ALWAYS stays open.
:: It re-launches itself inside "cmd /k" so the window never closes.
if not defined CLIFFCLAW_INNER (
  set "CLIFFCLAW_INNER=1"
  cmd /k "%~f0" %*
  exit /b
)

setlocal enabledelayedexpansion
title CliffClaw Mentor
cd /d "%~dp0"
color 0B

echo.
echo  ==========================================
echo   CLIFFCLAW MENTOR  ^|  Dashboard + AI Chat
echo  ==========================================
echo.

:: ── .env check ────────────────────────────────────────────────────────────
if not exist ".env" (
  color 0C
  echo  [ERROR] .env file not found!
  echo  Copy .env.example to .env and fill in your API keys.
  goto :done
)
echo  [OK] .env found

:: ── Python check ──────────────────────────────────────────────────────────
py --version >nul 2>&1
if errorlevel 1 (
  color 0C
  echo  [ERROR] Python not found. Install from https://python.org
  goto :done
)
for /f "tokens=2" %%v in ('py --version 2^>^&1') do echo  [OK] Python %%v

:: ── Docker Desktop ─────────────────────────────────────────────────────────
docker info >nul 2>&1
if not errorlevel 1 goto :docker_ready

docker --version >nul 2>&1
if errorlevel 1 (
  echo  [WARN] Docker not installed. Skipping Redis auto-start.
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

set /a tries=0
:wait_docker
set /a tries+=1
if !tries! gtr 18 (
  echo  [ERROR] Docker did not start in time.
  goto :done
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

:: ── Kill any stale Flask/agent processes on port 5000 ─────────────────────
echo  Cleaning up stale processes...
for /f "tokens=5" %%p in ('netstat -aon 2^>nul ^| findstr ":8501.*LISTENING"') do (
  taskkill /F /PID %%p >nul 2>&1
)
echo  [OK] Port 8501 clear

:: ── Virtual environment ───────────────────────────────────────────────────
if not exist "venv\Scripts\activate.bat" (
  echo  Creating venv...
  py -m venv venv
  if errorlevel 1 ( echo  [ERROR] venv creation failed & goto :done )
)
call venv\Scripts\activate.bat
echo  [OK] venv active

:: ── Dependencies ──────────────────────────────────────────────────────────
echo  Installing/checking dependencies...
pip install -r requirements.txt -q --disable-pip-version-check
echo  [OK] Dependencies step done

:: ── Quick import test ────────────────────────────────────────────────────
echo.
echo  Testing imports...
python -c "from ui.app import app; print('  [OK] All imports passed')"
if errorlevel 1 (
  echo.
  color 0C
  echo  ============================================
  echo   IMPORT FAILED — see error above
  echo  ============================================
  echo.
  echo  Try: pip install -r requirements.txt
  goto :done
)

:: ── Launch ────────────────────────────────────────────────────────────────
echo.
echo  ============================================
echo   Dashboard: http://127.0.0.1:8501
echo   Mentor chat + Live/Backtest toggle included
echo   Click [Launch Agents] once it opens.
echo   Press Ctrl+C here to stop.
echo  ============================================
echo.

:: Open browser after 2s (in background)
start "" powershell -WindowStyle Hidden -Command "Start-Sleep 2; Start-Process 'http://127.0.0.1:8501'"

:: ── Start Flask UI ────────────────────────────────────────────────────────
python -c "from ui.app import start_ui; start_ui()"

echo.
if errorlevel 1 (
  color 0C
  echo  Flask crashed! Check the error above.
) else (
  echo  CliffClaw Mentor stopped.
)

:done
echo.
echo  ── Window will stay open. Type EXIT to close. ──
