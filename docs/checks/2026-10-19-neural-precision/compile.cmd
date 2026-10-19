@echo off
chcp 65001 >nul
call "C:\Program Files\Microsoft Visual Studio\18\Community\VC\Auxiliary\Build\vcvars64.bat" >nul
if errorlevel 1 exit /b 1
cd /d "C:\Users\hanso\.codex\.chatgpt-projects\g-p-6a8b058617088191ac7cb44895558f31\Regicide"
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude /c src\board.cpp /Fobuild-neural-precision-runtime\board.obj
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude /c src\search.cpp /Fobuild-neural-precision-runtime\search.obj
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude /c src\evaluation.cpp /Fobuild-neural-precision-runtime\evaluation.obj
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude /c src\neural.cpp /Fobuild-neural-precision-runtime\neural.obj
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude /c src\transposition.cpp /Fobuild-neural-precision-runtime\transposition.obj
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude /c src\see.cpp /Fobuild-neural-precision-runtime\see.obj
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude /c src\uci.cpp /Fobuild-neural-precision-runtime\uci.obj
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude src\main.cpp build-neural-precision-runtime\board.obj build-neural-precision-runtime\search.obj build-neural-precision-runtime\neural.obj build-neural-precision-runtime\evaluation.obj build-neural-precision-runtime\transposition.obj build-neural-precision-runtime\see.obj build-neural-precision-runtime\uci.obj /Fobuild-neural-precision-runtime\main.obj /Febuild-neural-precision-runtime\regicide.exe
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude tests\rules_tests.cpp build-neural-precision-runtime\board.obj /Fobuild-neural-precision-runtime\rules_tests.obj /Febuild-neural-precision-runtime\regicide_tests.exe
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude tests\search_tests.cpp build-neural-precision-runtime\board.obj build-neural-precision-runtime\search.obj build-neural-precision-runtime\neural.obj build-neural-precision-runtime\evaluation.obj build-neural-precision-runtime\transposition.obj build-neural-precision-runtime\see.obj /Fobuild-neural-precision-runtime\search_tests.obj /Febuild-neural-precision-runtime\regicide_search_tests.exe
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude tests\evaluation_tests.cpp build-neural-precision-runtime\board.obj build-neural-precision-runtime\evaluation.obj /Fobuild-neural-precision-runtime\evaluation_tests.obj /Febuild-neural-precision-runtime\regicide_evaluation_tests.exe
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude tests\board_state_tests.cpp build-neural-precision-runtime\board.obj /Fobuild-neural-precision-runtime\board_state_tests.obj /Febuild-neural-precision-runtime\regicide_board_state_tests.exe
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude tests\transposition_tests.cpp build-neural-precision-runtime\board.obj build-neural-precision-runtime\search.obj build-neural-precision-runtime\neural.obj build-neural-precision-runtime\evaluation.obj build-neural-precision-runtime\transposition.obj build-neural-precision-runtime\see.obj /Fobuild-neural-precision-runtime\transposition_tests.obj /Febuild-neural-precision-runtime\regicide_transposition_tests.exe
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude tests\see_tests.cpp build-neural-precision-runtime\board.obj build-neural-precision-runtime\search.obj build-neural-precision-runtime\neural.obj build-neural-precision-runtime\evaluation.obj build-neural-precision-runtime\transposition.obj build-neural-precision-runtime\see.obj /Fobuild-neural-precision-runtime\see_tests.obj /Febuild-neural-precision-runtime\regicide_see_tests.exe
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude tests\neural_tests.cpp build-neural-precision-runtime\board.obj build-neural-precision-runtime\search.obj build-neural-precision-runtime\evaluation.obj build-neural-precision-runtime\neural.obj build-neural-precision-runtime\transposition.obj build-neural-precision-runtime\see.obj /Fobuild-neural-precision-runtime\neural_tests.obj /Febuild-neural-precision-runtime\regicide_neural_tests.exe
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude scripts\evaluation_benchmark.cpp build-neural-precision-runtime\board.obj build-neural-precision-runtime\evaluation.obj build-neural-precision-runtime\neural.obj /Fobuild-neural-precision-runtime\evaluation_benchmark.obj /Febuild-neural-precision-runtime\regicide_evaluation_benchmark.exe
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude scripts\neural_incremental_benchmark.cpp build-neural-precision-runtime\board.obj build-neural-precision-runtime\evaluation.obj build-neural-precision-runtime\neural.obj /Fobuild-neural-precision-runtime\neural_incremental_benchmark.obj /Febuild-neural-precision-runtime\regicide_neural_incremental_benchmark.exe
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude tests\neural_model_tests.cpp build-neural-precision-runtime\board.obj build-neural-precision-runtime\search.obj build-neural-precision-runtime\evaluation.obj build-neural-precision-runtime\neural.obj build-neural-precision-runtime\transposition.obj build-neural-precision-runtime\see.obj /Fobuild-neural-precision-runtime\neural_model_tests.obj /Febuild-neural-precision-runtime\regicide_neural_model_tests.exe
exit /b %errorlevel%