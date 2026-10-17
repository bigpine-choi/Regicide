@echo off
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\play_godot.ps1" %*
set "REGICIDE_GODOT_LAUNCH_EXIT=%errorlevel%"
if not "%REGICIDE_GODOT_LAUNCH_EXIT%"=="0" pause
exit /b %REGICIDE_GODOT_LAUNCH_EXIT%
