@echo off
title ScalpBot — EMERGENCY STOP
color 0C
echo.
echo  ==========================================
echo   SCALPBOT EMERGENCY STOP
echo  ==========================================
echo.
echo  Killing all agent processes...
taskkill /F /IM python.exe /T >nul 2>&1
taskkill /F /IM py.exe /T >nul 2>&1
echo  [OK] All Python processes killed.
echo.
echo  Stopping Redis...
docker compose stop redis >nul 2>&1
echo  [OK] Redis stopped.
echo.
echo  ScalpBot fully stopped.
echo.
pause
