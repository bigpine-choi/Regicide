import json, pathlib, subprocess, sys, time
sys.path.insert(0,str(pathlib.Path('scripts').resolve()))
from see_clean_guard import WindowsProcesses, EngineCpuGuard
out=pathlib.Path('build-see-cost')
guard=EngineCpuGuard(WindowsProcesses(),out/'detailed-search-profile.monitor.jsonl',interval=.5)
rows=[];failure=None
workloads=[('loss-scenes-search-profile-detailed-guarded',['build-see-cost/profile_see_search.exe'],'loss-scenes.fens')]
try:
 guard.start();guard.require_idle(10)
 for name,command,inputname in workloads:
  guard.sample(name+'-start');guard.assert_clean()
  with (out/inputname).open('rb') as source, (out/(name+'.json')).open('xb') as target, (out/(name+'.stderr.log')).open('xb') as error:
   child=subprocess.Popen(command,stdin=source,stdout=target,stderr=error)
   try:
    while child.poll() is None:
     guard.assert_clean();time.sleep(.1)
    guard.assert_clean()
    if child.returncode:raise RuntimeError(name+' failed with '+str(child.returncode))
   finally:
    if child.poll() is None:child.terminate();child.wait(timeout=10)
  rows.append({'name':name,'command':command,'input':inputname,'exit_code':child.returncode})
  guard.sample(name+'-finish');guard.assert_clean()
except Exception as error:
 failure=repr(error)
finally:
 guard.close()
 guard.summary['valid_for_comparison']=failure is None and not guard.summary['contaminated'] and len(rows)==len(workloads)
 guard.summary['workload_kind']='standalone SEE microprofile and separately instrumented search, no UCI child transports'
 (out/'detailed-search-profile.monitor.json').write_text(json.dumps(guard.summary,indent=2)+'\n',encoding='utf-8')
 (out/'detailed-search-profile-run.json').write_text(json.dumps({'workloads':rows,'failure':failure},indent=2)+'\n',encoding='utf-8')
print(json.dumps({'workloads':len(rows),'failure':failure,'valid':guard.summary['valid_for_comparison'],'external_cpu_increase_ticks':guard.summary['external_cpu_increase_ticks'],'snapshots':guard.summary['snapshot_count']},indent=2))
if failure:raise SystemExit(1)
