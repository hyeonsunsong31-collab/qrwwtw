@echo off
rem ===================================================================
rem  JARVIS -- start it now and check that it really appeared.
rem  (INSTALL_JARVIS.bat makes it start by itself at every sign-in.)
rem
rem  This window shows every start-up step. If something is wrong it
rem  says what and what to do, and saves a report to
rem  data\logs\diagnostics.txt. If the on-screen display is missing it
rem  is downloaded automatically.
rem ===================================================================
setlocal
title JARVIS
cd /d "%~dp0"

if not exist "boot\launcher.py" goto notextracted
if not exist ".venv\Scripts\python.exe" goto notinstalled

".venv\Scripts\python.exe" "boot\launcher.py" %*
set "RC=%errorlevel%"
if "%RC%"=="0" goto started
if "%RC%"=="3" goto needsinstall
if "%RC%"=="1" goto failed
if "%RC%"=="130" goto failed
goto brokenpython

:started
echo   This window closes by itself in a few seconds.
timeout /t 8 >nul
exit /b 0

:failed
echo   Press any key to close this window.
pause >nul
exit /b %RC%

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

:notinstalled
echo.
echo   JARVIS is not installed in this folder yet.
echo.
echo   Press any key to run the installer now, or close this window to cancel.
pause >nul
call "%~dp0INSTALL_JARVIS.bat"
exit /b %errorlevel%

:brokenpython
echo.
echo   JARVIS's Python environment could not be started (code %RC%).
echo   The installer repairs it and keeps everything that is already installed.

:needsinstall
echo.
echo   Press any key to run the installer now, or close this window to cancel.
pause >nul
call "%~dp0INSTALL_JARVIS.bat"
exit /b %errorlevel%
