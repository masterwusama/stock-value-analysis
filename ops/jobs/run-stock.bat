@echo off
chcp 65001 >nul
rem Daily market snapshot for all A/HK/US names (~2 min).
rem Closing this window or pressing Ctrl+C only ends the watch view - the crawl keeps running.
rem Re-attach later with run-watch.bat.  Job table and timings: ops\menu.ps1 header.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\menu.ps1" -Job stock
echo.
pause
