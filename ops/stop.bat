@echo off
chcp 65001 >nul
rem One-click stop: scheduler + API + leftover collector jobs
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop.ps1" %*
echo.
pause
