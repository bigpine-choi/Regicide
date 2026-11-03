param(
    [ValidatePattern('^[0-9a-f]{40}$')][string]$CandidateCommit,
    [string]$GodotPath = 'C:/Users/hanso/Tools/Godot/Godot_v4.7.2-stable_win64_console.exe'
)
$ErrorActionPreference = 'Stop'
$env:PYTHONIOENCODING = 'utf-8'
$repoPath = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location -LiteralPath $repoPath
$validationDir = $PSScriptRoot
$pythonPath = Join-Path $repoPath '.venv-tests/Scripts/python.exe'
$candidatePath = Join-Path $repoPath 'build-see-cost/regicide.exe'
$baselinePath = Join-Path $repoPath 'build-see-native/regicide.exe'
$freeze = Get-Content -LiteralPath (Join-Path $repoPath 'tests/data/see_cost_freeze_2026-10-18.json') -Raw | ConvertFrom-Json
if (-not $CandidateCommit) { $CandidateCommit = $freeze.candidate.commit }
if ($CandidateCommit -notmatch '^[0-9a-f]{40}$') { throw 'Supply the frozen candidate source commit.' }
if ($freeze.candidate.commit -and $freeze.candidate.commit -ne $CandidateCommit) { throw 'Candidate commit differs from the freeze manifest.' }
& git cat-file -e "${CandidateCommit}^{commit}"
if ($LASTEXITCODE -ne 0) { throw 'Candidate source commit is unavailable.' }
$binaryHashes = [ordered]@{
    baseline = (Get-FileHash -LiteralPath $baselinePath -Algorithm SHA256).Hash.ToLowerInvariant()
    candidate = (Get-FileHash -LiteralPath $candidatePath -Algorithm SHA256).Hash.ToLowerInvariant()
}
if ($binaryHashes.baseline -ne '2ba41c8303f266149323b55186f5c6a806c33f73d529d6911d6ca4b33bfa53db' -or
    ($freeze.candidate.sha256 -and $binaryHashes.candidate -ne $freeze.candidate.sha256)) {
    throw 'Validation binaries differ from the frozen inputs.'
}
$nativeNames = @('tests','search_tests','evaluation_tests','board_state_tests','transposition_tests','see_tests')
$pythonNames = @('differential','engine_integration','game_runner_tests','compare_engines_tests',
    'benchmark_search_tests','human_play_tests','handicap_play_tests','game_archive_tests','godot_bootstrap_tests')
$godotNames = @('godot_integration_tests','handicap_godot_tests','archive_godot_tests')
$labels = @($nativeNames | ForEach-Object { 'native-' + $_ }) + $pythonNames + $godotNames +
    @('baseline-native-rules','baseline-differential','frozen-engine-source')
foreach ($label in $labels) {
    $targetLog = Join-Path $validationDir ('validation-' + $label + '.log')
    if (Test-Path -LiteralPath $targetLog) { throw "Refusing to overwrite prior gate log: $targetLog" }
}
$statePath = Join-Path $validationDir 'validation-run-state.json'
foreach ($path in @($statePath, (Join-Path $validationDir 'validation-summary.json'))) {
    if (Test-Path -LiteralPath $path) { throw "Refusing to overwrite prior validation evidence: $path" }
}
foreach ($executable in @($pythonPath,$GodotPath,$baselinePath,$candidatePath,
        (Join-Path $repoPath 'build-see-native/regicide_tests.exe')) +
        @($nativeNames | ForEach-Object { Join-Path $repoPath ('build-see-cost/regicide_' + $_ + '.exe') })) {
    if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) { throw "Missing validation executable: $executable" }
}
$state = [ordered]@{schema_version=1; engine_source_commit=$CandidateCommit; binary_sha256=$binaryHashes;
    started_utc=[DateTime]::UtcNow.ToString('o'); completed_utc=$null; suites=@()}
function Save-ValidationState {
    $state | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $statePath -Encoding utf8
}
function Run-Validation {
    param([string]$Label, [string]$Executable, [string[]]$Arguments)
    $logPath = Join-Path $validationDir ('validation-' + $Label + '.log')
    if (Test-Path -LiteralPath $logPath) { throw "Refusing to overwrite gate log: $logPath" }
    $started = [DateTime]::UtcNow.ToString('o')
    Write-Output "START $Label $started"
    $code = 2
    try {
        & $Executable @Arguments *> $logPath
        $code = $LASTEXITCODE
    } catch {
        $_ | Out-String | Add-Content -LiteralPath $logPath -Encoding utf8
    }
    $ended = [DateTime]::UtcNow.ToString('o')
    $state.suites += [pscustomobject]@{label=$Label; executable=$Executable; arguments=$Arguments;
        started_utc=$started; ended_utc=$ended; exit_code=$code; log=$logPath}
    Save-ValidationState
    Write-Output "END $Label exit=$code $ended"
}
Save-ValidationState
foreach ($name in $nativeNames) {
    Run-Validation -Label ('native-' + $name) -Executable (Join-Path $repoPath ('build-see-cost/regicide_' + $name + '.exe')) -Arguments @()
}
foreach ($name in $pythonNames) {
    Run-Validation -Label $name -Executable $pythonPath -Arguments @(('tests/' + $name + '.py'), $candidatePath)
}
foreach ($name in $godotNames) {
    Run-Validation -Label $name -Executable $pythonPath -Arguments @(('tests/' + $name + '.py'), '--godot', $GodotPath,
        '--binary', $candidatePath, '--output', (Join-Path $validationDir ('scene-' + $name)))
}
Run-Validation -Label 'baseline-native-rules' -Executable (Join-Path $repoPath 'build-see-native/regicide_tests.exe') -Arguments @()
Run-Validation -Label 'baseline-differential' -Executable $pythonPath -Arguments @('tests/differential.py', $baselinePath)
$state.completed_utc = [DateTime]::UtcNow.ToString('o')
Save-ValidationState
& $pythonPath (Join-Path $validationDir 'finalize-validation.py') --candidate-commit $CandidateCommit
exit $LASTEXITCODE
