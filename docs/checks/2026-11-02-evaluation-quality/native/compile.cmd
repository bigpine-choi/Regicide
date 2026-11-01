@echo off
chcp 65001 >nul
call "C:\Program Files\Microsoft Visual Studio\18\Community\VC\Auxiliary\Build\vcvars64.bat" >nul
if errorlevel 1 exit /b 1
cd /d "C:\Users\hanso\.codex\.chatgpt-projects\g-p-6a8b058617088191ac7cb44895558f31\Regicide-m1-integration"
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude /c src\board.cpp /Fobuild-eval-activity\board.obj
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude /c src\search.cpp /Fobuild-eval-activity\search.obj
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude /c src\evaluation.cpp /Fobuild-eval-activity\evaluation.obj
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude /c tests\reference_evaluation_v054.cpp /Fobuild-eval-activity\reference_evaluation_v054.obj
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude /c tests\reference_evaluation_v056.cpp /Fobuild-eval-activity\reference_evaluation_v056.obj
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude /c src\transposition.cpp /Fobuild-eval-activity\transposition.obj
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude /c src\see.cpp /Fobuild-eval-activity\see.obj
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude /c src\neural.cpp /Fobuild-eval-activity\neural.obj
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude /c src\nnue.cpp /Fobuild-eval-activity\nnue.obj
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /GL- /DNDEBUG /Iinclude /c tests\reference_neural_v063.cpp /Fobuild-eval-activity\reference_neural_v063.obj
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude /c src\uci.cpp /Fobuild-eval-activity\uci.obj
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude src\main.cpp build-eval-activity\board.obj build-eval-activity\search.obj build-eval-activity\evaluation.obj build-eval-activity\transposition.obj build-eval-activity\see.obj build-eval-activity\neural.obj build-eval-activity\nnue.obj build-eval-activity\uci.obj /Fobuild-eval-activity\main.obj /Febuild-eval-activity\regicide.exe
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude tests\rules_tests.cpp build-eval-activity\board.obj /Fobuild-eval-activity\rules_tests.obj /Febuild-eval-activity\regicide_tests.exe
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude tests\search_tests.cpp build-eval-activity\board.obj build-eval-activity\search.obj build-eval-activity\evaluation.obj build-eval-activity\transposition.obj build-eval-activity\see.obj build-eval-activity\neural.obj build-eval-activity\nnue.obj /Fobuild-eval-activity\search_tests.obj /Febuild-eval-activity\regicide_search_tests.exe
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude tests\evaluation_tests.cpp build-eval-activity\board.obj build-eval-activity\evaluation.obj build-eval-activity\reference_evaluation_v054.obj build-eval-activity\reference_evaluation_v056.obj /Fobuild-eval-activity\evaluation_tests.obj /Febuild-eval-activity\regicide_evaluation_tests.exe
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude tests\board_state_tests.cpp build-eval-activity\board.obj /Fobuild-eval-activity\board_state_tests.obj /Febuild-eval-activity\regicide_board_state_tests.exe
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude tests\transposition_tests.cpp build-eval-activity\board.obj build-eval-activity\search.obj build-eval-activity\evaluation.obj build-eval-activity\transposition.obj build-eval-activity\see.obj build-eval-activity\neural.obj build-eval-activity\nnue.obj /Fobuild-eval-activity\transposition_tests.obj /Febuild-eval-activity\regicide_transposition_tests.exe
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude tests\see_tests.cpp build-eval-activity\board.obj build-eval-activity\search.obj build-eval-activity\evaluation.obj build-eval-activity\transposition.obj build-eval-activity\see.obj build-eval-activity\neural.obj build-eval-activity\nnue.obj /Fobuild-eval-activity\see_tests.obj /Febuild-eval-activity\regicide_see_tests.exe
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude tests\neural_tests.cpp build-eval-activity\reference_neural_v063.obj build-eval-activity\board.obj build-eval-activity\evaluation.obj build-eval-activity\neural.obj build-eval-activity\nnue.obj /Fobuild-eval-activity\neural_tests.obj /Febuild-eval-activity\regicide_neural_tests.exe
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude tests\nnue_tests.cpp build-eval-activity\board.obj build-eval-activity\search.obj build-eval-activity\evaluation.obj build-eval-activity\transposition.obj build-eval-activity\see.obj build-eval-activity\neural.obj build-eval-activity\nnue.obj /Fobuild-eval-activity\regicide_nnue_tests.obj /Febuild-eval-activity\regicide_nnue_tests.exe
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude tests\neural_model_tests.cpp build-eval-activity\board.obj build-eval-activity\search.obj build-eval-activity\evaluation.obj build-eval-activity\transposition.obj build-eval-activity\see.obj build-eval-activity\neural.obj build-eval-activity\nnue.obj /Fobuild-eval-activity\regicide_neural_model_tests.obj /Febuild-eval-activity\regicide_neural_model_tests.exe
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude tests\leaf_observer_tests.cpp build-eval-activity\board.obj build-eval-activity\search.obj build-eval-activity\evaluation.obj build-eval-activity\transposition.obj build-eval-activity\see.obj build-eval-activity\neural.obj build-eval-activity\nnue.obj /Fobuild-eval-activity\regicide_leaf_observer_tests.obj /Febuild-eval-activity\regicide_leaf_observer_tests.exe
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude scripts\collect_search_leaves.cpp build-eval-activity\board.obj build-eval-activity\search.obj build-eval-activity\evaluation.obj build-eval-activity\transposition.obj build-eval-activity\see.obj build-eval-activity\neural.obj build-eval-activity\nnue.obj /Fobuild-eval-activity\collect_search_leaves.obj /Febuild-eval-activity\regicide_leaf_data.exe
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude scripts\evaluation_benchmark.cpp build-eval-activity\board.obj build-eval-activity\search.obj build-eval-activity\evaluation.obj build-eval-activity\transposition.obj build-eval-activity\see.obj build-eval-activity\neural.obj build-eval-activity\nnue.obj /Fobuild-eval-activity\regicide_evaluation_benchmark.obj /Febuild-eval-activity\regicide_evaluation_benchmark.exe
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude scripts\neural_incremental_benchmark.cpp build-eval-activity\board.obj build-eval-activity\search.obj build-eval-activity\evaluation.obj build-eval-activity\transposition.obj build-eval-activity\see.obj build-eval-activity\neural.obj build-eval-activity\nnue.obj /Fobuild-eval-activity\regicide_neural_incremental_benchmark.obj /Febuild-eval-activity\regicide_neural_incremental_benchmark.exe
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude scripts\inspect_evaluation.cpp build-eval-activity\board.obj build-eval-activity\search.obj build-eval-activity\evaluation.obj build-eval-activity\transposition.obj build-eval-activity\see.obj build-eval-activity\neural.obj build-eval-activity\nnue.obj /Fobuild-eval-activity\inspect_evaluation.obj /Febuild-eval-activity\regicide_evaluation_probe.exe
if errorlevel 1 exit /b 1

exit /b %errorlevel%