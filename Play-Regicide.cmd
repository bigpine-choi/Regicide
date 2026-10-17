@echo off
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\play_human.ps1" %*
set "REGICIDE_LAUNCH_EXIT=%errorlevel%"
if not "%REGICIDE_LAUNCH_EXIT%"=="0" pause
exit /b %REGICIDE_LAUNCH_EXIT%
