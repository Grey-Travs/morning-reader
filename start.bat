@echo off
REM Double-click to launch Morning Reader.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo First-time setup hasn't been run yet. Run setup.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" launch.py
pause
