@echo off
chcp 65001 >nul
rem One-click start: FastAPI service + collector scheduler
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
echo.
pause
