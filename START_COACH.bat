@echo off
setlocal
cd /d "%~dp0"

echo ========================================
echo     Personal Chess Coach v0.8
echo ========================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo Creating Python environment...
    py -3.12 -m venv .venv
    if errorlevel 1 (
        echo.
        echo ERROR: Could not create Python environment.
        pause
        exit /b 1
    )
)

echo Checking dependencies...
".venv\Scripts\python.exe" -c "import flask, chess, requests" >nul 2>&1

if errorlevel 1 (
    echo Installing required packages...
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo ERROR: Could not install required packages.
        pause
        exit /b 1
    )
)

echo.
echo Starting Personal Chess Coach...
echo Open http://127.0.0.1:5000 in your browser.
echo Keep this window open while using the coach.
echo Use the Shut Down Coach button when finished.
echo.

start "" "http://127.0.0.1:5000"
".venv\Scripts\python.exe" app.py

echo.
echo Personal Chess Coach has stopped.
pause
