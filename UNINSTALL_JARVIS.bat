@echo off
rem Remove JARVIS auto-start and stop it. Your folder, records and models are kept;
rem delete the folder yourself afterwards if you want everything gone.
setlocal
title JARVIS Uninstaller
cd /d "%~dp0"

net session >nul 2>&1
if %errorlevel% neq 0 (
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)

echo Removing auto-start...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0boot\register_autostart.ps1" -Unregister

echo Stopping JARVIS...
rem Only stops JARVIS's own processes: python/pythonw/electron/node started from this folder.
powershell -NoProfile -Command "$r = (Resolve-Path '.').Path; Get-CimInstance Win32_Process | Where-Object { @('python.exe','pythonw.exe','electron.exe','node.exe') -contains $_.Name -and $_.CommandLine -and $_.CommandLine.Contains($r) } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

echo.
echo JARVIS auto-start removed and its processes stopped. Your folder and data are untouched.
pause
exit /b 0
