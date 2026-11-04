@echo off
title Opcare DAQ demo
cd /d "%~dp0"
echo.
echo   Opcare Smart Services - Data Acquisition Platform demo
echo   ------------------------------------------------------
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (where python >nul 2>nul && set "PY=python")
if defined PY %PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul || set "PY="
if not defined PY (
  echo   Python 3.10 or newer is needed. Opening the download page...
  echo   Install it, tick "Add python.exe to PATH", then double-click this file again.
  start "" https://www.python.org/downloads/
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
  echo   First run: preparing a private Python environment ...
  %PY% -m venv .venv || (echo   Could not create the environment. & pause & exit /b 1)
)
echo   Checking packages (first run downloads about 5 MB) ...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements-demo.txt || (echo   Package download failed - check the internet connection. & pause & exit /b 1)
echo   Starting. Your browser will open by itself. Close this window to stop the demo.
".venv\Scripts\python.exe" tools\demo\run_demo.py --open --token opcare-demo %*
if errorlevel 1 (set "RC=1") else (set "RC=0")
if "%~1"=="" pause
exit /b %RC%
