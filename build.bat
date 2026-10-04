@echo off
:: ============================================================
::  RAM Cutter — build standalone .exe for non-coders
::  Double-click this file to build dist\RAM Cutter.exe
:: ============================================================
title RAM Cutter Builder

echo.
echo  ==========================================
echo   RAM Cutter — Building standalone .exe
echo  ==========================================
echo.

:: Make sure we're in the script's own folder
cd /d "%~dp0"

:: ── 1. Find Python ────────────────────────────────────────
set PYTHON=python
where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found in PATH.
    echo         Please install Python 3.11+ from https://python.org
    echo         and tick "Add Python to PATH" during install.
    pause
    exit /b 1
)

for /f "tokens=*" %%v in ('python --version 2^>^&1') do set PY_VER=%%v
echo [OK] Found %PY_VER%
echo.

:: ── 2. Use venv python if present ────────────────────────
if exist ".venv\Scripts\python.exe" (
    echo [INFO] Using existing .venv
    set PYTHON=.venv\Scripts\python.exe
) else (
    echo [INFO] Creating virtual environment...
    python -m venv .venv
    set PYTHON=.venv\Scripts\python.exe
    echo [OK] venv created.
)
echo.

:: ── 3. Install / upgrade dependencies ────────────────────
echo [INFO] Installing dependencies (psutil, pyinstaller)...
%PYTHON% -m pip install --upgrade pip --quiet
%PYTHON% -m pip install "psutil>=5.9,<7" "pyinstaller>=6,<7" --quiet
if errorlevel 1 (
    echo [ERROR] pip install failed. Check your internet connection.
    pause
    exit /b 1
)
echo [OK] Dependencies installed.
echo.

:: ── 4. Build ──────────────────────────────────────────────
echo [INFO] Running PyInstaller...
echo.
%PYTHON% -m PyInstaller "RAM Cutter.spec" --noconfirm
if errorlevel 1 (
    echo.
    echo [ERROR] PyInstaller failed. See output above.
    pause
    exit /b 1
)

echo.
echo  ==========================================
echo   BUILD COMPLETE!
echo   Output: dist\RAM Cutter.exe
echo  ==========================================
echo.
echo  Share the file at:
echo    %~dp0dist\RAM Cutter.exe
echo.
pause
