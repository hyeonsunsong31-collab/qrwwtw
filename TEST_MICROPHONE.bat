@echo off
rem ===================================================================
rem  JARVIS -- guided microphone check.
rem
rem  Lists your microphones, reads the Windows privacy and mute settings,
rem  records a few seconds while you say "wake up Jarvis", and tells you
rem  exactly what to fix. Works while JARVIS is running. A copy of the
rem  report is saved to data\logs\mic_check.txt.
rem ===================================================================
setlocal
title JARVIS microphone check
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" goto notinstalled

".venv\Scripts\python.exe" "scripts\mic_check.py" %*
echo.
echo   Press any key to close this window.
pause >nul
exit /b 0

:notinstalled
echo.
echo   JARVIS is not installed yet. Double-click INSTALL_JARVIS.bat first.
echo.
pause
exit /b 3
