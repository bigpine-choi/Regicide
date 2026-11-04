@echo off
call "C:\Program Files\Microsoft Visual Studio\18\Community\VC\Auxiliary\Build\vcvars64.bat" >nul
if errorlevel 1 exit /b 1
cd /d "C:\Users\hanso\.codex\.chatgpt-projects\g-p-6a8b058617088191ac7cb44895558f31\Regicide-m1-integration"
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude /c src\board.cpp /Fobuild-gambit-horizon\candidate\board.obj
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude /c src\search.cpp /Fobuild-gambit-horizon\candidate\search.obj
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude /c src\evaluation.cpp /Fobuild-gambit-horizon\candidate\evaluation.obj
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude /c src\transposition.cpp /Fobuild-gambit-horizon\candidate\transposition.obj
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude /c src\see.cpp /Fobuild-gambit-horizon\candidate\see.obj
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude /c src\neural.cpp /Fobuild-gambit-horizon\candidate\neural.obj
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude /c src\nnue.cpp /Fobuild-gambit-horizon\candidate\nnue.obj
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude /c src\uci.cpp /Fobuild-gambit-horizon\candidate\uci.obj
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude /c src\main.cpp /Fobuild-gambit-horizon\candidate\main.obj
if errorlevel 1 exit /b 1
cl /nologo /EHsc build-gambit-horizon\candidate\board.obj build-gambit-horizon\candidate\search.obj build-gambit-horizon\candidate\evaluation.obj build-gambit-horizon\candidate\transposition.obj build-gambit-horizon\candidate\see.obj build-gambit-horizon\candidate\neural.obj build-gambit-horizon\candidate\nnue.obj build-gambit-horizon\candidate\uci.obj build-gambit-horizon\candidate\main.obj /Febuild-gambit-horizon\candidate\regicide.exe
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude scripts\inspect_evaluation.cpp build-gambit-horizon\candidate\board.obj build-gambit-horizon\candidate\search.obj build-gambit-horizon\candidate\evaluation.obj build-gambit-horizon\candidate\transposition.obj build-gambit-horizon\candidate\see.obj build-gambit-horizon\candidate\neural.obj build-gambit-horizon\candidate\nnue.obj /Fobuild-gambit-horizon\candidate\inspect_evaluation.obj /Febuild-gambit-horizon\candidate\regicide_evaluation_probe.exe
if errorlevel 1 exit /b 1

