@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Please run setup_windows.bat first.
    pause
    exit /b 1
)
echo Starting Business Card to Outlook at http://localhost:8501
echo (only reachable from this computer). Close this window to stop the app.
".venv\Scripts\python.exe" -m streamlit run app.py --server.address localhost --server.headless false
pause
