@echo off
call "C:\Program Files\Microsoft Visual Studio\18\Community\VC\Auxiliary\Build\vcvars64.bat" >nul
if errorlevel 1 exit /b 1
cd /d "C:\Users\hanso\.codex\.chatgpt-projects\g-p-6a8b058617088191ac7cb44895558f31\Regicide-m1-integration"
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude /c src\board.cpp /Fobuild-gambit-horizon\baseline\board.obj
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude /c src\search.cpp /Fobuild-gambit-horizon\baseline\search.obj
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude /c src\evaluation.cpp /Fobuild-gambit-horizon\baseline\evaluation.obj
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude /c src\transposition.cpp /Fobuild-gambit-horizon\baseline\transposition.obj
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude /c src\see.cpp /Fobuild-gambit-horizon\baseline\see.obj
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude /c src\neural.cpp /Fobuild-gambit-horizon\baseline\neural.obj
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude /c src\nnue.cpp /Fobuild-gambit-horizon\baseline\nnue.obj
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude /c src\uci.cpp /Fobuild-gambit-horizon\baseline\uci.obj
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude /c src\main.cpp /Fobuild-gambit-horizon\baseline\main.obj
if errorlevel 1 exit /b 1
cl /nologo /EHsc build-gambit-horizon\baseline\board.obj build-gambit-horizon\baseline\search.obj build-gambit-horizon\baseline\evaluation.obj build-gambit-horizon\baseline\transposition.obj build-gambit-horizon\baseline\see.obj build-gambit-horizon\baseline\neural.obj build-gambit-horizon\baseline\nnue.obj build-gambit-horizon\baseline\uci.obj build-gambit-horizon\baseline\main.obj /Febuild-gambit-horizon\baseline\regicide.exe
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude scripts\inspect_evaluation.cpp build-gambit-horizon\baseline\board.obj build-gambit-horizon\baseline\search.obj build-gambit-horizon\baseline\evaluation.obj build-gambit-horizon\baseline\transposition.obj build-gambit-horizon\baseline\see.obj build-gambit-horizon\baseline\neural.obj build-gambit-horizon\baseline\nnue.obj /Fobuild-gambit-horizon\baseline\inspect_evaluation.obj /Febuild-gambit-horizon\baseline\regicide_evaluation_probe.exe
if errorlevel 1 exit /b 1

