@echo off
chcp 65001 >nul
call "C:\Program Files\Microsoft Visual Studio\18\Community\VC\Auxiliary\Build\vcvars64.bat" >nul
if errorlevel 1 exit /b 1
cd /d "C:\Users\hanso\.codex\.chatgpt-projects\g-p-6a8b058617088191ac7cb44895558f31\Regicide-m1-integration"
REM version
"C:\Users\hanso\.codex\.chatgpt-projects\g-p-6a8b058617088191ac7cb44895558f31\Regicide-m1-integration\.venv-tests\Scripts\cmake.exe" --version
if errorlevel 1 exit /b 1
REM configure
"C:\Users\hanso\.codex\.chatgpt-projects\g-p-6a8b058617088191ac7cb44895558f31\Regicide-m1-integration\.venv-tests\Scripts\cmake.exe" -S "C:\Users\hanso\.codex\.chatgpt-projects\g-p-6a8b058617088191ac7cb44895558f31\Regicide-m1-integration" -B "C:\Users\hanso\.codex\.chatgpt-projects\g-p-6a8b058617088191ac7cb44895558f31\Regicide-m1-integration\build-m1-cmake" -G "NMake Makefiles" -DCMAKE_BUILD_TYPE=Release
if errorlevel 1 exit /b 1
REM build
"C:\Users\hanso\.codex\.chatgpt-projects\g-p-6a8b058617088191ac7cb44895558f31\Regicide-m1-integration\.venv-tests\Scripts\cmake.exe" --build "C:\Users\hanso\.codex\.chatgpt-projects\g-p-6a8b058617088191ac7cb44895558f31\Regicide-m1-integration\build-m1-cmake" --config Release --verbose
if errorlevel 1 exit /b 1
REM test-inventory
"C:\Users\hanso\.codex\.chatgpt-projects\g-p-6a8b058617088191ac7cb44895558f31\Regicide-m1-integration\.venv-tests\Scripts\ctest.exe" --test-dir "C:\Users\hanso\.codex\.chatgpt-projects\g-p-6a8b058617088191ac7cb44895558f31\Regicide-m1-integration\build-m1-cmake" -C Release --show-only=json-v1
if errorlevel 1 exit /b 1
REM ctest
"C:\Users\hanso\.codex\.chatgpt-projects\g-p-6a8b058617088191ac7cb44895558f31\Regicide-m1-integration\.venv-tests\Scripts\ctest.exe" --test-dir "C:\Users\hanso\.codex\.chatgpt-projects\g-p-6a8b058617088191ac7cb44895558f31\Regicide-m1-integration\build-m1-cmake" -C Release --output-on-failure --output-junit "C:\Users\hanso\.codex\.chatgpt-projects\g-p-6a8b058617088191ac7cb44895558f31\Regicide-m1-integration\docs\checks\2026-10-21-m1-integration\cmake\ctest.xml"
if errorlevel 1 exit /b 1
