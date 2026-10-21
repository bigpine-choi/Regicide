param([int]$TargetPid = 11552, [Parameter(Mandatory=$true)][string]$OutputPath)
$ErrorActionPreference = 'Stop'
if (Test-Path -LiteralPath $OutputPath) { throw 'Refusing to overwrite an existing parent-chain receipt' }
$receipt = [ordered]@{
    schema_version = 1; started_utc = [DateTime]::UtcNow.ToString('o'); status = 'RUNNING'
    requested_pid = $TargetPid; query_properties = @('ProcessId', 'ParentProcessId', 'Name')
    probe_sha256 = (Get-FileHash -LiteralPath $PSCommandPath -Algorithm SHA256).Hash.ToLowerInvariant()
    scope = 'Current reported PID/name/parent chain only, without command lines, window content, settings or process actions. No creation-time identity proof, so parent PID reuse cannot be excluded. Cannot identify the earlier main-stop cause.'
    chain = @()
}
$receipt | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $OutputPath -Encoding utf8
try {
    $seen = @{}; $currentPid = $TargetPid
    for ($depth = 0; $depth -lt 16 -and $currentPid -gt 0; $depth++) {
        if ($seen.ContainsKey($currentPid)) { throw 'Cycle in current reported parent PIDs' }
        $seen[$currentPid] = $true
        $row = Get-CimInstance -ClassName Win32_Process -Property ProcessId,ParentProcessId,Name -Filter "ProcessId = $currentPid"
        if ($null -eq $row) { $receipt.terminal = "PID $currentPid not present at query time"; break }
        $receipt.chain += [ordered]@{pid=[int]$row.ProcessId; process_name=$row.Name; parent_pid=[int]$row.ParentProcessId}
        $currentPid = [int]$row.ParentProcessId
    }
    $receipt.status = 'COMPLETE'
} catch { $receipt.status = 'ERROR'; $receipt.error = $_.Exception.GetType().Name + ': ' + $_.Exception.Message }
$receipt.finished_utc = [DateTime]::UtcNow.ToString('o')
$receipt | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $OutputPath -Encoding utf8
$receipt | ConvertTo-Json -Depth 6
if ($receipt.status -ne 'COMPLETE') { exit 1 }
