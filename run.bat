@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Мониторинг ремонтов
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

if exist ".venv\installed.ok" goto start

echo Первый запуск: создаю окружение Python и устанавливаю зависимости.
echo Нужен интернет, это займёт 1-2 минуты.
if exist ".venv\Scripts\python.exe" goto install
where py >nul 2>nul
if %errorlevel%==0 py -3 -m venv .venv
if exist ".venv\Scripts\python.exe" goto install
where python >nul 2>nul
if %errorlevel%==0 python -m venv .venv
if exist ".venv\Scripts\python.exe" goto install
echo.
echo Не найден Python 3.10 или новее.
echo Установите его с https://www.python.org/downloads/
echo При установке отметьте галочку "Add python.exe to PATH", затем снова запустите run.bat
pause
exit /b 1

:install
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto pipfail
echo ok> ".venv\installed.ok"
goto start

:pipfail
echo.
echo Не удалось установить зависимости. Проверьте подключение к интернету
echo или доступ к pypi.org и запустите run.bat ещё раз.
pause
exit /b 1

:start
".venv\Scripts\python.exe" app.py %*
if errorlevel 1 pause
