@echo off
rem Launch with a console window (useful for debugging)
cd /d "%~dp0"
.venv\Scripts\python.exe src\main.py
pause
