$ErrorActionPreference='Stop'
$repoRoot=Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location -LiteralPath $repoRoot
$pythonPath='$BASELINE_REPO\.venv-tests\Scripts\python.exe'
$baseline=Join-Path $repoRoot 'build\pawn-opt-validation\regicide-v056.exe'
$candidate=Join-Path $repoRoot 'build\regicide.exe'
$baseCommit='09090d0c35aed9ae1643fa1785203ac5b87e678e'
$candidateCommit='4ee6dde974c6e250c93d52d2ae812586d92f7dce'
$configs=@(
    @{label='default-depth3';depth=3;repeats=5;shield=$true;structure=$true},
    @{label='default-depth4';depth=4;repeats=5;shield=$true;structure=$true},
    @{label='default-depth5';depth=5;repeats=3;shield=$true;structure=$true},
    @{label='structure-off-depth3';depth=3;repeats=3;shield=$true;structure=$false},
    @{label='shield-off-depth3';depth=3;repeats=3;shield=$false;structure=$true},
    @{label='both-off-depth3';depth=3;repeats=3;shield=$false;structure=$false}
)
foreach($config in $configs){
    $options=@{Hash=16;'Move Overhead'=20;UsePawnShield=$config.shield;UsePawnStructure=$config.structure}|ConvertTo-Json -Compress
    & $pythonPath scripts/benchmark_search.py $baseline $candidate --output ('build/pawn-opt-validation/'+$config.label+'.json') --depth $config.depth --repeats $config.repeats --warmup 1 --seed 20261018 --baseline-commit $baseCommit --candidate-commit $candidateCommit --baseline-options $options --candidate-options $options --require-same-search *> ('build/pawn-opt-validation/'+$config.label+'.log')
    if($LASTEXITCODE -ne 0){throw ($config.label+' comparison failed')}
    Write-Output ('PASS '+$config.label)
}
