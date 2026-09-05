@echo off
chcp 65001 >/dev/null
rem Manual collector run: daily round = stock + agro (pass a job name to run only one)
rem   collect.bat            daily round, foreground
rem   collect.bat agro       one job
rem   collect.bat deep -Background
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0collect.ps1" %*
echo.
pause
