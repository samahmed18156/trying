@echo off
REM ==========================================================================
REM  run.bat - one-shot setup + scan for Windows
REM
REM    Double-click it, or from a terminal:   run.bat
REM    Other commands:                        run.bat scan --both
REM                                           run.bat cross --network bsc
REM                                           run.bat verify
REM                                           run.bat selftest
REM
REM  Installs dependencies into a local .venv on first run, then executes
REM  main.py with whatever arguments you passed.
REM ==========================================================================

setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [run.bat] Creating virtual environment in .venv ...
    py -3 -m venv .venv
    if errorlevel 1 (
        echo [run.bat] "py -3" failed. Trying "python" instead...
        python -m venv .venv
        if errorlevel 1 (
            echo [run.bat] ERROR: could not create a virtual environment.
            echo [run.bat] Install Python 3.10+ from python.org, tick
            echo [run.bat] "Add python.exe to PATH" during install, retry.
            pause
            exit /b 1
        )
    )
    echo [run.bat] Installing dependencies (first run only)...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [run.bat] ERROR: dependency install failed. Check your network.
        pause
        exit /b 1
    )
    if not exist ".env" (
        echo [run.bat] No .env found - copying .env.example to .env
        copy /y ".env.example" ".env" >nul
        echo [run.bat] Edit .env if you have a CoinMarketCap key. It runs without one.
    )
)

if "%~1"=="" (
    ".venv\Scripts\python.exe" main.py scan
) else (
    ".venv\Scripts\python.exe" main.py %*
)

set RC=%ERRORLEVEL%
echo.
echo [run.bat] exit code %RC%
pause
endlocal & exit /b %RC%
