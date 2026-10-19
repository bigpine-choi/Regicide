param(
    [switch]$Test,
    [ValidatePattern('^[A-Za-z0-9_-]+$')][string]$BuildDirectory = 'build'
)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
if (!(Test-Path -LiteralPath $vswhere)) { throw 'Visual Studio C++ Build Tools are required.' }
$installation = & $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (!$installation) { throw 'Install the Visual Studio Desktop development with C++ workload.' }
$vcvars = Join-Path $installation 'VC\Auxiliary\Build\vcvars64.bat'
$buildDir = Join-Path $repoRoot $BuildDirectory
New-Item -ItemType Directory -Path $buildDir -Force | Out-Null
# Keep compilation in one Windows shell so the compiler inherits its SDK environment.
$batchFile = Join-Path $buildDir 'compile.cmd'
$batch = @"
@echo off
chcp 65001 >nul
call "$vcvars" >nul
if errorlevel 1 exit /b 1
cd /d "$repoRoot"
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude /c src\board.cpp /Fobuild\board.obj
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude /c src\search.cpp /Fobuild\search.obj
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude /c src\evaluation.cpp /Fobuild\evaluation.obj
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude /c src\neural.cpp /Fobuild\neural.obj
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude /c src\transposition.cpp /Fobuild\transposition.obj
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude /c src\see.cpp /Fobuild\see.obj
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude /c src\uci.cpp /Fobuild\uci.obj
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude src\main.cpp build\board.obj build\search.obj build\neural.obj build\evaluation.obj build\transposition.obj build\see.obj build\uci.obj /Fobuild\main.obj /Febuild\regicide.exe
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude tests\rules_tests.cpp build\board.obj /Fobuild\rules_tests.obj /Febuild\regicide_tests.exe
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude tests\search_tests.cpp build\board.obj build\search.obj build\neural.obj build\evaluation.obj build\transposition.obj build\see.obj /Fobuild\search_tests.obj /Febuild\regicide_search_tests.exe
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude tests\evaluation_tests.cpp build\board.obj build\evaluation.obj /Fobuild\evaluation_tests.obj /Febuild\regicide_evaluation_tests.exe
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude tests\board_state_tests.cpp build\board.obj /Fobuild\board_state_tests.obj /Febuild\regicide_board_state_tests.exe
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude tests\transposition_tests.cpp build\board.obj build\search.obj build\neural.obj build\evaluation.obj build\transposition.obj build\see.obj /Fobuild\transposition_tests.obj /Febuild\regicide_transposition_tests.exe
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude tests\see_tests.cpp build\board.obj build\search.obj build\neural.obj build\evaluation.obj build\transposition.obj build\see.obj /Fobuild\see_tests.obj /Febuild\regicide_see_tests.exe
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude tests\neural_tests.cpp build\board.obj build\search.obj build\evaluation.obj build\neural.obj build\transposition.obj build\see.obj /Fobuild\neural_tests.obj /Febuild\regicide_neural_tests.exe
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude scripts\evaluation_benchmark.cpp build\board.obj build\evaluation.obj build\neural.obj /Fobuild\evaluation_benchmark.obj /Febuild\regicide_evaluation_benchmark.exe
exit /b %errorlevel%
"@
$batch = $batch.Replace('build\', ($BuildDirectory + '\'))
[System.IO.File]::WriteAllText($batchFile, $batch, [System.Text.UTF8Encoding]::new($false))
& $env:ComSpec /d /c $batchFile
if ($LASTEXITCODE -ne 0) { throw 'C++ build failed.' }
if ($Test) {
    & (Join-Path $buildDir 'regicide_tests.exe')
    if ($LASTEXITCODE -ne 0) { throw 'Rules tests failed.' }
    & (Join-Path $buildDir 'regicide_search_tests.exe')
    if ($LASTEXITCODE -ne 0) { throw 'Search tests failed.' }
    & (Join-Path $buildDir 'regicide_evaluation_tests.exe')
    if ($LASTEXITCODE -ne 0) { throw 'Evaluation tests failed.' }
    & (Join-Path $buildDir 'regicide_board_state_tests.exe')
    if ($LASTEXITCODE -ne 0) { throw 'Board state tests failed.' }
    & (Join-Path $buildDir 'regicide_transposition_tests.exe')
    if ($LASTEXITCODE -ne 0) { throw 'Transposition tests failed.' }
    & (Join-Path $buildDir 'regicide_see_tests.exe')
    if ($LASTEXITCODE -ne 0) { throw 'SEE tests failed.' }
    & (Join-Path $buildDir 'regicide_neural_tests.exe')
    if ($LASTEXITCODE -ne 0) { throw 'Neural tests failed.' }
}
