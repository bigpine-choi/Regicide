"""Reproduce default/material/neural equivalence against preserved b007323 binary.
Run from repository root: python docs/checks/2026-10-19-neural-incremental/compare_previous.py
"""
from pathlib import Path
import subprocess, json, hashlib, time, statistics
root=Path(__file__).resolve().parents[3]
out=Path(__file__).with_name('previous-baseline.json')
old=root/'build-neural/regicide.exe'
new=root/'build-neural-incremental/regicide.exe'
def digest(path): return hashlib.sha256(path.read_bytes()).hexdigest()
expected=json.loads((root/'docs/checks/2026-10-19-neural/manifest.json').read_text())['sha256']['build-neural/regicide.exe']
assert digest(old)==expected, 'preserved b007323 binary differs'
positions=json.loads((root/'tests/data/profile_positions.json').read_text())['positions']
def run(exe,position,flags):
 command=[str(exe),'search','4',*flags]
 if 'fen' in position: command+=['--fen',position['fen']]
 if position.get('moves'): command+=['--play',*position['moves']]
 t=time.perf_counter_ns()
 output=subprocess.check_output(command,text=True,timeout=60)
 wall=time.perf_counter_ns()-t
 fields={k:v for line in output.splitlines() if ':' in line for k,v in [line.split(':',1)]}
 return {'command':command,'wall_ns':wall,'fields':fields,'stdout':output}
rows=[]
for name,flags,repeats in [('classical',[],3),('material',['--no-position'],1),('neural',['--neural'],1)]:
 for repeat in range(repeats):
  for i,position in enumerate(positions):
   pair={'mode':name,'repeat':repeat,'position':position['name']}
   for role in (['old','new'] if (i+repeat)%2==0 else ['new','old']):
    pair[role]=run(old if role=='old' else new,position,flags)
   pair['equal']=all(pair['old']['fields'].get(k)==pair['new']['fields'].get(k) for k in pair['old']['fields'].keys()|pair['new']['fields'].keys() if k!='time_ms')
   assert pair['equal'],pair
   rows.append(pair)
classical=[r for r in rows if r['mode']=='classical']
doc={'old_base_commit':'b0073231ad25a41becadfbab0eff3c5f4b2f7363','old_sha256':digest(old),'new_sha256':digest(new),'depth':4,'rows':rows,'pairs':len(rows),'mismatches':sum(not r['equal'] for r in rows),'classical_wall_ratio_new_over_old':statistics.median(r['new']['wall_ns']/r['old']['wall_ns'] for r in classical),'classical_engine_ratio_new_over_old':statistics.median(int(r['new']['fields']['time_ms'])/int(r['old']['fields']['time_ms']) for r in classical if int(r['old']['fields']['time_ms'])>0),'limitations':'process startup included in wall; engine time integer ms; no affinity or load sampling; old/new is two different builds'}
out.write_text(json.dumps(doc,indent=2))
print({k:v for k,v in doc.items() if k!='rows'})
