@echo off
rem One-time setup: creates the "Vehicle Import" shortcut with its icon.
rem After this, just double-click "Vehicle Import" in this folder.
cd /d "%~dp0"
set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" tools\make_launcher.py
echo.
pause
