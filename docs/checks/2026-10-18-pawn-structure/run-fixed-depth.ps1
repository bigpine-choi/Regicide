$ErrorActionPreference='Stop'
$repoRoot=Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location -LiteralPath $repoRoot
$pythonPath='$BASELINE_REPO\.venv-tests\Scripts\python.exe'
$old=Join-Path $repoRoot 'build\pawn-structure-validation\regicide-v055.exe'
$new=Join-Path $repoRoot 'build\regicide.exe'
$oldCommit='585710724e93329a8f8baddfbc82724db4a4ce2a'
$newCommit='132f45451d0af68168cfe15507a3fdc069c86d9c'
foreach($depth in @(3,4)) {
    $baseOptions=@{Hash=16;'Move Overhead'=20}|ConvertTo-Json -Compress
    $newOptions=@{Hash=16;'Move Overhead'=20;UsePawnStructure=$false}|ConvertTo-Json -Compress
    & $pythonPath scripts/benchmark_search.py $old $new --output "build/pawn-structure-validation/legacy-depth$depth.json" --depth $depth --repeats 3 --warmup 1 --seed 20261018 --baseline-commit $oldCommit --candidate-commit $newCommit --baseline-options $baseOptions --candidate-options $newOptions --require-same-search *> "build/pawn-structure-validation/legacy-depth$depth.log"
    if($LASTEXITCODE -ne 0){throw "legacy depth $depth failed"}
    Write-Output "PASS legacy depth $depth"
}
$off=@{Hash=16;'Move Overhead'=20;UsePawnStructure=$false}|ConvertTo-Json -Compress
$on=@{Hash=16;'Move Overhead'=20;UsePawnStructure=$true}|ConvertTo-Json -Compress
& $pythonPath scripts/benchmark_search.py $new $new --output build/pawn-structure-validation/structure-depth3.json --depth 3 --repeats 3 --warmup 1 --seed 20261018 --baseline-commit $newCommit --candidate-commit $newCommit --baseline-options $off --candidate-options $on --allow-score-differences *> build/pawn-structure-validation/structure-depth3.log
if($LASTEXITCODE -ne 0){throw 'structure depth3 failed'}
Write-Output 'PASS structure depth3 (intentional score differences permitted)'
