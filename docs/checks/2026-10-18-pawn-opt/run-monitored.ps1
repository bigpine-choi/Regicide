param([ValidateSet('profile','search')][string]$Mode)
$ErrorActionPreference='Stop'
$repoRoot=Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location -LiteralPath $repoRoot
$output=Join-Path $PSScriptRoot ($Mode+'-load.json')
if(Test-Path -LiteralPath $output){throw 'Use a new monitor output path'}
function Snapshot-Cpu {
    $result=@{}
    foreach($item in Get-Process -ErrorAction SilentlyContinue){
        if($item.ProcessName -eq 'Idle'){continue}
        try{$value=$item.CPU;if($null -ne $value){$result[$item.Id]=@($item.ProcessName,[double]$value)}}catch{}
    }
    return $result
}
if($Mode -eq 'profile'){
    $file=Join-Path $repoRoot 'build-pawn-opt-profile\profile_evaluation.exe'
    $arguments=@('--compare-pawn-optimization','--fen-file','tests/data/pawn_structure_profile_2026_10_18.fens','--output','build/pawn-opt-validation/profile-windows.json')
}else{
    $file=(Get-Process -Id $PID).Path
    $arguments=@('-NoProfile','-File',('"'+(Join-Path $PSScriptRoot 'run-fixed-depth.ps1')+'"'))
}
$previous=Snapshot-Cpu
$watch=[Diagnostics.Stopwatch]::StartNew()
$previousTime=$watch.Elapsed.TotalSeconds
$child=Start-Process -FilePath $file -ArgumentList $arguments -WorkingDirectory $repoRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $PSScriptRoot ($Mode+'-driver.stdout.log')) -RedirectStandardError (Join-Path $PSScriptRoot ($Mode+'-driver.stderr.log'))
$child.Handle | Out-Null
$rows=[Collections.Generic.List[object]]::new()
do{
    Start-Sleep -Milliseconds 500
    $current=Snapshot-Cpu
    $now=$watch.Elapsed.TotalSeconds
    $total=0.0;$target=0.0;$browser=0.0;$engine=0.0;$monitor=0.0
    foreach($processId in $current.Keys){
        if(!$previous.ContainsKey($processId) -or $previous[$processId][0] -ne $current[$processId][0]){continue}
        $delta=[Math]::Max(0.0,$current[$processId][1]-$previous[$processId][1])
        $total+=$delta
        if($processId -eq $child.Id){$target+=$delta}
        if($processId -eq $PID){$monitor+=$delta}
        if($current[$processId][0] -match '^(whale|chrome|msedge|firefox)$'){$browser+=$delta}
        if($current[$processId][0] -match '^regicide'){$engine+=$delta}
    }
    $rows.Add([pscustomobject]@{elapsed_seconds=$now;interval_seconds=($now-$previousTime);all_observed_cpu_seconds=$total;target_cpu_seconds=$target;monitor_cpu_seconds=$monitor;browser_cpu_seconds=$browser;regicide_cpu_seconds=$engine})
    $previous=$current;$previousTime=$now
    $child.Refresh()
}while(!$child.HasExited)
$child.WaitForExit();$child.Refresh()
[ordered]@{mode=$Mode;interval_target_ms=500;elapsed_seconds=$watch.Elapsed.TotalSeconds;logical_cpus=[Environment]::ProcessorCount;exit_code=$child.ExitCode;monitor_policy='Sample process CPU counters without controlling other applications. Names/PIDs are not persisted. Aggregate omits exited/new inaccessible processes; no thermal/frequency or complete host-load guarantee. Search regicide counters include the benchmark engines.';samples=$rows} | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $output -Encoding utf8
Write-Output ('Completed '+$Mode+' exit='+$child.ExitCode+' seconds='+$watch.Elapsed.TotalSeconds)
if($child.ExitCode -ne 0){Get-Content -LiteralPath (Join-Path $PSScriptRoot ($Mode+'-driver.stderr.log')) -Tail 15;exit $child.ExitCode}

exit 0
