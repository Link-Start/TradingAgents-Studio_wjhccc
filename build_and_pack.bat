@echo off
REM 一键构建前端 + 打部署包 deploy.tar.gz (Windows 版)
REM 用法: 双击,或在 PowerShell/cmd 里执行  .\build_and_pack.bat
setlocal enableextensions
cd /d "%~dp0"

echo [1/3] Installing frontend deps (npm install) ...
pushd web\frontend
call npm install
if errorlevel 1 ( echo [ERROR] npm install failed & popd & exit /b 1 )

echo [2/3] Building frontend (vite build) ...
call npx vite build
if errorlevel 1 ( echo [ERROR] vite build failed & popd & exit /b 1 )
popd

if not exist "web\frontend\dist\index.html" (
  echo [ERROR] build output missing: web\frontend\dist\index.html
  exit /b 1
)

echo [3/3] Packing deploy.tar.gz ...
if exist deploy.tar.gz del /f /q deploy.tar.gz
tar -czf deploy.tar.gz --exclude=*__pycache__* --exclude=*.pyc --exclude=*.log --exclude=web/frontend/node_modules tradingagents cli web pyproject.toml README.md .env uv.lock
if errorlevel 1 ( echo [ERROR] tar failed & exit /b 1 )

echo.
echo === DONE ===
dir deploy.tar.gz | findstr deploy.tar.gz
echo Location: %cd%\deploy.tar.gz
endlocal
