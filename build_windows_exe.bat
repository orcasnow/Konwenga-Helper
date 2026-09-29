@echo off
setlocal
cd /d "%~dp0"

set "PLAYWRIGHT_BROWSERS_PATH=%CD%\build_assets\pw-browsers"
set "PLAYWRIGHT_SKIP_BROWSER_GC=1"

echo [1/4] Installing build and application dependencies...
python -m pip install -r requirements-local.txt pyinstaller
if errorlevel 1 goto :failed

echo [2/4] Installing bundled Chromium...
python -m playwright install chromium
if errorlevel 1 goto :failed

echo [3/4] Packaging the one-file Windows executable...
python -m PyInstaller --noconfirm --clean Dankoba_Helper_Local.spec
if errorlevel 1 goto :failed

echo [4/4] Build complete.
echo Output: %CD%\dist\Dankoba_Helper_Local.exe
pause
exit /b 0

:failed
echo Build failed. Review the error above.
pause
exit /b 1
