@echo off
setlocal
cd /d "%~dp0"
set "AUTO_SHORTS_SETUP_ONLY=1"
call start_desktop.bat
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --workpath build_visual_engine --distpath desktop_release_visual_engine "AUTO SHORTS STUDIO.spec"
if errorlevel 1 (
  echo Build failed.
  pause
  exit /b 1
)
if exist "bin\ffmpeg.exe" (
  if not exist "desktop_release_visual_engine\AUTO SHORTS STUDIO\bin" mkdir "desktop_release_visual_engine\AUTO SHORTS STUDIO\bin"
  copy /Y "bin\ffmpeg.exe" "desktop_release_visual_engine\AUTO SHORTS STUDIO\bin\ffmpeg.exe" >nul
  if exist "bin\FFmpeg-LICENSE.txt" copy /Y "bin\FFmpeg-LICENSE.txt" "desktop_release_visual_engine\AUTO SHORTS STUDIO\bin\FFmpeg-LICENSE.txt" >nul
  if exist "bin\FFmpeg-README.txt" copy /Y "bin\FFmpeg-README.txt" "desktop_release_visual_engine\AUTO SHORTS STUDIO\bin\FFmpeg-README.txt" >nul
) else (
  echo WARNING: bin\ffmpeg.exe is missing. The app will need FFmpeg installed separately.
)
echo Built: desktop_release_visual_engine\AUTO SHORTS STUDIO\AUTO SHORTS STUDIO.exe
pause
