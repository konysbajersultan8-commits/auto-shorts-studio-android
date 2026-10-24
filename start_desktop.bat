@echo off
setlocal
cd /d "%~dp0"
set "PYTHON_EXE="
if exist ".venv\Scripts\python.exe" set "PYTHON_EXE=.venv\Scripts\python.exe"
where python >nul 2>nul
if not defined PYTHON_EXE if not errorlevel 1 (
  python -c "import sys; assert sys.version_info >= (3,11)" >nul 2>nul
  if not errorlevel 1 set "PYTHON_EXE=python"
)
if not defined PYTHON_EXE if exist "%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" set "PYTHON_EXE=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if not defined PYTHON_EXE (
  echo Python 3.11+ not found. Install Python and retry.
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
  "%PYTHON_EXE%" -m venv --without-pip --system-site-packages .venv
  if errorlevel 1 (
    echo Could not create virtual environment.
    pause
    exit /b 1
  )
)
".venv\Scripts\python.exe" -c "import PIL,numpy,comtypes,elevenlabs,PySide6.QtWidgets,PySide6.QtMultimedia,PySide6.QtMultimediaWidgets" >nul 2>nul
if errorlevel 1 (
  echo Installing desktop dependencies into project environment...
  "%PYTHON_EXE%" -m pip install --target ".venv\Lib\site-packages" -r requirements.txt -r desktop_requirements.txt
  if errorlevel 1 (
    echo Dependency installation failed. Check Internet connection and retry.
    pause
    exit /b 1
  )
)
if "%AUTO_SHORTS_SETUP_ONLY%"=="1" exit /b 0
".venv\Scripts\python.exe" desktop.py
if errorlevel 1 pause
