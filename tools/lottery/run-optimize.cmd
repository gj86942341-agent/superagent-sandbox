@echo off
rem Monthly honest-optimisation report: walk-forward + significance + holdout.
setlocal
set "PY=python"
set "SCRIPT=%~dp0optimize.py"
set "LOG=%~dp0optimize.log"
echo ===== %DATE% %TIME% ===== >> "%LOG%"
"%PY%" "%SCRIPT%" >> "%LOG%" 2>&1
exit /b %errorlevel%
