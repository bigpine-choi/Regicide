param(
    [switch]$Test,
    [switch]$EvaluationProbe,
    [switch]$NeuralProfile,
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
$probeBuild = if ($EvaluationProbe) {
@"
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude scripts\inspect_evaluation.cpp build\board.obj build\search.obj build\evaluation.obj build\transposition.obj build\see.obj build\neural.obj build\nnue.obj /Fobuild\inspect_evaluation.obj /Febuild\regicide_evaluation_probe.exe
if errorlevel 1 exit /b 1
"@
} else { '' }
$neuralProfileBuild = if ($NeuralProfile) {
@"
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /GL- /DNDEBUG /Iinclude /Itests scripts\profile_neural.cpp build\reference_neural_v063.obj build\board.obj build\evaluation.obj build\search.obj build\transposition.obj build\see.obj build\neural.obj build\nnue.obj /Fobuild\profile_neural.obj /Febuild\regicide_profile_neural.exe
if errorlevel 1 exit /b 1
"@
} else { '' }
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
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude /c tests\reference_evaluation_v054.cpp /Fobuild\reference_evaluation_v054.obj
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude /c tests\reference_evaluation_v056.cpp /Fobuild\reference_evaluation_v056.obj
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude /c src\transposition.cpp /Fobuild\transposition.obj
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude /c src\see.cpp /Fobuild\see.obj
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude /c src\neural.cpp /Fobuild\neural.obj
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude /c src\nnue.cpp /Fobuild\nnue.obj
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /GL- /DNDEBUG /Iinclude /c tests\reference_neural_v063.cpp /Fobuild\reference_neural_v063.obj
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude /c src\uci.cpp /Fobuild\uci.obj
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude src\main.cpp build\board.obj build\search.obj build\evaluation.obj build\transposition.obj build\see.obj build\neural.obj build\nnue.obj build\uci.obj /Fobuild\main.obj /Febuild\regicide.exe
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude tests\rules_tests.cpp build\board.obj /Fobuild\rules_tests.obj /Febuild\regicide_tests.exe
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude tests\search_tests.cpp build\board.obj build\search.obj build\evaluation.obj build\transposition.obj build\see.obj build\neural.obj build\nnue.obj /Fobuild\search_tests.obj /Febuild\regicide_search_tests.exe
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude tests\queen_defense_tests.cpp build\board.obj build\search.obj build\evaluation.obj build\transposition.obj build\see.obj build\neural.obj build\nnue.obj /Fobuild\queen_defense_tests.obj /Febuild\regicide_queen_defense_tests.exe
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude tests\forcing_tests.cpp build\board.obj build\search.obj build\evaluation.obj build\transposition.obj build\see.obj build\neural.obj build\nnue.obj /Fobuild\forcing_tests.obj /Febuild\regicide_forcing_tests.exe
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude tests\evaluation_tests.cpp build\board.obj build\evaluation.obj build\reference_evaluation_v054.obj build\reference_evaluation_v056.obj /Fobuild\evaluation_tests.obj /Febuild\regicide_evaluation_tests.exe
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude tests\board_state_tests.cpp build\board.obj /Fobuild\board_state_tests.obj /Febuild\regicide_board_state_tests.exe
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude tests\transposition_tests.cpp build\board.obj build\search.obj build\evaluation.obj build\transposition.obj build\see.obj build\neural.obj build\nnue.obj /Fobuild\transposition_tests.obj /Febuild\regicide_transposition_tests.exe
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude tests\see_tests.cpp build\board.obj build\search.obj build\evaluation.obj build\transposition.obj build\see.obj build\neural.obj build\nnue.obj /Fobuild\see_tests.obj /Febuild\regicide_see_tests.exe
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude tests\neural_tests.cpp build\reference_neural_v063.obj build\board.obj build\evaluation.obj build\neural.obj build\nnue.obj /Fobuild\neural_tests.obj /Febuild\regicide_neural_tests.exe
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude tests\nnue_tests.cpp build\board.obj build\search.obj build\evaluation.obj build\transposition.obj build\see.obj build\neural.obj build\nnue.obj /Fobuild\regicide_nnue_tests.obj /Febuild\regicide_nnue_tests.exe
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude tests\neural_model_tests.cpp build\board.obj build\search.obj build\evaluation.obj build\transposition.obj build\see.obj build\neural.obj build\nnue.obj /Fobuild\regicide_neural_model_tests.obj /Febuild\regicide_neural_model_tests.exe
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude tests\leaf_observer_tests.cpp build\board.obj build\search.obj build\evaluation.obj build\transposition.obj build\see.obj build\neural.obj build\nnue.obj /Fobuild\regicide_leaf_observer_tests.obj /Febuild\regicide_leaf_observer_tests.exe
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude scripts\collect_search_leaves.cpp build\board.obj build\search.obj build\evaluation.obj build\transposition.obj build\see.obj build\neural.obj build\nnue.obj /Fobuild\collect_search_leaves.obj /Febuild\regicide_leaf_data.exe
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude scripts\evaluation_benchmark.cpp build\board.obj build\search.obj build\evaluation.obj build\transposition.obj build\see.obj build\neural.obj build\nnue.obj /Fobuild\regicide_evaluation_benchmark.obj /Febuild\regicide_evaluation_benchmark.exe
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude scripts\neural_incremental_benchmark.cpp build\board.obj build\search.obj build\evaluation.obj build\transposition.obj build\see.obj build\neural.obj build\nnue.obj /Fobuild\regicide_neural_incremental_benchmark.obj /Febuild\regicide_neural_incremental_benchmark.exe
if errorlevel 1 exit /b 1
$probeBuild
$neuralProfileBuild
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
    & (Join-Path $buildDir 'regicide_queen_defense_tests.exe')
    if ($LASTEXITCODE -ne 0) { throw 'Queen defense tests failed.' }
    & (Join-Path $buildDir 'regicide_forcing_tests.exe')
    if ($LASTEXITCODE -ne 0) { throw 'Forcing continuation tests failed.' }
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
    & (Join-Path $buildDir 'regicide_nnue_tests.exe')
    if ($LASTEXITCODE -ne 0) { throw 'NNUE tests failed.' }
    & (Join-Path $buildDir 'regicide_neural_model_tests.exe')
    if ($LASTEXITCODE -ne 0) { throw 'NNUE model tests failed.' }
    & (Join-Path $buildDir 'regicide_leaf_observer_tests.exe')
    if ($LASTEXITCODE -ne 0) { throw 'Leaf observer tests failed.' }
}
