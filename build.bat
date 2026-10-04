@echo off
setlocal
title RAM Cutter Release Builder
cd /d "%~dp0"

echo.
echo  ==========================================
echo   RAM Cutter - Build release installer
echo  ==========================================
echo.

set "PYTHON=python"
where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python 3.11 or newer was not found in PATH.
    echo         Install it from https://python.org and enable Add Python to PATH.
    goto :failed
)

if exist ".venv\Scripts\python.exe" (
    set "PYTHON=.venv\Scripts\python.exe"
) else (
    echo [INFO] Creating build virtual environment...
    python -m venv .venv
    if errorlevel 1 goto :failed
    set "PYTHON=.venv\Scripts\python.exe"
)

"%PYTHON%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 (
    echo [ERROR] The selected Python environment must be version 3.11 or newer.
    goto :failed
)

for /f "tokens=2 delims== " %%v in ('findstr /r /c:"^version = " pyproject.toml') do set "APP_VERSION=%%~v"
if not defined APP_VERSION (
    echo [ERROR] Could not read the project version from pyproject.toml.
    goto :failed
)

set "ISCC="
where ISCC.exe >nul 2>&1
if not errorlevel 1 set "ISCC=ISCC.exe"
if not defined ISCC if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not defined ISCC (
    echo [ERROR] Inno Setup 6 was not found.
    echo         Install it from https://jrsoftware.org/isinfo.php and run build.bat again.
    goto :failed
)

echo [INFO] Installing pinned build dependencies...
"%PYTHON%" -m pip install --disable-pip-version-check --requirement requirements-build.txt
if errorlevel 1 goto :failed

echo [INFO] Building standalone executable with PyInstaller...
"%PYTHON%" -m PyInstaller "RAM Cutter.spec" --noconfirm --clean --workpath "%TEMP%\RAM Cutter PyInstaller-%RANDOM%-%RANDOM%"
if errorlevel 1 goto :failed

echo [INFO] Building RAM Cutter v%APP_VERSION% Setup.exe with Inno Setup...
"%ISCC%" /O+ /DAppVersion=%APP_VERSION% "installer\RAM Cutter.iss"
if errorlevel 1 goto :failed

echo.
echo  ==========================================
echo   BUILD COMPLETE
echo   Executable: dist\RAM Cutter.exe
echo   Installer:  dist\RAM Cutter v%APP_VERSION% Setup.exe
echo  ==========================================
echo.
pause
exit /b 0

:failed
echo.
echo [ERROR] Release build did not complete.
pause
exit /b 1
