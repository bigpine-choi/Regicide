@echo off
call "$VS_VCVARS64" >nul
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude tests\evaluation_tests.cpp build\board.obj build\evaluation.obj build\reference_evaluation_v054.obj /Fobuild\evaluation_tests.obj /Febuild\regicide_evaluation_tests.exe
if errorlevel 1 exit /b 1
build\regicide_evaluation_tests.exe
exit /b %errorlevel%