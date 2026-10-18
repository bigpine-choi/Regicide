$ErrorActionPreference='Stop'
$repoRoot=Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location -LiteralPath $repoRoot
$pythonPath='$BASELINE_REPO\.venv-tests\Scripts\python.exe'
$baseline=Join-Path $repoRoot 'build\lmr-validation\regicide-v057.exe'
$candidate=Join-Path $repoRoot 'build\regicide.exe'
$baseCommit='5b5b5deedbef96133836609ec00229c7a548a15a'
$candidateCommit='157305d804bc1828641a7e8d9b9b793b7746bcc3'
foreach($depth in @(3,4,5)){
    $label='off-equivalence-depth'+$depth
    & $pythonPath scripts/lmr_experiment.py benchmark --telemetry ('build/lmr-validation/'+$label+'-lmr.json') -- $baseline $candidate --output ('build/lmr-validation/'+$label+'.json') --depth $depth --repeats 3 --warmup 1 --seed 20261018 --baseline-commit $baseCommit --candidate-commit $candidateCommit --baseline-options '{"Hash":16,"Move Overhead":20}' --candidate-options '{"Hash":16,"Move Overhead":20,"UseLMR":false}' --require-same-search *> ('build/lmr-validation/'+$label+'.log')
    if($LASTEXITCODE -ne 0){throw ($label+' failed')}
    Write-Output ('PASS '+$label)
}
foreach($depth in @(5,6)){
    $label='on-cost-depth'+$depth
    & $pythonPath scripts/lmr_experiment.py benchmark --telemetry ('build/lmr-validation/'+$label+'-lmr.json') -- $candidate $candidate --output ('build/lmr-validation/'+$label+'.json') --depth $depth --repeats 3 --warmup 1 --seed 20261018 --baseline-commit $candidateCommit --candidate-commit $candidateCommit --baseline-options '{"Hash":16,"Move Overhead":20,"UseLMR":false}' --candidate-options '{"Hash":16,"Move Overhead":20,"UseLMR":true}' --allow-score-differences *> ('build/lmr-validation/'+$label+'.log')
    if($LASTEXITCODE -ne 0){throw ($label+' failed')}
    Write-Output ('PASS '+$label)
}
exit 0
