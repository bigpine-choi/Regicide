@echo off
call "C:\Program Files\Microsoft Visual Studio\18\Community\VC\Auxiliary\Build\vcvars64.bat" >nul
cd /d "C:\Users\hanso\.codex\.chatgpt-projects\g-p-6a8b058617088191ac7cb44895558f31\Regicide"
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude tests\see_tests.cpp build-see-native\board.obj build-see-cost\candidate_see.obj /Fobuild-see-cost\see_tests.obj /Febuild-see-cost\see_tests.exe
exit /b %errorlevel%