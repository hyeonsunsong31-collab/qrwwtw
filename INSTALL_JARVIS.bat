@echo off
rem ===================================================================
rem  JARVIS -- double-click installer.
rem  Installs Python, Node.js, Ollama, FFmpeg and all dependencies,
rem  downloads the language model, voices and speech models, registers
rem  auto-start at logon, and launches JARVIS. After this JARVIS starts
rem  every time you log on. Safe to run again: finished steps are skipped.
rem ===================================================================
setlocal
title JARVIS Installer
cd /d "%~dp0"

rem --- self-elevate: installing runtimes needs administrator rights ---
net session >nul 2>&1
if %errorlevel% neq 0 (
  echo Requesting administrator privileges...
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)

echo.
echo   Installing JARVIS. The first run can take 15-40 minutes because it
echo   downloads a language model, voices and speech models.
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup_windows.ps1"
set RC=%errorlevel%
echo.
if not "%RC%"=="0" goto failed

echo  ------------------------------------------------------------------
echo   JARVIS is installed, running, and starts automatically at logon.
echo   Wake it by saying "wake up Jarvis" or clicking the WAKE button.
echo   To remove auto-start later, run UNINSTALL_JARVIS.bat.
echo  ------------------------------------------------------------------
echo.
pause
exit /b 0

:failed
echo  ------------------------------------------------------------------
echo   Setup hit a problem. Details are in data\install.log
echo   You can safely run INSTALL_JARVIS.bat again; finished steps are skipped.
echo  ------------------------------------------------------------------
echo.
pause
exit /b %RC%
