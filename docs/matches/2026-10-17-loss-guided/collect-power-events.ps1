$ErrorActionPreference = 'Stop'
$guard = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'wake-guard.json') -Raw | ConvertFrom-Json
$windowStart = [DateTimeOffset]::Parse($guard.started)
$windowEnd = [DateTimeOffset]::Parse($guard.finished)
try {
    $powerEvents = @(Get-WinEvent -FilterHashtable @{LogName='System'; ProviderName='Microsoft-Windows-Kernel-Power'; Id=506,507; StartTime=$windowStart.LocalDateTime; EndTime=$windowEnd.LocalDateTime} -ErrorAction Stop | Select-Object @{Name='time_created'; Expression={$_.TimeCreated.ToString('o')}}, @{Name='id'; Expression={$_.Id}}, @{Name='provider'; Expression={$_.ProviderName}}, @{Name='message'; Expression={$_.Message}})
} catch {
    if ($_.FullyQualifiedErrorId -notlike 'NoMatchingEventsFound*') { throw }
    $powerEvents = @()
}
ConvertTo-Json -InputObject @($powerEvents) -Depth 5 | Set-Content -LiteralPath (Join-Path $PSScriptRoot 'suspend-events-window.json') -Encoding utf8
$queryRecord = [ordered]@{queried_at=(Get-Date).ToString('o'); log='System'; provider='Microsoft-Windows-Kernel-Power'; ids=@(506,507); start=$guard.started; end=$guard.finished; events=$powerEvents.Count; result='query succeeded'}
$queryRecord | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $PSScriptRoot 'suspend-event-query.json') -Encoding utf8
