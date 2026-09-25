@echo off
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo Virtual environment not found.
    echo Create it first with:
    echo python -m venv .venv
    echo.
    pause
    exit /b 1
)

echo Starting Church Parking Violation Tracker v1.0...
".venv\Scripts\python.exe" -m streamlit run app.py --server.address 0.0.0.0
pause
