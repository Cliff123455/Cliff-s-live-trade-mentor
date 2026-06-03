@echo off
setlocal enabledelayedexpansion
title TradingView MCP // Setup for Claude Desktop
cd /d "%~dp0"
color 0B

echo.
echo  ==========================================
echo   TRADINGVIEW MCP  ^|  Claude Desktop Setup
echo  ==========================================
echo.
echo  This installs the TradingView MCP server so Claude Desktop
echo  can read your charts and drive TradingView Desktop.
echo.
echo  It clones into:  "%USERPROFILE%\tradingview-mcp"
echo.

:: ── Node.js check ─────────────────────────────────────────────────────────
node --version >nul 2>&1
if errorlevel 1 (
  color 0C
  echo  [ERROR] Node.js not found. Install Node 18+ from https://nodejs.org
  echo          (Pick the "LTS" installer, then re-run this script.^)
  pause
  exit /b 1
)
for /f "tokens=*" %%v in ('node --version 2^>^&1') do echo  [OK] Node %%v

:: ── git check ─────────────────────────────────────────────────────────────
git --version >nul 2>&1
if errorlevel 1 (
  color 0C
  echo  [ERROR] git not found. Install from https://git-scm.com then re-run.
  pause
  exit /b 1
)
echo  [OK] git found

:: ── Clone or update the MCP repo ──────────────────────────────────────────
set "MCP_DIR=%USERPROFILE%\tradingview-mcp"
if exist "%MCP_DIR%\.git" (
  echo  [INFO] Already cloned. Pulling latest...
  git -C "%MCP_DIR%" pull --ff-only
) else (
  echo  [INFO] Cloning tradingview-mcp...
  git clone https://github.com/tradesdontlie/tradingview-mcp.git "%MCP_DIR%"
  if errorlevel 1 (
    color 0C
    echo  [ERROR] Clone failed. Check your internet connection and re-run.
    pause
    exit /b 1
  )
)
echo  [OK] Repo ready at "%MCP_DIR%"

:: ── Install dependencies ──────────────────────────────────────────────────
echo  [INFO] Installing npm dependencies (this can take a minute)...
pushd "%MCP_DIR%"
call npm install
if errorlevel 1 (
  color 0C
  echo  [ERROR] npm install failed. See messages above.
  popd
  pause
  exit /b 1
)
popd
echo  [OK] Dependencies installed

:: ── Print the Claude Desktop config block ─────────────────────────────────
echo.
echo  ============================================
echo   NEXT STEPS  (do these once^)
echo  ============================================
echo.
echo  1^) Open Claude Desktop's config file:
echo        %APPDATA%\Claude\claude_desktop_config.json
echo     (If it doesn't exist: Claude Desktop ^> Settings ^> Developer
echo      ^> Edit Config will create it.^)
echo.
echo  2^) Add this "tradingview" entry inside "mcpServers":
echo.
echo     {
echo       "mcpServers": {
echo         "tradingview": {
echo           "command": "node",
echo           "args": ["%MCP_DIR:\=\\%\\src\\server.js"]
echo         }
echo       }
echo     }
echo.
echo  3^) Start TradingView Desktop in debug mode BEFORE opening Claude:
echo        "%MCP_DIR%\scripts\launch_tv_debug.bat"
echo.
echo  4^) Fully quit and reopen Claude Desktop. The TradingView tools
echo     should appear in the tools menu.
echo.
echo  Full guide: TRADINGVIEW_MCP_SETUP.md in this folder.
echo  ============================================
echo.
pause
