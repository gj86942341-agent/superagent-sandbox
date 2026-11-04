@echo off
rem Weekly rule-drift watch: crawl the official rule pages and compare them with the
rem constants the system uses. Output appended to rules-watch.log.
setlocal
set "PY=python"
set "SCRIPT=%~dp0crawler.py"
set "LOG=%~dp0rules-watch.log"
echo ===== %DATE% %TIME% ===== >> "%LOG%"
"%PY%" "%SCRIPT%" rules >> "%LOG%" 2>&1
exit /b %errorlevel%
