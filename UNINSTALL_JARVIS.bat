@echo off
rem Remove JARVIS auto-start and stop it. (Does not delete this folder or your data.)
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
taskkill /f /im electron.exe >nul 2>&1
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*jarvis_service.py*' -or $_.CommandLine -like '*agent.server*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

echo.
echo JARVIS auto-start removed and processes stopped. Your folder and data are untouched.
pause
