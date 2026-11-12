@echo off
rem Stop JARVIS now. It still starts by itself at the next sign-in;
rem UNINSTALL_JARVIS.bat removes that. Start it again with START_JARVIS.bat.
setlocal
title JARVIS
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" goto notinstalled
".venv\Scripts\python.exe" "boot\launcher.py" --stop
timeout /t 5 >nul
exit /b 0

:notinstalled
echo.
echo   JARVIS is not installed in this folder, so there is nothing to stop here.
echo.
pause
exit /b 1
