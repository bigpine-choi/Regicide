@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\play_godot.ps1" -NeuralModel "%~dp0models\regicide-small-v1.nn"
if errorlevel 1 pause
