@echo off
chcp 65001 >nul
call "C:\Program Files\Microsoft Visual Studio\18\Community\VC\Auxiliary\Build\vcvars64.bat" >nul
if errorlevel 1 exit /b 1
cd /d "$REPO"
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /GL- /Iinclude /Itests /c tests\reference_evaluation_v054.cpp /FAs /Fa"$REPO\build-shield-profile\reference_evaluation_v054.asm" /Fo"$REPO\build-shield-profile\reference_assembly.obj"
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /GL- /Iinclude /Itests /c src\evaluation.cpp /FAs /Fa"$REPO\build-shield-profile\candidate_evaluation.asm" /Fo"$REPO\build-shield-profile\candidate_assembly.obj"
exit /b %errorlevel%