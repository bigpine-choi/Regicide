@echo off
call "C:\Program Files\Microsoft Visual Studio\18\Community\VC\Auxiliary\Build\vcvars64.bat" >nul
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude /c src/board.cpp src/search.cpp src/evaluation.cpp src/neural.cpp src/transposition.cpp src/see.cpp /Fobuild-neural\
if errorlevel 1 exit /b %errorlevel%
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude src/main.cpp src/uci.cpp build-neural/board.obj build-neural/search.obj build-neural/evaluation.obj build-neural/neural.obj build-neural/transposition.obj build-neural/see.obj /Febuild-neural/regicide.exe /Fobuild-neural\
if errorlevel 1 exit /b %errorlevel%
for %%t in (rules search evaluation board_state transposition see neural) do (
 cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude tests/%%t_tests.cpp build-neural/board.obj build-neural/search.obj build-neural/evaluation.obj build-neural/neural.obj build-neural/transposition.obj build-neural/see.obj /Febuild-neural/%%t_tests.exe /Fobuild-neural/%%t_tests.obj
 if errorlevel 1 exit /b 1
)
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude scripts/evaluation_benchmark.cpp build-neural/board.obj build-neural/evaluation.obj build-neural/neural.obj /Febuild-neural/evaluation_benchmark.exe /Fobuild-neural/evaluation_benchmark.obj
