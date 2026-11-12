@echo off
rem Remove JARVIS auto-start and stop it. Your folder, records and models are kept;
rem delete the folder yourself afterwards if you want everything gone.
setlocal
title JARVIS Uninstaller
cd /d "%~dp0"

fltmc >nul 2>&1
if not errorlevel 1 goto elevated
if /i "%~1"=="--elevated" goto noadmin
set "JARVIS_SELF=%~f0"
powershell -NoProfile -Command "Start-Process -FilePath $env:JARVIS_SELF -ArgumentList '--elevated' -Verb RunAs"
if errorlevel 1 goto noadmin
exit /b 0

:elevated
echo Removing auto-start...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0boot\register_autostart.ps1" -Unregister

echo Stopping JARVIS...
if exist ".venv\Scripts\python.exe" ".venv\Scripts\python.exe" "boot\launcher.py" --stop
rem Second pass without Python: only JARVIS's own processes, i.e. python/pythonw/electron/node
rem whose command line contains this folder.
powershell -NoProfile -Command "$r = (Resolve-Path '.').Path; Get-CimInstance Win32_Process | Where-Object { @('python.exe','pythonw.exe','electron.exe','node.exe') -contains $_.Name -and $_.CommandLine -and $_.CommandLine.Contains($r) } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

echo.
echo JARVIS auto-start removed and its processes stopped. Your folder and data are untouched.
pause
exit /b 0

:noadmin
echo.
echo   Removing auto-start needs administrator rights, but they were not granted.
echo   Double-click UNINSTALL_JARVIS.bat again and answer Yes when Windows asks.
echo.
pause
exit /b 1
