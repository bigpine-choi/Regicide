param(
    [Parameter(Mandatory=$true)][ValidatePattern('^[0-9a-fA-F]{40}$')][string]$CandidateCommit,
    [Parameter(Mandatory=$true)][ValidatePattern('^[0-9a-fA-F]{40}$')][string]$HarnessCommit,
    [Parameter(Mandatory=$true)][string]$ValidationEvidence,
    [ValidatePattern('^attempt-[0-9]{3}$')][string]$Attempt = 'attempt-001'
)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $repoRoot
$baselineCommit = '1441d5ec23733af570b05521620935619c39cefa'
$expectedCandidateCommit = '0edc06e437b934d177fc1e9b0907600b6287be72'
$python = Join-Path $repoRoot '.venv-tests/Scripts/python.exe'
$baseline = Join-Path $repoRoot 'build-see-baseline/source/build/regicide.exe'
$candidate = Join-Path $repoRoot 'build-see-native/regicide.exe'
$opening = Join-Path $repoRoot 'tests/data/see_replication_openings_2026-10-18.json'
$expectedHashes = [ordered]@{
    baseline = 'c333ccfa7ec3efbaa7f52df4c007875e64f68d0816a847c1390aa3213d0e427b'
    candidate = '2ba41c8303f266149323b55186f5c6a806c33f73d529d6911d6ca4b33bfa53db'
}
$openingHash = 'e12daff0f80ba3e2a09b2c4e9105f5b897763a777d7a2ede46e40012d2003289'
$outputRoot = Join-Path $repoRoot 'build/see-replication'
$outputDirectory = Join-Path $outputRoot $Attempt

function Assert-FrozenInputs {
    $head = & git rev-parse --verify HEAD
    if ($LASTEXITCODE -ne 0 -or $head -ne $HarnessCommit -or (& git status --porcelain)) {
        throw 'Use the clean, frozen harness checkout with the exact HarnessCommit.'
    }
    if ($CandidateCommit -ne $expectedCandidateCommit) { throw 'The frozen SEE engine source commit differs.' }
    foreach ($revision in @($baselineCommit, $CandidateCommit, $HarnessCommit)) {
        & git cat-file -e "${revision}^{commit}"
        if ($LASTEXITCODE -ne 0) { throw "Required source commit is unavailable: $revision" }
    }
    foreach ($role in @('baseline','candidate')) {
        $path = if ($role -eq 'baseline') { $baseline } else { $candidate }
        if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expectedHashes[$role]) {
            throw "The frozen $role executable hash differs. No measurements were accepted."
        }
    }
    if ((Get-FileHash -LiteralPath $opening -Algorithm SHA256).Hash.ToLowerInvariant() -ne $openingHash) {
        throw 'The preregistered fresh opening file changed.'
    }
}
Assert-FrozenInputs
$validationPath = (Resolve-Path -LiteralPath $ValidationEvidence).Path
$validation = Get-Content -LiteralPath $validationPath -Raw | ConvertFrom-Json
if ($validation.all_passed -ne $true) { throw 'All rule and engine validation must pass before a match.' }
foreach ($role in @('baseline','candidate')) {
    if ($validation.binary_sha256.$role -ne $expectedHashes[$role]) {
        throw "Validation evidence does not identify the frozen $role executable."
    }
}
if (Test-Path -LiteralPath $outputDirectory) { throw "Preserve previous attempt directory: $outputDirectory" }
New-Item -ItemType Directory -Path $outputRoot -Force | Out-Null
$previousAttempts = @(Get-ChildItem -LiteralPath $outputRoot -Directory | Where-Object { $_.Name -match '^attempt-[0-9]{3}$' } | Sort-Object Name)
$expectedAttempt = 'attempt-{0:d3}' -f ($previousAttempts.Count + 1)
if ($Attempt -ne $expectedAttempt) { throw "Attempt numbering must be contiguous: expected $expectedAttempt" }
$adoptedBlocks = [ordered]@{}
if ($previousAttempts.Count -gt 0) {
    foreach ($previousAttempt in $previousAttempts) {
        $prior = Get-Content -LiteralPath (Join-Path $previousAttempt.FullName 'run-state.json') -Raw | ConvertFrom-Json
        if ($prior.harness_commit -ne $HarnessCommit -or $prior.candidate_commit -ne $CandidateCommit -or
            $prior.opening_sha256 -ne $openingHash) {
            throw 'Every preserved attempt must use the same frozen inputs and harness.'
        }
        $retryAllowed = $false
        foreach ($priorStep in $prior.steps) {
            $priorMonitorPath = Join-Path $previousAttempt.FullName $priorStep.monitor_json
            if (-not (Test-Path -LiteralPath $priorMonitorPath)) { throw 'Prior guard evidence is missing; retry is blocked.' }
            $priorMonitor = Get-Content -LiteralPath $priorMonitorPath -Raw | ConvertFrom-Json
            if ($priorStep.valid_for_comparison -eq $true -and $priorMonitor.valid_for_comparison -eq $true) {
                if ($adoptedBlocks.Contains($priorStep.name)) { throw 'A previously clean time block was rerun; evidence needs review.' }
                $adoptedBlocks[$priorStep.name] = "../$($previousAttempt.Name)/$($priorStep.name).json"
            } elseif ($priorMonitor.contaminated -eq $true -and $priorMonitor.failure.retry_allowed -eq $true) {
                $retryAllowed = $true
            } else {
                throw 'A prior non-load/ownership/engine failure blocks automatic retry.'
            }
        }
        if ($prior.contaminated -ne $true -or -not $retryAllowed) {
            throw 'A new attempt is allowed only after documented retryable contamination; never rerun clean results.'
        }
    }
}
New-Item -ItemType Directory -Path $outputDirectory | Out-Null
if (-not ('RegicideSeeCleanWakeGuard' -as [type])) {
    Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class RegicideSeeCleanWakeGuard {
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern uint SetThreadExecutionState(uint flags);
}
'@
}
function Quote-WindowsArgument([string]$Value) {
    return '"' + (($Value -replace '(\\*)"', '$1$1\"') -replace '(\\+)$', '$1$1') + '"'
}
$runState = [ordered]@{
    schema_version = 1
    attempt = $Attempt
    started_utc = [DateTime]::UtcNow.ToString('o')
    finished_utc = $null
    candidate_commit = $CandidateCommit
    harness_commit = $HarnessCommit
    baseline_commit = $baselineCommit
    binary_sha256 = $expectedHashes
    opening_sha256 = $openingHash
    validation_path = $validationPath
    validation_sha256 = (Get-FileHash -LiteralPath $validationPath -Algorithm SHA256).Hash.ToLowerInvariant()
    sequential_workloads = $true
    external_engine_poll_seconds = 0.5
    external_engine_busy_delta_ticks = 0
    idle_required_seconds = 10
    new_or_disappearing_external_engine_is_failure = $true
    contaminated = $false
    entire_current_time_block_invalid_on_contamination = $true
    retry_policy = 'Only after documented contamination, preserve entire prior attempt, restart all 16 games of a time block; never select games by result.'
    requested_wake_state = 'ES_CONTINUOUS | ES_SYSTEM_REQUIRED'
    display_required = $false
    persistent_power_policy_changed = $false
    prior_wake_state = $null
    restore_requested_state = $null
    restore_succeeded = $false
    steps = @()
    adopted_blocks = $adoptedBlocks
    error = $null
    exit_code = $null
}
$statePath = Join-Path $outputDirectory 'run-state.json'
function Save-RunState {
    $runState | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $statePath -Encoding utf8
}
$previousState = [RegicideSeeCleanWakeGuard]::SetThreadExecutionState([uint32]2147483649)
$runState.prior_wake_state = $previousState
$measurement = $null
$abortFile = Join-Path $outputDirectory 'abort-request.json'
$runExitCode = 2
try {
    if ($previousState -eq 0) { throw 'Temporary system wake requirement failed.' }
    Save-RunState
    foreach ($movetime in @(100, 1000)) {
        Assert-FrozenInputs
        $name = "time$movetime"
        if ($adoptedBlocks.Contains($name)) {
            Write-Host "Preserving the first valid $name block: $($adoptedBlocks[$name])"
            continue
        }
        $stem = Join-Path $outputDirectory $name
        $arguments = @('scripts/see_clean_guard.py', '--baseline', $baseline, '--candidate', $candidate,
            '--baseline-commit', $baselineCommit, '--candidate-commit', $CandidateCommit,
            '--harness-commit', $HarnessCommit, '--baseline-sha256', $expectedHashes.baseline,
            '--candidate-sha256', $expectedHashes.candidate, '--openings', $opening,
            '--movetime', [string]$movetime, '--pgn', "$stem.pgn",
            '--monitor-json', "$stem.monitor.json", '--monitor-log', "$stem.monitor.jsonl",
            '--abort-file', $abortFile)
        $record = [ordered]@{name=$name; started_utc=[DateTime]::UtcNow.ToString('o');
            finished_utc=$null; exit_code=$null; contaminated=$null; valid_for_comparison=$false;
            arguments=$arguments; monitor_json="$name.monitor.json"; monitor_log="$name.monitor.jsonl"}
        $runState.steps += $record
        Save-RunState
        $quotedArguments = ($arguments | ForEach-Object { Quote-WindowsArgument ([string]$_) }) -join ' '
        $measurement = Start-Process -FilePath $python -ArgumentList $quotedArguments -WorkingDirectory $repoRoot `
            -WindowStyle Hidden -PassThru -RedirectStandardOutput "$stem.log" -RedirectStandardError "$stem.stderr.log"
        $record.launcher_pid = $measurement.Id
        $record.launcher_start_utc = $measurement.StartTime.ToUniversalTime().ToString('o')
        Save-RunState
        $measurement.WaitForExit()
        $measurement.Refresh()
        $stepExitCode = $measurement.ExitCode
        $measurement.Dispose()
        $measurement = $null
        $record.exit_code = $stepExitCode
        $record.finished_utc = [DateTime]::UtcNow.ToString('o')
        if (Test-Path -LiteralPath "$stem.monitor.json") {
            $monitor = Get-Content -LiteralPath "$stem.monitor.json" -Raw | ConvertFrom-Json
            $record.contaminated = $monitor.contaminated
            $record.valid_for_comparison = $monitor.valid_for_comparison
            if ($monitor.contaminated) { $runState.contaminated = $true }
            if ($record.valid_for_comparison -eq $true) { $adoptedBlocks[$name] = "$name.json" }
        }
        $record.artifact_sha256 = [ordered]@{}
        foreach ($extension in @('json','pgn','monitor.json','monitor.jsonl','log','stderr.log')) {
            $artifactPath = "$stem.$extension"
            if (Test-Path -LiteralPath $artifactPath -PathType Leaf) {
                $record.artifact_sha256["$name.$extension"] = (Get-FileHash -LiteralPath $artifactPath -Algorithm SHA256).Hash.ToLowerInvariant()
            }
        }
        Save-RunState
        Get-Content -LiteralPath "$stem.log" -Tail 4
        if ($stepExitCode -ne 0 -or $record.valid_for_comparison -ne $true) {
            throw "Step $name stopped or was invalid; all evidence was preserved and no later time block was started."
        }
    }
    Assert-FrozenInputs
    $runExitCode = 0
} catch {
    $runState.error = $_.Exception.Message
    Write-Host $runState.error
} finally {
    if ($null -ne $measurement) {
        # Request cooperative cancellation first. The helper checks this every
        # native sample and finishes only its own UCI children before publishing.
        '{"reason":"runner cleanup"}' | Set-Content -LiteralPath $abortFile -Encoding utf8
        if (-not $measurement.WaitForExit(60000)) {
            # No external process is stopped. Verify this still is the exact
            # launcher we created, then terminate only its descendant tree.
            $current = Get-Process -Id $measurement.Id -ErrorAction SilentlyContinue
            if ($null -ne $current -and $current.StartTime -eq $measurement.StartTime) {
                & taskkill.exe /PID $measurement.Id /T /F | Out-Null
            }
            $runState.error = 'Owned measurement required forced cleanup after cooperative abort timeout.'
        }
        $measurement.Dispose()
        $measurement = $null
    }
    if ($previousState -ne 0) {
        $restoreState = [uint32]($previousState -bor [uint32]2147483648)
        $runState.restore_requested_state = $restoreState
        $runState.restore_succeeded = [RegicideSeeCleanWakeGuard]::SetThreadExecutionState($restoreState) -ne 0
        if (-not $runState.restore_succeeded) { $runExitCode = 2 }
    }
    $runState.finished_utc = [DateTime]::UtcNow.ToString('o')
    $runState.exit_code = $runExitCode
    Save-RunState
}
exit $runExitCode
