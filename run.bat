@echo off
cd /d "%~dp0"
if exist "%~dp0.venv\Scripts\pythonw.exe" goto launch

echo Setting up AutoKey...
set "BASEPY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%BASEPY%" set "BASEPY=python"
"%BASEPY%" -m venv "%~dp0.venv"
if errorlevel 1 (
    echo Could not create the Python environment.
    pause
    exit /b 1
)
"%~dp0.venv\Scripts\python.exe" -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 (
    echo Could not install the required packages.
    pause
    exit /b 1
)

:launch
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0autokey.py"
