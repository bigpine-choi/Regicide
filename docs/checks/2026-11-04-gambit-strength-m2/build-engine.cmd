@echo off
call "C:\Program Files\Microsoft Visual Studio\18\Community\VC\Auxiliary\Build\vcvars64.bat" >nul
if errorlevel 1 exit /b 1
cd /d "C:\Users\hanso\.codex\.chatgpt-projects\g-p-6a8b058617088191ac7cb44895558f31\Regicide-m1-integration"
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude /c src\board.cpp /Fobuild-gambit-m2\board.obj
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude /c src\search.cpp /Fobuild-gambit-m2\search.obj
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude /c src\evaluation.cpp /Fobuild-gambit-m2\evaluation.obj
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude /c src\transposition.cpp /Fobuild-gambit-m2\transposition.obj
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude /c src\see.cpp /Fobuild-gambit-m2\see.obj
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude /c src\neural.cpp /Fobuild-gambit-m2\neural.obj
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude /c src\nnue.cpp /Fobuild-gambit-m2\nnue.obj
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude /c src\uci.cpp /Fobuild-gambit-m2\uci.obj
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude /c src\main.cpp /Fobuild-gambit-m2\main.obj
if errorlevel 1 exit /b 1
cl /nologo /EHsc build-gambit-m2\board.obj build-gambit-m2\search.obj build-gambit-m2\evaluation.obj build-gambit-m2\transposition.obj build-gambit-m2\see.obj build-gambit-m2\neural.obj build-gambit-m2\nnue.obj build-gambit-m2\uci.obj build-gambit-m2\main.obj /Febuild-gambit-m2\regicide.exe
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude scripts\inspect_evaluation.cpp build-gambit-m2\board.obj build-gambit-m2\search.obj build-gambit-m2\evaluation.obj build-gambit-m2\transposition.obj build-gambit-m2\see.obj build-gambit-m2\neural.obj build-gambit-m2\nnue.obj /Fobuild-gambit-m2\inspect_evaluation.obj /Febuild-gambit-m2\regicide_evaluation_probe.exe
if errorlevel 1 exit /b 1
