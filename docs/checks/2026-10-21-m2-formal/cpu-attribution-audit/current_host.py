"""Read-only current CPU trace; not retrospective attribution of old failures."""
import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[4]
sys.path.insert(0,str(ROOT/'scripts'))
from see_clean_guard import WindowsProcesses

def utc():return datetime.now(timezone.utc).isoformat()
def run(output,seconds):
 output.mkdir(parents=True,exist_ok=False)
 native=WindowsProcesses();kernel=native.kernel
 kernel.GetSystemTimes.argtypes=[ctypes.POINTER(wintypes.FILETIME)]*3
 kernel.GetSystemTimes.restype=wintypes.BOOL
 held={};errors={}
 def system():
  values=[wintypes.FILETIME() for _ in range(3)]
  if not kernel.GetSystemTimes(*(ctypes.byref(x) for x in values)):raise ctypes.WinError(ctypes.get_last_error())
  return [x.dwLowDateTime+(x.dwHighDateTime<<32) for x in values]
 def snapshot():
  began=time.monotonic();start_utc=utc();anchor_before=system()
  inventory=native.snapshot()
  for pid,p in inventory.items():
   if pid==0 or pid in held:continue
   handle=kernel.OpenProcess(0x101000,False,pid)
   if not handle:
    errors[pid]={'pid':pid,'name':p.name,'parent_pid':p.parent_pid,'error':ctypes.get_last_error()};continue
   measured=native._handle_times(handle,p)
   if measured.error:
    kernel.CloseHandle(handle);errors[pid]={'pid':pid,'name':p.name,'error':measured.error};continue
   held[pid]=(handle,measured);errors.pop(pid,None)
  measured={}
  for pid,(handle,p) in list(held.items()):
   value=native._handle_times(handle,p)
   measured[str(pid)]=asdict(value)
   if not value.error:held[pid]=(handle,value)
  anchor_after=system();end=time.monotonic()
  return {'utc':start_utc,'monotonic_end':end,'capture_seconds':end-began,
   'system_before':anchor_before,'system_after':anchor_after,'processes':measured,
   'inaccessible_current':[item for pid,item in errors.items() if pid in inventory],
   'inventory':[{'pid':p.pid,'parent_pid':p.parent_pid,'name':p.name} for p in inventory.values()]}
 receipt={'status':'RUNNING','started_utc':utc(),'pid':os.getpid(),'logical_cpus':os.cpu_count(),
  'probe_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'interval_seconds':2,
  'seconds':seconds,'windows':[],'scope':'Current read-only process counters and PID/name/parent inventory. No commands, environment, memory, window contents, process control or past-cause inference. Diagnostic CPU is included. Protected counters and never-seen short-lived processes can be missing; reads are not simultaneous.'}
 raw=(output/'snapshots.jsonl').open('x',encoding='utf-8')
 try:
  previous=snapshot();raw.write(json.dumps(previous)+'\n');raw.flush()
  for i in range(max(1,seconds//2)):
   time.sleep(2);current=snapshot();raw.write(json.dumps(current)+'\n');raw.flush()
   old=previous['system_after'];new=current['system_after'];capacity=(new[1]+new[2])-(old[1]+old[2]);idle=new[0]-old[0]
   assert capacity>0 and 0<=idle<=capacity
   rows=[]
   for pid,p in current['processes'].items():
    before=previous['processes'].get(pid)
    if p['error'] or before is None or before['error'] or before['creation_ticks']!=p['creation_ticks']:continue
    delta=p['cpu_ticks']-before['cpu_ticks'];assert delta>=0
    if delta:rows.append({'pid':p['pid'],'name':p['name'],'parent_pid':p['parent_pid'],
     'creation_ticks':p['creation_ticks'],'cpu_seconds':delta/1e7,'fraction_of_all_logical':delta/capacity,
     'ended':p['exited_during_snapshot']})
   rows.sort(key=lambda x:x['cpu_seconds'],reverse=True)
   receipt['windows'].append({'index':i+1,'start_utc':previous['utc'],'end_utc':current['utc'],
    'elapsed_seconds':current['monotonic_end']-previous['monotonic_end'],
    'aggregate_host_busy_fraction':(capacity-idle)/capacity,
    'observed_process_fraction':sum(x['fraction_of_all_logical'] for x in rows),
    'top_processes':rows[:15],'inaccessible_count':len(current['inaccessible_current']),
    'capture_seconds':current['capture_seconds']})
   previous=current
  receipt['status']='COMPLETE'
 finally:
  raw.close()
  for handle,p in held.values():kernel.CloseHandle(handle)
  native.close();receipt['finished_utc']=utc()
  (output/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n',encoding='utf-8')
 print(json.dumps({'status':receipt['status'],'probe_pid':receipt['pid'],'windows':[
  {'host_fraction':x['aggregate_host_busy_fraction'],'top':x['top_processes'][:3]} for x in receipt['windows']]}))

if __name__=='__main__':
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True);parser.add_argument('--seconds',type=int,default=24);args=parser.parse_args();run(args.output,args.seconds)
