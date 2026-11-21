@echo off
setlocal
cd /d "%~dp0"

py -3.15 --version >nul 2>&1
if errorlevel 1 goto :python_missing

set "PLAYWRIGHT_BROWSERS_PATH=%CD%\build_assets\pw-browsers"
set "PLAYWRIGHT_SKIP_BROWSER_GC=1"

echo [1/4] Installing build and application dependencies...
py -3.15 -m pip install --upgrade -r requirements-local.txt "pyinstaller>=6.16"
if errorlevel 1 goto :failed

echo [2/4] Installing bundled Chromium...
py -3.15 -m playwright install chromium
if errorlevel 1 goto :failed

echo [3/4] Packaging the one-file Windows executable...
py -3.15 -m PyInstaller --noconfirm --clean Dankoba_Helper_Local.spec
if errorlevel 1 goto :failed

echo [4/4] Build complete.
echo Output: %CD%\dist\Dankoba_Helper_Local.exe
pause
exit /b 0

:python_missing
echo Python 3.15 was not found. Install 64-bit Python 3.15 and its py launcher first.
pause
exit /b 1

:failed
echo Build failed. Review the error above.
pause
exit /b 1
