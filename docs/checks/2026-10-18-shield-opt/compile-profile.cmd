@echo off
chcp 65001 >nul
call "C:\Program Files\Microsoft Visual Studio\18\Community\VC\Auxiliary\Build\vcvars64.bat" >nul
if errorlevel 1 exit /b 1
cd /d "$REPO"
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /GL- /Iinclude /Itests /c src\board.cpp /Fo"$REPO\build-shield-profile\board.obj"
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /GL- /Iinclude /Itests /c src\evaluation.cpp /Fo"$REPO\build-shield-profile\evaluation.obj"
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /GL- /Iinclude /Itests /c tests\reference_evaluation_v054.cpp /Fo"$REPO\build-shield-profile\reference_evaluation_v054.obj"
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /GL- /Iinclude /Itests /c scripts\profile_evaluation.cpp /Fo"$REPO\build-shield-profile\profile_evaluation.obj"
if errorlevel 1 exit /b 1
link /nologo "$REPO\build-shield-profile\profile_evaluation.obj" "$REPO\build-shield-profile\board.obj" "$REPO\build-shield-profile\evaluation.obj" "$REPO\build-shield-profile\reference_evaluation_v054.obj" /OUT:"$REPO\build-shield-profile\profile_evaluation.exe"
exit /b %errorlevel%