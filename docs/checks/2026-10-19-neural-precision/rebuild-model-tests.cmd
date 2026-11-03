@echo off
chcp 65001 >nul
call "C:\Program Files\Microsoft Visual Studio\18\Community\VC\Auxiliary\Build\vcvars64.bat" >nul
if errorlevel 1 exit /b 1
cd /d "C:\Users\hanso\.codex\.chatgpt-projects\g-p-6a8b058617088191ac7cb44895558f31\Regicide"
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude tests\neural_model_tests.cpp build-neural-precision-runtime\board.obj build-neural-precision-runtime\search.obj build-neural-precision-runtime\evaluation.obj build-neural-precision-runtime\neural.obj build-neural-precision-runtime\transposition.obj build-neural-precision-runtime\see.obj /Fobuild-neural-precision-runtime\neural_model_tests.obj /Febuild-neural-precision-runtime\regicide_neural_model_tests.exe
if errorlevel 1 exit /b 1
build-neural-precision-runtime\regicide_neural_model_tests.exe
exit /b %errorlevel%
