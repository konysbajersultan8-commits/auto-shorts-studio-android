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
  echo Python 3.11+ не найден. Установите Python с python.org и запустите файл снова.
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
  "%PYTHON_EXE%" -m venv --without-pip --system-site-packages .venv
  if errorlevel 1 (
    echo Не удалось создать виртуальное окружение.
    pause
    exit /b 1
  )
)
".venv\Scripts\python.exe" -c "import PIL,numpy" >nul 2>nul
if errorlevel 1 (
  echo Устанавливаются Pillow и NumPy...
  "%PYTHON_EXE%" -m pip install --target ".venv\Lib\site-packages" -r requirements.txt
  if errorlevel 1 (
    echo Не удалось установить зависимости. Проверьте интернет или установите Pillow и NumPy вручную.
    pause
    exit /b 1
  )
)
".venv\Scripts\python.exe" app.py
if errorlevel 1 pause
