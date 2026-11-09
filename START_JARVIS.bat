@echo off
rem Start JARVIS now, in the background. INSTALL_JARVIS.bat sets up auto-start at logon.
setlocal
cd /d "%~dp0"

schtasks /query /tn JARVIS >nul 2>&1
if %errorlevel%==0 (
  schtasks /run /tn JARVIS >nul
  goto started
)
if exist ".venv\Scripts\pythonw.exe" (
  start "" ".venv\Scripts\pythonw.exe" "boot\jarvis_service.py"
  goto started
)
echo JARVIS is not installed yet - please run INSTALL_JARVIS.bat first.
pause
exit /b 1

:started
echo JARVIS is starting. Say "wake up Jarvis" or press the WAKE button.
timeout /t 3 >nul
exit /b 0
