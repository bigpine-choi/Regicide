param([Parameter(Mandatory=$true)][string]$CandidateCommit)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $repoRoot
if ((& git rev-parse HEAD) -ne $CandidateCommit -or (& git status --porcelain)) {
    throw 'Run from the clean, frozen candidate checkout.'
}
$outputDirectory = Join-Path $repoRoot 'build/task06-evidence'
New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
foreach ($name in @('development100','heldout100')) {
    foreach ($extension in @('json','pgn','log')) {
        if (Test-Path -LiteralPath (Join-Path $outputDirectory "$name.$extension")) {
            throw "Preserve previous output: $name.$extension already exists."
        }
    }
}
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class RegicideStrengthWakeGuard {
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern uint SetThreadExecutionState(uint flags);
}
'@
$previousState = [RegicideStrengthWakeGuard]::SetThreadExecutionState([uint32]2147483649)
if ($previousState -eq 0) { throw 'Temporary system wake requirement failed.' }
$runState = [ordered]@{
    started = (Get-Date).ToString('o')
    prior_state = $previousState
    requested_state = 'ES_CONTINUOUS | ES_SYSTEM_REQUIRED'
    display_required = $false
    persistent_power_policy_changed = $false
    candidate_commit = $CandidateCommit
    bundles = @()
    finished = $null
    restore_requested_state = $null
    restore_succeeded = $false
}
$comparisonExitCode = 2
try {
    foreach ($bundle in @(
        @{name='development100'; openings='tests/data/comparison_openings.json'},
        @{name='heldout100'; openings='tests/data/heldout_openings_2026-10-17.json'}
    )) {
        $record = [ordered]@{name=$bundle.name; started=(Get-Date).ToString('o'); finished=$null; exit_code=$null}
        $comparisonArguments = @(
            'scripts/compare_engines.py',
            'build-strength-baseline/source/build/regicide.exe', 'build/regicide.exe',
            '--movetime', '100', '--openings', $bundle.openings,
            '--pgn', "build/task06-evidence/$($bundle.name).pgn",
            '--baseline-commit', 'a31091548033d538e01d2c194f05bada403d5263',
            '--candidate-commit', $CandidateCommit
        )
        & .venv-tests/Scripts/python.exe @comparisonArguments *> "build/task06-evidence/$($bundle.name).log"
        $comparisonExitCode = $LASTEXITCODE
        $record.exit_code = $comparisonExitCode
        $record.finished = (Get-Date).ToString('o')
        $runState.bundles += $record
        Get-Content -LiteralPath "build/task06-evidence/$($bundle.name).log" -Tail 4
        if ($comparisonExitCode -ne 0) { break }
    }
} finally {
    $restoreState = [uint32]($previousState -bor [uint32]2147483648)
    $restored = [RegicideStrengthWakeGuard]::SetThreadExecutionState($restoreState)
    $runState.restore_requested_state = $restoreState
    $runState.restore_succeeded = $restored -ne 0
    $runState.finished = (Get-Date).ToString('o')
    $runState | ConvertTo-Json -Depth 5 | Set-Content -Encoding utf8 build/task06-evidence/wake-guard.json
}
exit $comparisonExitCode
