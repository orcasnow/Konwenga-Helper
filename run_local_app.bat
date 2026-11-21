@echo off
cd /d "%~dp0"

py -3.15 --version >nul 2>&1
if errorlevel 1 goto :python_missing

py -3.15 -m pip install --upgrade -r requirements-local.txt
if errorlevel 1 goto :failed
py -3.15 -m playwright install chromium
if errorlevel 1 goto :failed
py -3.15 Dankoba_Helper_Local.py
if errorlevel 1 goto :failed
exit /b 0

:python_missing
echo Python 3.15 was not found. Install 64-bit Python 3.15 and its py launcher first.
pause
exit /b 1

:failed
echo Setup or application launch failed. Review the error above.
pause
exit /b 1
