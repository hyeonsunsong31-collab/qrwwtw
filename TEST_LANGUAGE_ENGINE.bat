@echo off
rem ===================================================================
rem  JARVIS -- guided language-engine check.
rem
rem  Finds Ollama (the offline language engine) and starts it if it is
rem  not running, downloads the language model if it is missing, asks it
rem  one question, and tells you exactly what to fix. A copy of the
rem  report is saved to data\logs\llm_check.txt.
rem ===================================================================
setlocal
title JARVIS language-engine check
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" goto notinstalled

".venv\Scripts\python.exe" "scripts\llm_check.py" %*
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
