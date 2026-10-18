param(
    [ValidatePattern('^[A-Za-z0-9_-]+$')][string]$BuildDirectory = 'build-shield-profile'
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
$batchFile = Join-Path $buildDir 'compile-profile.cmd'
# Every source has its own object. Explicit /GL- disables whole-program
# optimization, preserving real repeated calls into both evaluation versions.
$batch = @"
@echo off
chcp 65001 >nul
call "$vcvars" >nul
if errorlevel 1 exit /b 1
cd /d "$repoRoot"
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /GL- /Iinclude /Itests /c src\board.cpp /Fo"$buildDir\board.obj"
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /GL- /Iinclude /Itests /c src\evaluation.cpp /Fo"$buildDir\evaluation.obj"
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /GL- /Iinclude /Itests /c tests\reference_evaluation_v054.cpp /Fo"$buildDir\reference_evaluation_v054.obj"
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /WX /permissive- /utf-8 /O2 /DNDEBUG /GL- /Iinclude /Itests /c scripts\profile_evaluation.cpp /Fo"$buildDir\profile_evaluation.obj"
if errorlevel 1 exit /b 1
link /nologo "$buildDir\profile_evaluation.obj" "$buildDir\board.obj" "$buildDir\evaluation.obj" "$buildDir\reference_evaluation_v054.obj" /OUT:"$buildDir\profile_evaluation.exe"
exit /b %errorlevel%
"@
[System.IO.File]::WriteAllText($batchFile, $batch, [System.Text.UTF8Encoding]::new($false))
& $env:ComSpec /d /c $batchFile
if ($LASTEXITCODE -ne 0) { throw 'Evaluation profile build failed.' }
Write-Output (Join-Path $buildDir 'profile_evaluation.exe')
