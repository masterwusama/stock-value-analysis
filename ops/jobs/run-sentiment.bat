@echo off
chcp 65001 >nul
rem Post-close sentiment daily report (state machine / sector effect / candidate pool).
rem Closing this window or pressing Ctrl+C only ends the watch view - the crawl keeps running.
rem Re-attach later with run-watch.bat.  Job table and timings: ops\menu.ps1 header.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\menu.ps1" -Job sentiment
echo.
pause
