@echo off
rem Scheduled entry point: fetch -> check -> (on draw days) predict.
rem Output is appended to daily.log so it survives the task ending.
setlocal
set "PY=python"
set "SCRIPT=%~dp0lottery.py"
set "LOG=%~dp0daily.log"
echo ===== %DATE% %TIME% ===== >> "%LOG%"
"%PY%" "%SCRIPT%" daily >> "%LOG%" 2>&1
exit /b %errorlevel%
