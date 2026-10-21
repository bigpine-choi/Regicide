param([Parameter(Mandatory=$true)][string]$OutputDirectory)
$ErrorActionPreference='Stop'
$destination=[IO.Path]::GetFullPath($OutputDirectory)
if(Test-Path -LiteralPath $destination){throw 'Preserve existing diagnostic results'}
New-Item -ItemType Directory -Path $destination | Out-Null
Copy-Item -LiteralPath $PSCommandPath -Destination (Join-Path $destination 'probe-source.ps1')
Add-Type -TypeDefinition @'
using System;using System.Runtime.InteropServices;using System.ComponentModel;
public static class AttributionNativeTimes {
 [DllImport("kernel32.dll", SetLastError=true)] static extern bool GetSystemTimes(out ulong idle,out ulong kernel,out ulong user);
 public static ulong[] Read(){ulong i,k,u;if(!GetSystemTimes(out i,out k,out u))throw new Win32Exception(Marshal.GetLastWin32Error());return new ulong[]{i,k,u};}
}
'@
$logical=[Environment]::ProcessorCount
$receipt=[ordered]@{status='RUNNING';started_utc=[DateTime]::UtcNow.ToString('o');measurement_pid=$PID;logical_cpus=$logical;
 probe_sha256=(Get-FileHash -LiteralPath $PSCommandPath -Algorithm SHA256).Hash.ToLowerInvariant();windows=@();
 scope='Current read-only WMI raw process counters including protected processes. No command lines, windows, environments, memory or process control. WMI/diagnostic overhead is included; this is not attribution of past M2 stops.';
 method='Stable PID+ElapsedTime+Name raw CPU delta / provider Timestamp_Sys100NS delta / logical processors. Separate native system endpoint counters; provider and native windows are not exactly simultaneous.';
 source='https://learn.microsoft.com/en-us/previous-versions/aa394323(v=vs.85)'}
$rawPath=Join-Path $destination 'snapshots.jsonl'
function Snapshot {
 $begin=[DateTime]::UtcNow.ToString('o');$nativeBefore=[AttributionNativeTimes]::Read()
 $rows=@(Get-CimInstance Win32_PerfRawData_PerfProc_Process | ForEach-Object {
  [ordered]@{name=$_.Name;pid=[long]$_.IDProcess;parent_pid=[long]$_.CreatingProcessID;start_counter=[long]$_.ElapsedTime;
   cpu_ticks=[long]$_.PercentProcessorTime;stamp=[long]$_.Timestamp_Sys100NS}
 })
 $value=@{utc=$begin;end_utc=[DateTime]::UtcNow.ToString('o');native_before=$nativeBefore;native_after=[AttributionNativeTimes]::Read();processes=$rows}
 $value | ConvertTo-Json -Depth 6 -Compress | Add-Content -LiteralPath $rawPath -Encoding utf8
 return $value
}
try{
 $before=Snapshot
 for($index=1;$index -le 6;$index++){
  Start-Sleep -Seconds 2;$after=Snapshot;$old=@{};$ranked=@();$sum=0.0
  foreach($entry in $before.processes){$old[[string]$entry.pid+'|'+$entry.name]=$entry}
  foreach($entry in $after.processes){
   if($entry.pid -eq 0 -or $entry.name -eq '_Total'){continue}
   $previous=$old[[string]$entry.pid+'|'+$entry.name]
   if(!$previous -or $entry.start_counter -ne $previous.start_counter){continue}
   $cpu=([long]$entry.cpu_ticks)-([long]$previous.cpu_ticks);$elapsed=([long]$entry.stamp)-([long]$previous.stamp)
   if($cpu -lt 0 -or $elapsed -le 0){continue}
   $fraction=[double]$cpu/[double]$elapsed/$logical;$sum+=$fraction
   if($cpu -gt 0){$ranked += [ordered]@{name=$entry.name;pid=$entry.pid;parent_pid=$entry.parent_pid;start_counter=$entry.start_counter;
    cpu_seconds=[double]$cpu/10000000;provider_seconds=[double]$elapsed/10000000;fraction_of_all_logical=$fraction}}
  }
  $total=([long]$after.native_after[1]-[long]$before.native_after[1])+([long]$after.native_after[2]-[long]$before.native_after[2])
  $idle=[long]$after.native_after[0]-[long]$before.native_after[0]
  if($total -le 0 -or $idle -lt 0 -or $idle -gt $total){throw 'Native aggregate counters invalid'}
  $receipt.windows += [ordered]@{index=$index;start_utc=$before.utc;end_utc=$after.utc;process_sum_fraction=$sum;
   native_host_busy_fraction=[double]($total-$idle)/$total;start_process_count=$before.processes.Count;end_process_count=$after.processes.Count;
   top_processes=@($ranked | Sort-Object -Property @{Expression={$_.fraction_of_all_logical};Descending=$true} | Select-Object -First 15)}
  $before=$after
 }
 $receipt.status='COMPLETE'
}catch{$receipt.status='ERROR';$receipt.error=$_.Exception.ToString()}
finally{$receipt.finished_utc=[DateTime]::UtcNow.ToString('o');$receipt | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath (Join-Path $destination 'receipt.json') -Encoding utf8}
$receipt.windows | ForEach-Object {[ordered]@{host=$_.native_host_busy_fraction;observed_sum=$_.process_sum_fraction;top=@($_.top_processes | Select-Object -First 4)}} | ConvertTo-Json -Depth 7
if($receipt.status -ne 'COMPLETE'){exit 2}
