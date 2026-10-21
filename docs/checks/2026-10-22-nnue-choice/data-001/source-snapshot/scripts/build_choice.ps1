param(
    [switch]$Test,
    [ValidatePattern('^[A-Za-z0-9_-]+$')][string]$BuildDirectory = 'build-m3-choice'
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
$batchFile = Join-Path $buildDir 'compile-choice.cmd'
$units = @('board', 'search', 'evaluation', 'transposition', 'see', 'neural', 'nnue')
$commands = @('@echo off', 'chcp 65001 >nul', "call `"$vcvars`" >nul", 'if errorlevel 1 exit /b 1', "cd /d `"$repoRoot`"")
foreach ($unit in $units) {
    $commands += "cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude /c src\$unit.cpp /Fo$BuildDirectory\$unit.obj"
    $commands += 'if errorlevel 1 exit /b 1'
}
$objects = ($units | ForEach-Object { "$BuildDirectory\$_.obj" }) -join ' '
$commands += "cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude scripts\collect_choice_leaves.cpp $objects /Fo$BuildDirectory\collect_choice_leaves.obj /Fe$BuildDirectory\regicide_choice_data.exe"
$commands += 'if errorlevel 1 exit /b 1'
$commands += "cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /Iinclude tests\choice_leaf_tests.cpp $objects /Fo$BuildDirectory\choice_leaf_tests.obj /Fe$BuildDirectory\regicide_choice_leaf_tests.exe"
$commands += 'exit /b %errorlevel%'
[System.IO.File]::WriteAllText($batchFile, ($commands -join "`r`n"), [System.Text.UTF8Encoding]::new($false))
& $env:ComSpec /d /c $batchFile
if ($LASTEXITCODE -ne 0) { throw 'Choice collector build failed.' }
if ($Test) {
    & (Join-Path $buildDir 'regicide_choice_leaf_tests.exe')
    if ($LASTEXITCODE -ne 0) { throw 'Choice support tests failed.' }
}
