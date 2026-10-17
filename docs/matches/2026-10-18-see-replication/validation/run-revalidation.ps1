$ErrorActionPreference='Stop'
$env:PYTHONIOENCODING='utf-8'
$repoPath = (Get-Location).Path
$validationDir = Join-Path $repoPath 'build/see-clean-evidence'
$pythonPath = Join-Path $repoPath '.venv-tests/Scripts/python.exe'
$candidatePath = Join-Path $repoPath 'build-see-native/regicide.exe'
$baselinePath = Join-Path $repoPath 'build-see-baseline/source/build/regicide.exe'
$godotPath = 'C:/Users/hanso/Tools/Godot/Godot_v4.7.2-stable_win64_console.exe'
$results = @()
function Run-Validation {
  param([string]$Label, [string]$Executable, [string[]]$Arguments)
  $logPath = Join-Path $validationDir ('validation-' + $Label + '.log')
  if (Test-Path -LiteralPath $logPath) { throw ('Refusing to overwrite log ' + $logPath) }
  $started = (Get-Date).ToUniversalTime().ToString('o')
  Write-Output ('START ' + $Label + ' ' + $started)
  & $Executable @Arguments *> $logPath
  $code = $LASTEXITCODE
  $ended = (Get-Date).ToUniversalTime().ToString('o')
  $script:results += [pscustomobject]@{label=$Label; executable=$Executable; arguments=$Arguments; started_utc=$started; ended_utc=$ended; exit_code=$code; log=$logPath}
  $script:results | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $validationDir 'validation-run-state.json') -Encoding utf8
  Write-Output ('END ' + $Label + ' exit=' + $code + ' ' + $ended)
}
foreach ($name in @('tests','search_tests','evaluation_tests','board_state_tests','transposition_tests','see_tests')) {
  Run-Validation -Label ('native-' + $name) -Executable (Join-Path $repoPath ('build-see-native/regicide_' + $name + '.exe')) -Arguments @()
}
foreach ($name in @('differential','engine_integration','game_runner_tests','compare_engines_tests','benchmark_search_tests','human_play_tests','handicap_play_tests','game_archive_tests','godot_bootstrap_tests')) {
  Run-Validation -Label $name -Executable $pythonPath -Arguments @(('tests/' + $name + '.py'), $candidatePath)
}
foreach ($name in @('godot_integration_tests','handicap_godot_tests','archive_godot_tests')) {
  Run-Validation -Label $name -Executable $pythonPath -Arguments @(('tests/' + $name + '.py'), '--godot', $godotPath, '--binary', $candidatePath, '--output', (Join-Path $validationDir ('scene-' + $name)))
}
Run-Validation -Label baseline-native-rules -Executable (Join-Path $repoPath 'build-see-baseline/source/build/regicide_tests.exe') -Arguments @()
Run-Validation -Label baseline-differential -Executable $pythonPath -Arguments @('tests/differential.py', $baselinePath)
$hashes = [pscustomobject]@{candidate_sha256=(Get-FileHash -Algorithm SHA256 -LiteralPath $candidatePath).Hash.ToLowerInvariant(); baseline_sha256=(Get-FileHash -Algorithm SHA256 -LiteralPath $baselinePath).Hash.ToLowerInvariant(); completed_utc=(Get-Date).ToUniversalTime().ToString('o'); failed=@($results | Where-Object exit_code -ne 0).Count}
$hashes | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $validationDir 'validation-summary.json') -Encoding utf8
Write-Output ($hashes | ConvertTo-Json)
if ($hashes.failed -gt 0) { exit 1 }
