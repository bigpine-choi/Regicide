@echo off
call "C:\Program Files\Microsoft Visual Studio\18\Community\VC\Auxiliary\Build\vcvars64.bat" >nul
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude scripts\trace_gambit_leaf.cpp build-gambit-m2\board.obj build-gambit-m2\search.obj build-gambit-m2\evaluation.obj build-gambit-m2\transposition.obj build-gambit-m2\see.obj build-gambit-m2\neural.obj build-gambit-m2\nnue.obj /Fobuild-gambit-m2\trace_gambit_leaf.obj /Febuild-gambit-m2\regicide_trace_gambit_leaf.exe
exit /b %errorlevel%
