@echo off
setlocal
set "PYTHONUTF8=1"
set "PYTHONDONTWRITEBYTECODE=1"
python "%~dp0app\launch.py" %*
if errorlevel 1 pause
