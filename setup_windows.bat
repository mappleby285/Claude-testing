@echo off
setlocal
cd /d "%~dp0"
echo ============================================================
echo  Business Card to Outlook - one-time setup
echo ============================================================
echo.

rem --- Find Python 3.10 or newer --------------------------------
set "PY="
where py >nul 2>nul && (py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul && set "PY=py -3")
if not defined PY (
    where python >nul 2>nul && (python -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul && set "PY=python")
)
if not defined PY (
    echo [ERROR] Python 3.10 or newer was not found.
    echo Install it from https://www.python.org/downloads/windows/
    echo and tick "Add python.exe to PATH" during installation. Then run this file again.
    pause
    exit /b 1
)
echo Using: %PY%
%PY% --version

rem --- Virtual environment --------------------------------------
if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment in .venv ...
    %PY% -m venv .venv || goto :fail
)
echo Installing packages (this can take a few minutes) ...
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul || goto :fail
".venv\Scripts\python.exe" -m pip install -r requirements-dev.txt || goto :fail

rem --- .env file ------------------------------------------------
if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo.
    echo Created .env - Notepad will open. Paste your Anthropic API key after
    echo ANTHROPIC_API_KEY=   then save and close Notepad.
    notepad ".env"
)

rem --- Self-test (no API calls; uses mocks) ---------------------
echo.
echo Running automated tests ...
".venv\Scripts\python.exe" -m pytest -q
if errorlevel 1 (
    echo [WARNING] Some tests failed. The app may still work; see the output above.
) else (
    echo All tests passed.
)

echo.
echo Setup complete. Double-click run_app.bat to start.
pause
exit /b 0

:fail
echo.
echo [ERROR] Setup failed. Check your internet connection and the messages above.
pause
exit /b 1
