@echo off
rem One-time setup: draws the VPA icon and puts a "VPA" shortcut on the
rem Desktop, in the Start menu and in this folder.
rem After this, just double-click "VPA".
cd /d "%~dp0"
set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" tools\make_launcher.py
echo.
pause
