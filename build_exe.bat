@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Сборка EXE
set PYTHONUTF8=1
if not exist ".venv\installed.ok" (
  echo Сначала один раз запустите run.bat, чтобы создать окружение.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m pip install pyinstaller
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onedir --name RepairMonitor ^
  --add-data "templates;templates" --add-data "static;static" app.py
if errorlevel 1 goto fail
echo.
echo Готово: папка dist\RepairMonitor, запускать RepairMonitor.exe
echo База будет создана в dist\RepairMonitor\data
pause
exit /b 0
:fail
echo Сборка не удалась, подробности выше.
pause
exit /b 1
