@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "VENVPY=%~dp0.venv\Scripts\python.exe"
set "BASEPY="
set "USE_PYLAUNCHER="

call :venv_ok
if not errorlevel 1 goto launch

echo Preparing a Python environment for this computer...
call :find_python
if not defined BASEPY if not defined USE_PYLAUNCHER (
    echo.
    echo Python 3 was not found.
    echo Install Python 3 from https://www.python.org/downloads/
    echo and check "Add python.exe to PATH".
    echo.
    pause
    exit /b 1
)

if exist "%~dp0.venv\" (
    echo Removing the old environment copied from another computer...
    rmdir /s /q "%~dp0.venv" 2>nul
    if exist "%~dp0.venv\Scripts\python.exe" (
        echo Could not remove .venv. Exit AutoKey and try again.
        pause
        exit /b 1
    )
)

if defined USE_PYLAUNCHER (
    py -3 -m venv "%~dp0.venv"
) else (
    "%BASEPY%" -m venv "%~dp0.venv"
)
if errorlevel 1 (
    echo Could not create the Python environment.
    pause
    exit /b 1
)

"%VENVPY%" -m pip install --upgrade pip
"%VENVPY%" -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 (
    echo Could not install the required packages.
    pause
    exit /b 1
)

call :venv_ok
if errorlevel 1 (
    echo The environment was created, but AutoKey still cannot start.
    pause
    exit /b 1
)

:launch
wscript //nologo "%~dp0start_autokey.vbs"
exit /b 0

:venv_ok
if not exist "%VENVPY%" exit /b 1
"%VENVPY%" -c "import keyboard, pystray, PIL, tkinter" >nul 2>&1
exit /b %ERRORLEVEL%

:find_python
where py >nul 2>&1
if not errorlevel 1 (
    py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
    if not errorlevel 1 (
        set "USE_PYLAUNCHER=1"
        exit /b 0
    )
)
for %%V in (Python314 Python313 Python312 Python311 Python310) do (
    if exist "%LOCALAPPDATA%\Programs\Python\%%V\python.exe" (
        set "BASEPY=%LOCALAPPDATA%\Programs\Python\%%V\python.exe"
        exit /b 0
    )
)
for %%V in (314 313 312 311 310) do (
    if exist "%ProgramFiles%\Python%%V\python.exe" (
        set "BASEPY=%ProgramFiles%\Python%%V\python.exe"
        exit /b 0
    )
    if exist "C:\Python%%V\python.exe" (
        set "BASEPY=C:\Python%%V\python.exe"
        exit /b 0
    )
)
where python >nul 2>&1
if not errorlevel 1 (
    python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
    if not errorlevel 1 (
        set "BASEPY=python"
        exit /b 0
    )
)
exit /b 1
