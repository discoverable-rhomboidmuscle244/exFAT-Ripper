@echo off
REM ============================================================
REM  Build exFAT Ripper into a single Windows .exe
REM  Just double-click this file. You need Python 3.10+ installed.
REM ============================================================

setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo.
echo ============================================
echo    exFAT Ripper  -  EXE builder
echo ============================================
echo.

REM ---- 1. find Python under whatever name it has ----
REM    Windows installs it as python, py, or python3 depending on setup.
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
    echo [X] Python was not found.
    echo.
    echo     None of these worked:  python   py   python3
    echo.
    echo     Install Python 3.10 or newer from:
    echo         https://www.python.org/downloads/
    echo     IMPORTANT: on the first installer screen, tick the box
    echo         "Add python.exe to PATH"
    echo     then re-run this build.
    echo.
    pause
    exit /b 1
)

echo [1/4] Python found  ^(using "!PY!"^)
for /f "tokens=*" %%v in ('!PY! --version 2^>^&1') do echo       %%v
echo.

REM ---- 2. install dependencies ----
echo [2/4] Installing dependencies...
echo       ^(first time this can take several minutes^)
echo.
!PY! -m pip install --upgrade pip
!PY! -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo [X] Failed to install dependencies. Read the messages above.
    echo     A common fix: run this build again ^(pip sometimes needs a retry^).
    pause
    exit /b 1
)
echo.

REM ---- 3. install the Playwright Chromium engine ----
echo [3/4] Installing the Chromium engine for Playwright...
echo.
!PY! -m playwright install chromium
if errorlevel 1 (
    echo.
    echo [!] Playwright browser install had a problem - continuing anyway.
    echo     The app can still work if you run it with Brave/Chrome already
    echo     open on the debug port. You can also retry this step later with:
    echo         !PY! -m playwright install chromium
    echo.
)

REM ---- 4. build the exe ----
echo [4/4] Building the .exe with PyInstaller...
echo       ^(this bundles Chromium so the exe is self-contained -
echo        it makes the build slow and the .exe large, ~200-300 MB^)
echo.
!PY! -m PyInstaller --noconfirm --clean exFAT_Ripper.spec
if errorlevel 1 (
    echo.
    echo [X] Build failed. Read the PyInstaller messages above.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  DONE.
echo.
echo  Your app is here:
echo      dist\exFAT Ripper.exe
echo.
echo  Double-click it to run. The first scrape builds the library
echo  ^(1-2 minutes^); after that it opens instantly and links
echo  resolve the moment you click a game.
echo ============================================================
echo.
pause
endlocal
