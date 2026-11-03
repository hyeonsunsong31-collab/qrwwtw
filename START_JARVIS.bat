@echo off
rem Start JARVIS now (background). Auto-start at logon is set up by INSTALL_JARVIS.bat.
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
  start "" ".venv\Scripts\pythonw.exe" "boot\jarvis_service.py"
) else (
  start "" pythonw "boot\jarvis_service.py"
)
echo JARVIS is starting in the background. Say "wake up Jarvis" or press the WAKE button.
