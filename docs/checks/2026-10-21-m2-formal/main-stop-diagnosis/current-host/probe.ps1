param([Parameter(Mandatory=$true)][string]$OutputDirectory)
$ErrorActionPreference = 'Stop'
$destination = [IO.Path]::GetFullPath($OutputDirectory)
if (Test-Path -LiteralPath $destination) { throw 'Refusing to replace an existing diagnosis receipt' }
New-Item -ItemType Directory -Path $destination | Out-Null
Copy-Item -LiteralPath $PSCommandPath -Destination (Join-Path $destination 'probe-source.ps1')
$clock = [Diagnostics.Stopwatch]::StartNew()
$logical = [Environment]::ProcessorCount
$receipt = [ordered]@{
    schema_version = 1
    status = 'RUNNING'
    started_utc = [DateTime]::UtcNow.ToString('o')
    probe_sha256 = (Get-FileHash -LiteralPath $PSCommandPath -Algorithm SHA256).Hash.ToLowerInvariant()
    logical_processors = $logical
    powershell_version = $PSVersionTable.PSVersion.ToString()
    measurement_pid = $PID
    requested_windows = 3
    requested_window_seconds = 2
    collected_fields = @('process_name', 'pid', 'start_time_utc', 'cumulative_cpu_seconds')
    excluded_fields = @('command_line', 'environment', 'credentials', 'process_memory')
    process_actions = 'Read only. No process is created, changed, signaled or terminated by the measurement loop.'
    interpretation = 'Failure-postdated current-host diagnosis only. These windows cannot identify the cause of the earlier 09:27:01 UTC main interruption.'
    method = 'Get-Process cumulative CPU deltas divided by actual elapsed seconds and logical processor count; PID 0 Idle is excluded from busy CPU. GetSystemTimes independently gives aggregate host busy CPU.'
    windows = @()
}
$output = Join-Path $destination 'receipt.json'
function Save-Receipt { $receipt | ConvertTo-Json -Depth 14 | Set-Content -LiteralPath $output -Encoding utf8 }
Save-Receipt
try {
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
public static class RegicideCurrentHostCpu {
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool GetSystemTimes(out ulong idle, out ulong kernel, out ulong user);
    public static ulong[] Read() {
        ulong idle, kernel, user;
        if (!GetSystemTimes(out idle,out kernel,out user)) throw new Win32Exception(Marshal.GetLastWin32Error());
        return new ulong[]{idle,kernel,user};
    }
}
'@
    function Snapshot {
        $started = $clock.Elapsed.TotalSeconds
        $stamp = [DateTime]::UtcNow
        $entries = @{}
        foreach ($process in Get-Process) {
            $cpu = $null; $created = $null; $cpuError = $null
            try {
                $rawCpu = $process.CPU
                if ($null -eq $rawCpu) { $cpuError = 'CounterUnavailable' }
                else { $cpu = [double]$rawCpu }
            } catch { $cpuError = $_.Exception.GetType().Name }
            try { $created = $process.StartTime.ToUniversalTime().ToString('o') } catch { }
            $entries[[string]$process.Id] = [ordered]@{
                process_name = $process.ProcessName
                pid = $process.Id
                start_time_utc = $created
                cumulative_cpu_seconds = $cpu
                cpu_read_error = $cpuError
            }
        }
        $native = [RegicideCurrentHostCpu]::Read()
        $ended = $clock.Elapsed.TotalSeconds
        return @{ entries = $entries; utc = $stamp; midpoint = ($started+$ended)/2;
                  capture_seconds = $ended-$started; native = $native }
    }
    $before = Snapshot
    for ($window = 1; $window -le 3; $window++) {
        Start-Sleep -Seconds 2
        $after = Snapshot
        $elapsed = $after.midpoint - $before.midpoint
        $rows = @(); $missingEnd = @(); $sum = 0.0; $unmeasured = 0; $newMeasured = 0
        foreach ($key in $after.entries.Keys) {
            $current = $after.entries[$key]
            if ($current.pid -eq 0) { continue }
            $old = $before.entries[$key]
            $delta = $null; $basis = 'no_baseline'
            if ($null -eq $current.cumulative_cpu_seconds) { $basis = 'cpu_unavailable' }
            elseif ($null -ne $old -and $null -ne $old.cumulative_cpu_seconds -and
                    $old.process_name -eq $current.process_name -and
                    !($old.start_time_utc -and $current.start_time_utc -and $old.start_time_utc -ne $current.start_time_utc)) {
                $delta = $current.cumulative_cpu_seconds - $old.cumulative_cpu_seconds
                $basis = 'same_identity_delta'
                if ($delta -lt 0) { $delta = $null; $basis = 'counter_regressed_or_pid_reused' }
            }
            elseif ($current.start_time_utc -and [DateTime]::Parse($current.start_time_utc) -ge $before.utc) {
                $delta = $current.cumulative_cpu_seconds; $basis = 'new_process_entire_lifetime_within_window'; $newMeasured++
            }
            if ($null -eq $delta) { $unmeasured++ } else { $sum += $delta }
            $rows += [ordered]@{
                process_name = $current.process_name; pid = $current.pid; start_time_utc = $current.start_time_utc
                cumulative_cpu_seconds_before = $(if ($null -ne $old) { $old.cumulative_cpu_seconds } else { $null })
                cumulative_cpu_seconds_after = $current.cumulative_cpu_seconds
                end_cpu_read_error = $current.cpu_read_error
                cpu_delta_seconds = $delta
                cpu_fraction_of_all_logical = $(if ($null -ne $delta) { $delta/$elapsed/$logical } else { $null })
                basis = $basis
            }
        }
        foreach ($key in $before.entries.Keys) {
            if (!$after.entries.ContainsKey($key)) {
                $missingEnd += @{process_name=$before.entries[$key].process_name;pid=$before.entries[$key].pid}
            }
        }
        $idle = [double]$after.native[0]-[double]$before.native[0]
        $total = ([double]$after.native[1]-[double]$before.native[1])+([double]$after.native[2]-[double]$before.native[2])
        if ($total -le 0 -or $idle -lt 0 -or $idle -gt $total) { throw 'Invalid native aggregate CPU counter delta' }
        $ranked = @($rows | Where-Object { $null -ne $_.cpu_fraction_of_all_logical } |
                    Sort-Object -Property @{Expression={$_.cpu_fraction_of_all_logical};Descending=$true},pid)
        $receipt.windows += [ordered]@{
            window = $window; start_utc = $before.utc.ToString('o'); end_utc = $after.utc.ToString('o')
            actual_elapsed_seconds = $elapsed
            start_snapshot_seconds = $before.capture_seconds; end_snapshot_seconds = $after.capture_seconds
            observed_process_cpu_delta_seconds = $sum
            observed_process_cpu_fraction_of_all_logical = $sum/$elapsed/$logical
            aggregate_host_busy_fraction_getsystemtimes = ($total-$idle)/$total
            aggregate_total_delta_100ns = $total; aggregate_idle_delta_100ns = $idle
            start_processes = $before.entries.Count; end_processes = $after.entries.Count
            unmeasured_end_process_count = $unmeasured; new_processes_with_measured_lifetime = $newMeasured
            disappeared_processes = $missingEnd
            top_processes = @($ranked | Select-Object -First 15)
            all_end_processes = $rows
            limitation = 'Ended-between-snapshots process CPU and inaccessible counters are missing from process sums; transient processes may never be seen. Aggregate host CPU includes them. Asynchronous counters make process sums approximate.'
        }
        Save-Receipt
        $before = $after
    }
    $receipt.status = 'COMPLETE'
} catch {
    $receipt.status = 'ERROR'
    $receipt.error = $_.Exception.GetType().Name + ': ' + $_.Exception.Message
} finally {
    $receipt.finished_utc = [DateTime]::UtcNow.ToString('o')
    $receipt.elapsed_seconds = $clock.Elapsed.TotalSeconds
    Save-Receipt
}
$receipt | Select-Object status,logical_processors,elapsed_seconds | ConvertTo-Json
if ($receipt.status -ne 'COMPLETE') { exit 1 }
