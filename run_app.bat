@echo off
REM Run the app directly without building an .exe - handy for testing.
REM Needs dependencies installed (run build_exe.bat once, or install
REM manually with the commands shown in README.md).

setlocal EnableDelayedExpansion
cd /d "%~dp0"

REM ---- find Python under whatever name it has ----
set "PY="
python --version >nul 2>nul
if not errorlevel 1 set "PY=python"
if "!PY!"=="" (
    py --version >nul 2>nul
    if not errorlevel 1 set "PY=py"
)
if "!PY!"=="" (
    python3 --version >nul 2>nul
    if not errorlevel 1 set "PY=python3"
)
if "!PY!"=="" (
    echo [X] Python was not found ^(tried: python, py, python3^).
    echo     Install Python 3.10+ from https://www.python.org/downloads/
    echo     and tick "Add python.exe to PATH".
    pause
    exit /b 1
)

echo Starting PS5 exFAT Library  (using "!PY!")...
!PY! app.py
if errorlevel 1 (
    echo.
    echo The app exited with an error. If it mentions a missing module,
    echo run build_exe.bat once first to install the dependencies.
    pause
)
endlocal
