@echo off
chcp 65001 >nul
call "$VS_VCVARS64" >nul
if errorlevel 1 exit /b 1
cd /d "$REPO"
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /GL- /Iinclude /Itests /c src\board.cpp /Fo"$REPO\build-pawn-opt-profile\board.obj"
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /GL- /Iinclude /Itests /c src\evaluation.cpp /Fo"$REPO\build-pawn-opt-profile\evaluation.obj"
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /GL- /Iinclude /Itests /c tests\reference_evaluation_v054.cpp /Fo"$REPO\build-pawn-opt-profile\reference_evaluation_v054.obj"
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /GL- /Iinclude /Itests /c tests\reference_evaluation_v056.cpp /Fo"$REPO\build-pawn-opt-profile\reference_evaluation_v056.obj"
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /GL- /Iinclude /Itests /c scripts\profile_evaluation.cpp /Fo"$REPO\build-pawn-opt-profile\profile_evaluation.obj"
if errorlevel 1 exit /b 1
link /nologo "$REPO\build-pawn-opt-profile\profile_evaluation.obj" "$REPO\build-pawn-opt-profile\board.obj" "$REPO\build-pawn-opt-profile\evaluation.obj" "$REPO\build-pawn-opt-profile\reference_evaluation_v054.obj" "$REPO\build-pawn-opt-profile\reference_evaluation_v056.obj" /OUT:"$REPO\build-pawn-opt-profile\profile_evaluation.exe"
exit /b %errorlevel%