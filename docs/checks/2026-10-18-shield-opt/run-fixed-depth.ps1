$ErrorActionPreference='Stop'
$repoRoot=Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location -LiteralPath $repoRoot
$pythonPath='$BASELINE_REPO\.venv-tests\Scripts\python.exe'
$baseline=Join-Path $repoRoot 'build\shield-opt-validation\regicide-v054.exe'
$candidate=Join-Path $repoRoot 'build\regicide.exe'
$baseCommit='07321d8b27b95dad9a1fc66c9170ab8085829868'
$candidateCommit='9dce742339428a4d3a449494f7284b6f66d9d7d6'
foreach($config in @(@{label='on-depth3';depth=3;repeats=5;shield=$true},@{label='on-depth4';depth=4;repeats=5;shield=$true},@{label='off-depth3';depth=3;repeats=3;shield=$false})){
    $optionObject=@{Hash=16;'Move Overhead'=20;UsePawnShield=$config.shield}
    $options=$optionObject | ConvertTo-Json -Compress
    & $pythonPath scripts/benchmark_search.py $baseline $candidate --output ('build/shield-opt-validation/'+$config.label+'.json') --depth $config.depth --repeats $config.repeats --warmup 1 --seed 20261018 --baseline-commit $baseCommit --candidate-commit $candidateCommit --baseline-options $options --candidate-options $options --require-same-search *> ('build/shield-opt-validation/'+$config.label+'.log')
    if($LASTEXITCODE -ne 0){Get-Content ('build/shield-opt-validation/'+$config.label+'.log') -Tail 20;throw ($config.label+' comparison failed')}
    Write-Output ('PASS '+$config.label)
}
