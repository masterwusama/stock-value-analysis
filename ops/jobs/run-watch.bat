@echo off
chcp 65001 >nul
rem Attach to whatever crawl is running now, no new job started.
rem Closing this window or pressing Ctrl+C only ends the watch view - the crawl keeps running.
rem Same view you get after menu.bat launches a job - this file starts nothing.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\menu.ps1" -Watch
echo.
pause
