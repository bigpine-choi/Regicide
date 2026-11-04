@echo off
call "C:\Program Files\Microsoft Visual Studio\18\Community\VC\Auxiliary\Build\vcvars64.bat" >nul
if errorlevel 1 exit /b 1
cd /d "C:\Users\hanso\.codex\.chatgpt-projects\g-p-6a8b058617088191ac7cb44895558f31\Regicide-m1-integration"
if errorlevel 1 exit /b 1
if errorlevel 1 exit /b 1
if errorlevel 1 exit /b 1
if errorlevel 1 exit /b 1
if errorlevel 1 exit /b 1
if errorlevel 1 exit /b 1
if errorlevel 1 exit /b 1
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude /c src\uci.cpp /Fobuild-gambit-horizon\candidate\uci.obj
if errorlevel 1 exit /b 1
if errorlevel 1 exit /b 1
cl /nologo /EHsc build-gambit-horizon\candidate\board.obj build-gambit-horizon\candidate\search.obj build-gambit-horizon\candidate\evaluation.obj build-gambit-horizon\candidate\transposition.obj build-gambit-horizon\candidate\see.obj build-gambit-horizon\candidate\neural.obj build-gambit-horizon\candidate\nnue.obj build-gambit-horizon\candidate\uci.obj build-gambit-horizon\candidate\main.obj /Febuild-gambit-horizon\candidate\regicide.exe
if errorlevel 1 exit /b 1
if errorlevel 1 exit /b 1
