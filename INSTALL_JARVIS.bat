@echo off
rem ===================================================================
rem  JARVIS -- double-click installer.
rem  Installs Python, Ollama, FFmpeg and all dependencies, downloads the
rem  on-screen display, the language model, voices and speech models,
rem  registers auto-start at sign-in, launches JARVIS and checks that it
rem  is really on screen. After this JARVIS starts every time you sign
rem  in. Safe to run again: finished steps are skipped, broken ones are
rem  repaired.
rem ===================================================================
setlocal
title JARVIS Installer
cd /d "%~dp0"

if not exist "scripts\setup_windows.ps1" goto notextracted

rem --- self-elevate: installing runtimes needs administrator rights ---
fltmc >nul 2>&1
if not errorlevel 1 goto elevated
if /i "%~1"=="--elevated" goto noadmin
echo Requesting administrator rights...
set "JARVIS_SELF=%~f0"
powershell -NoProfile -Command "Start-Process -FilePath $env:JARVIS_SELF -ArgumentList '--elevated' -Verb RunAs"
if errorlevel 1 goto noadmin
exit /b 0

:elevated
echo.
echo   Installing JARVIS. The first run can take 15-40 minutes because it
echo   downloads a language model, voices and speech models.
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup_windows.ps1"
set "RC=%errorlevel%"
echo.
if "%RC%"=="0" goto done
if "%RC%"=="2" goto notvisible
goto failed

:done
echo  ------------------------------------------------------------------
echo   JARVIS is installed, on screen, and starts by itself at sign-in.
echo   Wake it by saying "wake up Jarvis" or clicking the WAKE button
echo   in the bottom-right corner of the screen (or press Ctrl+Alt+J).
echo.
echo   START_JARVIS.bat      start it now / show where it is
echo   STOP_JARVIS.bat       stop it
echo   UNINSTALL_JARVIS.bat  remove auto-start
echo  ------------------------------------------------------------------
echo.
pause
exit /b 0

:notvisible
echo  ------------------------------------------------------------------
echo   JARVIS is installed, but it did not come up on screen.
echo   The reason and what to do are written just above, and in
echo   data\logs\diagnostics.txt. Fix that, then double-click
echo   START_JARVIS.bat.
echo  ------------------------------------------------------------------
echo.
pause
exit /b 2

:failed
echo  ------------------------------------------------------------------
echo   Setup hit a problem. Details are above and in data\install.log
echo   You can safely run INSTALL_JARVIS.bat again; finished steps are
echo   skipped.
echo  ------------------------------------------------------------------
echo.
pause
exit /b %RC%

:noadmin
echo.
echo   The installer needs administrator rights, but did not get them.
echo   Double-click INSTALL_JARVIS.bat again and answer Yes when Windows asks.
echo.
pause
exit /b 1

:notextracted
echo.
echo   This is not the complete JARVIS folder.
echo.
echo   If you opened this file from inside the ZIP, extract the whole ZIP
echo   first: right-click the ZIP, choose "Extract All", open the extracted
echo   folder and double-click INSTALL_JARVIS.bat there.
echo.
pause
exit /b 1
