$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '../..')
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class RegicideTaskWakeGuard {
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern uint SetThreadExecutionState(uint flags);
}
'@
$previousState = [RegicideTaskWakeGuard]::SetThreadExecutionState([uint32]2147483649)
if ($previousState -eq 0) { throw 'Temporary system wake requirement failed.' }
$runState = [ordered]@{
    started = (Get-Date).ToString('o')
    prior_state = $previousState
    requested_state = 'ES_CONTINUOUS | ES_SYSTEM_REQUIRED'
    display_required = $false
    persistent_power_policy_changed = $false
    comparison_exit_code = $null
    finished = $null
    restore_requested_state = $null
    restore_succeeded = $false
}
$comparisonExitCode = 2
try {
    & .venv-tests/Scripts/python.exe scripts/compare_engines.py build-memory-baseline/source/build/regicide.exe build/regicide.exe --movetime 100 --pgn build/task05-evidence/time100-retry.pgn --baseline-commit ef52f14569222acbfaa512245bab81558795180f --candidate-commit 4a0f95a035b1cc0eff8e8d56a14ea898621eab64 *> build/task05-evidence/time100-retry.log
    $comparisonExitCode = $LASTEXITCODE
    $runState.comparison_exit_code = $comparisonExitCode
} finally {
    $restoreState = [uint32]($previousState -bor [uint32]2147483648)
    $restored = [RegicideTaskWakeGuard]::SetThreadExecutionState($restoreState)
    $runState.restore_requested_state = $restoreState
    $runState.restore_succeeded = $restored -ne 0
    $runState.finished = (Get-Date).ToString('o')
    $runState | ConvertTo-Json | Set-Content -Encoding utf8 build/task05-evidence/wake-guard.json
}
Get-Content build/task05-evidence/time100-retry.log -Tail 22
exit $comparisonExitCode
