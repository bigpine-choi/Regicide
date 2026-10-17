@echo off
call "C:\Program Files\Microsoft Visual Studio\18\Community\VC\Auxiliary\Build\vcvars64.bat" >nul
cd /d "C:\Users\hanso\.codex\.chatgpt-projects\g-p-6a8b058617088191ac7cb44895558f31\Regicide"
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude build-see-cost\profile_see_search.cpp build-see-cost\profile_see_wrapper.cpp build-see-native\board.obj build-see-native\search.obj build-see-native\evaluation.obj build-see-native\transposition.obj build-see-cost\reference_see.obj /Fobuild-see-cost\ /Febuild-see-cost\profile_see_search.exe
exit /b %errorlevel%