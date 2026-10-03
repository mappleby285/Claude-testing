@echo off
rem Watch a folder without the browser interface. Usage: run_watch.bat "C:\path\to\folder"
cd /d "%~dp0"
if "%~1"=="" (
    echo Usage: run_watch.bat "C:\path\to\folder"
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m cardconv watch "%~1"
pause
