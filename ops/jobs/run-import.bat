@echo off
chcp 65001 >nul
rem Re-import collector JSON into MySQL only (~3 min).
rem Closing this window or pressing Ctrl+C only ends the watch view - the crawl keeps running.
rem Re-attach later with run-watch.bat.  Job table and timings: ops\menu.ps1 header.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\menu.ps1" -Job import
echo.
pause
