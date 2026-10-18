$ErrorActionPreference='Stop'
$repoRoot=Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location -LiteralPath $repoRoot
$browserPattern='^(whale|chrome|msedge|firefox|brave|opera|vivaldi|arc|iexplore|chromium|thorium|waterfox|floorp|librewolf)$'
$allowedEngines=@((Join-Path $repoRoot 'build\regicide.exe'),(Join-Path $repoRoot 'build\lmr-validation\regicide-v057.exe'))
$output=Join-Path $PSScriptRoot 'match-load.json'
if(Test-Path -LiteralPath $output){throw 'Use a fresh measurement directory'}
function Snapshot-Cpu {
    $items=@{};$unreadable=0;$browserCount=0;$webviewCount=0;$engineCount=0;$unexpectedEngines=0
    foreach($p in Get-Process -ErrorAction SilentlyContinue){
        $name=$p.ProcessName
        if($name -eq 'Idle'){continue}
        if($name -match $browserPattern){++$browserCount}
        if($name -eq 'msedgewebview2'){++$webviewCount}
        if($name -match '^regicide'){
            ++$engineCount
            try{if($p.Path -notin $allowedEngines){++$unexpectedEngines}}catch{++$unexpectedEngines}
        }
        try{
            $cpu=$p.CPU
            if($null -eq $cpu){++$unreadable;continue}
            $stamp=$p.StartTime.ToUniversalTime().Ticks
            $items[$p.Id]=@($name,[double]$cpu,$stamp)
        }catch{++$unreadable}
    }
    return @{items=$items;unreadable=$unreadable;browser_count=$browserCount;webview_count=$webviewCount;engine_count=$engineCount;unexpected_engine_count=$unexpectedEngines}
}
function Delta-Row($before,$after,$elapsed,$interval,$phase){
    $total=0.0;$browser=0.0;$webview=0.0;$engine=0.0;$monitor=0.0
    foreach($processId in $after.items.Keys){
        $item=$after.items[$processId]
        $old=$before.items[$processId]
        $existing=$null -ne $old -and $old[0] -eq $item[0] -and $old[2] -eq $item[2]
        $delta=if($existing){[Math]::Max(0.0,$item[1]-$old[1])}else{0.0}
        # A newly observed browser/webview is charged its lifetime CPU as well;
        # browser process presence already invalidates a measurement block.
        if(!$existing -and ($item[0] -match $browserPattern -or $item[0] -eq 'msedgewebview2')){$delta=[Math]::Max(0.0,$item[1])}
        $total+=$delta
        if($item[0] -match $browserPattern){$browser+=$delta}
        if($item[0] -eq 'msedgewebview2'){$webview+=$delta}
        if($item[0] -match '^regicide'){$engine+=$delta}
        if($processId -eq $PID){$monitor+=$delta}
    }
    return [pscustomobject]@{phase=$phase;elapsed_seconds=$elapsed;interval_seconds=$interval;browser_processes=$after.browser_count;webview_processes=$after.webview_count;regicide_processes=$after.engine_count;unexpected_regicide_processes=$after.unexpected_engine_count;unreadable_cpu_processes=$after.unreadable;browser_cpu_seconds=$browser;webview_cpu_seconds=$webview;regicide_cpu_seconds=$engine;all_observed_cpu_seconds=$total;monitor_cpu_seconds=$monitor}
}
$preRows=[Collections.Generic.List[object]]::new()
$preWatch=[Diagnostics.Stopwatch]::StartNew()
$previous=Snapshot-Cpu
$initial=[ordered]@{browser_processes=$previous.browser_count;webview_processes=$previous.webview_count;regicide_processes=$previous.engine_count}
$previousTime=0.0
do{
    Start-Sleep -Milliseconds 500
    $current=Snapshot-Cpu;$now=$preWatch.Elapsed.TotalSeconds
    $preRows.Add((Delta-Row $previous $current $now ($now-$previousTime) 'preflight'))
    $previous=$current;$previousTime=$now
}while($preWatch.Elapsed.TotalSeconds -lt 20)
$preflightPassed=$initial.browser_processes -eq 0 -and $initial.regicide_processes -eq 0 -and @($preRows|Where-Object {$_.browser_processes -gt 0 -or $_.regicide_processes -gt 0 -or $_.browser_cpu_seconds -gt 0}).Count -eq 0
[ordered]@{started_utc=[DateTime]::UtcNow.AddSeconds(-$preWatch.Elapsed.TotalSeconds).ToString('o');duration_seconds=$preWatch.Elapsed.TotalSeconds;initial_inventory=$initial;passed=$preflightPassed;samples=$preRows}|ConvertTo-Json -Depth 7|Set-Content -LiteralPath (Join-Path $PSScriptRoot 'preflight-load.json') -Encoding utf8
if(!$preflightPassed){throw 'Browser or Regicide detected during preflight; no experimental game launched'}
Write-Output 'PASS 20-second preflight: standalone browsers 0; no user Regicide running'
$started=[DateTime]::UtcNow.ToString('o')
$file=(Get-Process -Id $PID).Path
$arguments=@('-NoProfile','-File',('"'+(Join-Path $PSScriptRoot 'run-match.ps1')+'"'))
$previous=Snapshot-Cpu
$watch=[Diagnostics.Stopwatch]::StartNew();$previousTime=0.0
$child=Start-Process -FilePath $file -ArgumentList $arguments -WorkingDirectory $repoRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $PSScriptRoot 'match-driver.stdout.log') -RedirectStandardError (Join-Path $PSScriptRoot 'match-driver.stderr.log')
$child.Handle|Out-Null
$rows=[Collections.Generic.List[object]]::new()
$contaminated=$false
do{
    Start-Sleep -Milliseconds 500
    $current=Snapshot-Cpu;$now=$watch.Elapsed.TotalSeconds
    $row=Delta-Row $previous $current $now ($now-$previousTime) 'match'
    $rows.Add($row)
    if($row.browser_processes -gt 0 -or $row.browser_cpu_seconds -gt 0 -or $row.unexpected_regicide_processes -gt 0 -or $row.regicide_processes -gt 2){
        if(!$contaminated){Write-Output 'CONTAMINATION: block will be preserved and excluded regardless of score'}
        $contaminated=$true
    }
    $previous=$current;$previousTime=$now;$child.Refresh()
}while(!$child.HasExited)
$child.WaitForExit();$child.Refresh()
$postRows=[Collections.Generic.List[object]]::new()
for($step=0;$step -lt 4;++$step){
    Start-Sleep -Milliseconds 500
    $current=Snapshot-Cpu;$now=$watch.Elapsed.TotalSeconds
    $postRows.Add((Delta-Row $previous $current $now ($now-$previousTime) 'postflight'))
    $previous=$current;$previousTime=$now
}
if(@($postRows|Where-Object {$_.browser_processes -gt 0 -or $_.browser_cpu_seconds -gt 0 -or $_.regicide_processes -gt 0}).Count -gt 0){$contaminated=$true}
[ordered]@{mode='same-condition-idle-rematch';started_utc=$started;finished_utc=[DateTime]::UtcNow.ToString('o');logical_cpus=[Environment]::ProcessorCount;interval_target_ms=500;elapsed_seconds=$watch.Elapsed.TotalSeconds;exit_code=$child.ExitCode;browser_load_criterion_passed=(!$contaminated);preflight_file='preflight-load.json';standalone_browser_pattern=$browserPattern;embedded_webview_policy='msedgewebview2 is observed separately; count need not be zero. Nonzero CPU is disclosed, not silently counted as zero browser rendering. No app process was terminated or suspended by the monitor.';sampling_limits='Endpoint CPU samples omit exited, inaccessible and sub-interval processes; lifetime charge on newly observed browser/webview can overcount boundary CPU. All-process CPU omits new non-browser processes. No affinity, thermal, clock, GPU or exclusive-host guarantee. Regicide sum includes the two comparison engines; no compiler, regression or independent audit runs during play. Names/PIDs are not persisted.';samples=$rows;postflight_samples=$postRows}|ConvertTo-Json -Depth 8|Set-Content -LiteralPath $output -Encoding utf8
Write-Output ('Completed idle rematch exit='+$child.ExitCode+' browser_criterion='+(!$contaminated)+' seconds='+$watch.Elapsed.TotalSeconds)
exit $child.ExitCode
