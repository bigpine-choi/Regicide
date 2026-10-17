param([Parameter(Mandatory=$true)][ValidatePattern('^[0-9a-fA-F]{40}$')][string]$CandidateCommit)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $repoRoot
$baselineCommit = '1441d5ec23733af570b05521620935619c39cefa'
$python = Join-Path $repoRoot '.venv-tests/Scripts/python.exe'
$baseline = Join-Path $repoRoot 'build-see-baseline/source/build/regicide.exe'
$candidate = Join-Path $repoRoot 'build-see-native/regicide.exe'
$outputDirectory = Join-Path $repoRoot 'build/see-evidence'

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
    foreach ($extension in @('json','pgn','log','stderr.log')) {
        if (Test-Path -LiteralPath (Join-Path $outputDirectory "$name.$extension")) {
            throw "Preserve previous output: $name.$extension already exists."
        }
    }
}
if (-not ('RegicideSeeWakeGuard' -as [type])) {
    Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class RegicideSeeWakeGuard {
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
    opening_scope = 'Previously used PVS development comparison set, not fresh held-out data.'
    sequential_workloads = $true
    external_engine_poll_seconds = 5
    external_engine_busy_delta_seconds = 0.05
    external_engine_snapshots = @()
    observed_external_engine_cpu_increase = $false
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
    $runState | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $statePath -Encoding utf8
}

function Get-ExternalEngineSnapshot([int]$MeasurementProcessId = 0, [string]$Phase) {
    # Only the two engines owned directly by this measurement Python process are
    # excluded. Existing user engines remain visible even if their path matches.
    $items = @()
    foreach ($native in @(Get-CimInstance Win32_Process -Filter "Name='regicide.exe'")) {
        if ($MeasurementProcessId -ne 0 -and $native.ParentProcessId -eq $MeasurementProcessId) { continue }
        $process = Get-Process -Id $native.ProcessId -ErrorAction SilentlyContinue
        if ($null -ne $process) {
            $items += [ordered]@{pid=[int]$native.ProcessId; parent_pid=[int]$native.ParentProcessId;
                creation_time=[string]$native.CreationDate; path=[string]$native.ExecutablePath;
                cpu_seconds=[double]$process.CPU}
        }
    }
    $snapshot = [ordered]@{utc=[DateTime]::UtcNow.ToString('o'); phase=$Phase;
        measurement_python_pid=$MeasurementProcessId; processes=@($items)}
    $runState.external_engine_snapshots += $snapshot
    return $snapshot
}

function Get-ExternalCpuIncrease($Previous, $Current) {
    $increase = 0.0
    foreach ($item in $Current.processes) {
        $prior = @($Previous.processes | Where-Object { $_.pid -eq $item.pid -and $_.creation_time -eq $item.creation_time })
        $delta = if ($prior.Count -gt 0) { $item.cpu_seconds - $prior[0].cpu_seconds } else { $item.cpu_seconds }
        if ($delta -gt 0) { $increase += $delta }
    }
    return $increase
}

function Assert-ExternalEnginesIdle([string]$Phase) {
    $previous = Get-ExternalEngineSnapshot -Phase "$Phase-before-idle-check"
    $idleChecks = 0
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        Start-Sleep -Seconds 2
        $current = Get-ExternalEngineSnapshot -Phase "$Phase-idle-check"
        $increase = Get-ExternalCpuIncrease $previous $current
        $current.cpu_increase_seconds = $increase
        Save-RunState
        if ($increase -le $runState.external_engine_busy_delta_seconds) { $idleChecks++ } else { $idleChecks = 0 }
        if ($idleChecks -ge 2) { return }
        $previous = $current
    }
    throw 'Existing user engine computation did not become idle within 120 seconds. No user game was closed; coordinate an idle interval before a new run.'
}

function Quote-WindowsArgument([string]$Value) {
    # Start-Process joins ArgumentList itself. Quote each item using the Windows
    # argv convention so JSON quotes and paths with spaces survive PS 5.1/7.
    return '"' + (($Value -replace '(\\*)"', '$1$1\"') -replace '(\\+)$', '$1$1') + '"'
}

$previousState = [RegicideSeeWakeGuard]::SetThreadExecutionState([uint32]2147483649)
$runState.prior_wake_state = $previousState
$runExitCode = 2
$measurement = $null
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
        Assert-ExternalEnginesIdle -Phase $step.name
        $stem = Join-Path $outputDirectory $step.name
        if ($step.kind -eq 'cost') {
            $arguments = @('scripts/benchmark_search.py', $baseline, $candidate,
                '--output', "$stem.json", '--positions', 'tests/data/profile_positions.json',
                '--depth', [string]$step.depth, '--repeats', '5', '--warmup', '1', '--seed', '20261018',
                '--baseline-commit', $baselineCommit, '--candidate-commit', $CandidateCommit)
            if ($step.control) { $arguments += @('--candidate-options', '{"UseSEE":false}', '--require-same-search') }
            else { $arguments += '--allow-score-differences' }
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
        $quotedArguments = ($arguments | ForEach-Object { Quote-WindowsArgument ([string]$_) }) -join ' '
        $measurement = Start-Process -FilePath $python -ArgumentList $quotedArguments -WorkingDirectory $repoRoot `
            -WindowStyle Hidden -PassThru -RedirectStandardOutput "$stem.log" -RedirectStandardError "$stem.stderr.log"
        $lastSnapshot = Get-ExternalEngineSnapshot -MeasurementProcessId $measurement.Id -Phase "$($step.name)-running"
        while (-not $measurement.WaitForExit(5000)) {
            $snapshot = Get-ExternalEngineSnapshot -MeasurementProcessId $measurement.Id -Phase "$($step.name)-running"
            $increase = Get-ExternalCpuIncrease $lastSnapshot $snapshot
            $snapshot.cpu_increase_seconds = $increase
            if ($increase -gt $runState.external_engine_busy_delta_seconds) {
                $runState.observed_external_engine_cpu_increase = $true
            }
            $lastSnapshot = $snapshot
            Save-RunState
        }
        $measurement.Refresh()
        $stepExitCode = $measurement.ExitCode
        $snapshot = Get-ExternalEngineSnapshot -Phase "$($step.name)-finished"
        $increase = Get-ExternalCpuIncrease $lastSnapshot $snapshot
        $snapshot.cpu_increase_seconds = $increase
        if ($increase -gt $runState.external_engine_busy_delta_seconds) { $runState.observed_external_engine_cpu_increase = $true }
        $record.exit_code = $stepExitCode
        $record.finished_utc = [DateTime]::UtcNow.ToString('o')
        $measurement.Dispose()
        $measurement = $null
        Save-RunState
        Get-Content -LiteralPath "$stem.log" -Tail 4
        if ($stepExitCode -ne 0) { throw "Step $($step.name) failed with exit code $stepExitCode; evidence was preserved." }
        if ($step.kind -eq 'cost' -and -not $step.control) {
            # Finish and preserve every paired sample, then require review of
            # score differences before any later workload or timed match.
            $costResult = Get-Content -LiteralPath "$stem.json" -Raw | ConvertFrom-Json
            if ($costResult.summary.score_mismatches -gt 0) {
                throw "Step $($step.name) preserved $($costResult.summary.score_mismatches) score mismatches. Investigate before continuing; no timed matches were started."
            }
        }
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
    # The runner never stops user engines. On a harness exception, wait for its
    # child to exit so no owned workload survives a reported finished run.
    if ($null -ne $measurement) {
        $measurement.WaitForExit()
        $measurement.Dispose()
    }
    if ($previousState -ne 0) {
        $restoreState = [uint32]($previousState -bor [uint32]2147483648)
        $runState.restore_requested_state = $restoreState
        $runState.restore_succeeded = [RegicideSeeWakeGuard]::SetThreadExecutionState($restoreState) -ne 0
        if (-not $runState.restore_succeeded) { $runExitCode = 2 }
    }
    $runState.finished_utc = [DateTime]::UtcNow.ToString('o')
    $runState.exit_code = $runExitCode
    Save-RunState
}
exit $runExitCode
