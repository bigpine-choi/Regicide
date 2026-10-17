param([Parameter(Mandatory=$true)][ValidatePattern('^[0-9a-fA-F]{40}$')][string]$CandidateCommit)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $repoRoot
$baselineCommit = '8820094588cddadff39bc7cac7c19f92ed522822'
$python = Join-Path $repoRoot '.venv-tests/Scripts/python.exe'
$baseline = Join-Path $repoRoot 'build-pvs-baseline/source/build/regicide.exe'
$candidate = Join-Path $repoRoot 'build-pvs-native/regicide.exe'
$outputDirectory = Join-Path $repoRoot 'build/pvs-evidence'

function Assert-FrozenCheckout {
    $head = & git rev-parse --verify HEAD
    if ($LASTEXITCODE -ne 0 -or $head -ne $CandidateCommit -or (& git status --porcelain)) {
        throw 'Run from the clean, frozen candidate checkout with the exact CandidateCommit.'
    }
}
Assert-FrozenCheckout
& git cat-file -e "${baselineCommit}^{commit}"
if ($LASTEXITCODE -ne 0) { throw 'The baseline source commit is unavailable.' }
foreach ($path in @($python, $baseline, $candidate)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Required file missing: $path" }
}
$hashes = [ordered]@{
    baseline = (Get-FileHash -LiteralPath $baseline -Algorithm SHA256).Hash.ToLowerInvariant()
    candidate = (Get-FileHash -LiteralPath $candidate -Algorithm SHA256).Hash.ToLowerInvariant()
}
$steps = @(
    @{name='cost-depth3'; kind='cost'; depth=3; control=$false},
    @{name='cost-depth4'; kind='cost'; depth=4; control=$false},
    @{name='control-depth3'; kind='cost'; depth=3; control=$true},
    @{name='control-depth4'; kind='cost'; depth=4; control=$true},
    @{name='time100'; kind='match'; movetime=100},
    @{name='time1000'; kind='match'; movetime=1000}
)
New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
foreach ($name in @('run-state') + @($steps | ForEach-Object { $_.name })) {
    foreach ($extension in @('json','pgn','log')) {
        if (Test-Path -LiteralPath (Join-Path $outputDirectory "$name.$extension")) {
            throw "Preserve previous output: $name.$extension already exists."
        }
    }
}
if (-not ('RegicidePvsWakeGuard' -as [type])) {
    Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class RegicidePvsWakeGuard {
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern uint SetThreadExecutionState(uint flags);
}
'@
}
$runState = [ordered]@{
    schema_version = 1
    started_utc = [DateTime]::UtcNow.ToString('o')
    finished_utc = $null
    candidate_commit = $CandidateCommit
    baseline_commit = $baselineCommit
    binary_sha256 = $hashes
    opening_sha256 = (Get-FileHash -LiteralPath 'tests/data/pvs_openings_2026-10-18.json' -Algorithm SHA256).Hash.ToLowerInvariant()
    profile_positions_sha256 = (Get-FileHash -LiteralPath 'tests/data/profile_positions.json' -Algorithm SHA256).Hash.ToLowerInvariant()
    sequential_workloads = $true
    requested_wake_state = 'ES_CONTINUOUS | ES_SYSTEM_REQUIRED'
    display_required = $false
    persistent_power_policy_changed = $false
    prior_wake_state = $null
    restore_requested_state = $null
    restore_succeeded = $false
    steps = @()
    error = $null
    exit_code = $null
}
$statePath = Join-Path $outputDirectory 'run-state.json'
function Save-RunState {
    $runState | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $statePath -Encoding utf8
}
$previousState = [RegicidePvsWakeGuard]::SetThreadExecutionState([uint32]2147483649)
$runState.prior_wake_state = $previousState
$runExitCode = 2
try {
    if ($previousState -eq 0) { throw 'Temporary system wake requirement failed.' }
    Save-RunState
    foreach ($step in $steps) {
        Assert-FrozenCheckout
        foreach ($role in @('baseline','candidate')) {
            $path = if ($role -eq 'baseline') { $baseline } else { $candidate }
            if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $hashes[$role]) {
                throw "The $role executable changed during the comparison."
            }
        }
        $stem = Join-Path $outputDirectory $step.name
        if ($step.kind -eq 'cost') {
            $arguments = @('scripts/benchmark_search.py', $baseline, $candidate,
                '--output', "$stem.json", '--positions', 'tests/data/profile_positions.json',
                '--depth', [string]$step.depth, '--repeats', '5', '--warmup', '1', '--seed', '20261018',
                '--baseline-commit', $baselineCommit, '--candidate-commit', $CandidateCommit)
            if ($step.control) { $arguments += @('--candidate-options', '{"PVS":false}', '--require-same-search') }
        } else {
            $arguments = @('scripts/compare_engines.py', $baseline, $candidate,
                '--movetime', [string]$step.movetime, '--openings', 'tests/data/pvs_openings_2026-10-18.json',
                '--max-plies', '500', '--draw-policy', 'claim', '--pgn', "$stem.pgn",
                '--baseline-commit', $baselineCommit, '--candidate-commit', $CandidateCommit)
        }
        $record = [ordered]@{name=$step.name; started_utc=[DateTime]::UtcNow.ToString('o');
            finished_utc=$null; exit_code=$null; arguments=$arguments}
        $runState.steps += $record
        Save-RunState
        # Continue locally so native stderr is retained in the log on both PS 5.1 and 7;
        # the native exit code, not stderr formatting, determines success.
        $oldPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = 'Continue'
            & $python @arguments *> "$stem.log"
            $stepExitCode = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $oldPreference
        }
        $record.exit_code = $stepExitCode
        $record.finished_utc = [DateTime]::UtcNow.ToString('o')
        Save-RunState
        Get-Content -LiteralPath "$stem.log" -Tail 4
        if ($stepExitCode -ne 0) { throw "Step $($step.name) failed with exit code $stepExitCode; evidence was preserved." }
    }
    Assert-FrozenCheckout
    foreach ($role in @('baseline','candidate')) {
        $path = if ($role -eq 'baseline') { $baseline } else { $candidate }
        if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $hashes[$role]) {
            throw "The $role executable changed during the comparison."
        }
    }
    $runExitCode = 0
} catch {
    $runState.error = $_.Exception.Message
    Write-Host $runState.error
} finally {
    if ($previousState -ne 0) {
        $restoreState = [uint32]($previousState -bor [uint32]2147483648)
        $runState.restore_requested_state = $restoreState
        $runState.restore_succeeded = [RegicidePvsWakeGuard]::SetThreadExecutionState($restoreState) -ne 0
        if (-not $runState.restore_succeeded) { $runExitCode = 2 }
    }
    $runState.finished_utc = [DateTime]::UtcNow.ToString('o')
    $runState.exit_code = $runExitCode
    Save-RunState
}
exit $runExitCode
