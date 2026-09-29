@echo off
cd /d "%~dp0"
python -m pip install -r requirements-local.txt
python -m playwright install chromium
python Dankoba_Helper_Local.py
if errorlevel 1 pause
