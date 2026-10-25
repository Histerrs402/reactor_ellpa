@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
if not exist ".venv\Scripts\python.exe" (
  echo Сначала запустите run.bat
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m unittest discover -s tests -v
pause
