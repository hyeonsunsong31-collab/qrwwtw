@echo off
rem ===================================================================
rem  JARVIS — double-click installer.
rem  Installs Python/Node/Ollama + dependencies + models, registers
rem  auto-start at logon, and launches JARVIS now. After this, JARVIS
rem  starts automatically every time you turn on the PC.
rem ===================================================================
title JARVIS Installer
cd /d "%~dp0"

rem --- self-elevate (installing runtimes needs administrator) ---
net session >nul 2>&1
if %errorlevel% neq 0 (
  echo Requesting administrator privileges...
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)

echo.
echo  Installing JARVIS. First run can take 10-25 minutes (downloads a language model).
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup_windows.ps1"

echo.
echo  ------------------------------------------------------------------
echo   Setup finished. JARVIS is running and will auto-start at logon.
echo   Wake it by saying "wake up Jarvis" or clicking the WAKE button.
echo   To remove auto-start later, run UNINSTALL_JARVIS.bat.
echo  ------------------------------------------------------------------
echo.
pause
