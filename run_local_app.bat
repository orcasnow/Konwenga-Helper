@echo off
cd /d "%~dp0"
python Dankoba_Helper_Local.py
if errorlevel 1 pause
