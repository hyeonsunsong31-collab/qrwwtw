@echo off
rem Start JARVIS with software rendering for its on-screen display.
rem Use this if the HUD does not appear, is black, or flickers: that is
rem usually the graphics driver. The choice is remembered. To go back,
rem right-click the JARVIS icon next to the clock and untick
rem "Software rendering".
setlocal
cd /d "%~dp0"
call "%~dp0START_JARVIS.bat" --safe-graphics
exit /b %errorlevel%
