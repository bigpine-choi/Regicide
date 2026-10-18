import hashlib,json,subprocess
from pathlib import Path
root=Path.cwd(); out=root/'build/neural-cost-quality'; state=root/'build/learning-cycle'
base='25d37d8173da9e72bbc4b20178e2933ac4640911'
protected=['scripts/learning_arena.py','scripts/learning_registry.py','tests/data/learning_arena_openings_v1.json',
 'src/board.cpp','src/search.cpp','src/evaluation.cpp','src/neural.cpp','src/see.cpp','src/transposition.cpp',
 *[p.as_posix() for p in Path('include/regicide').glob('*.hpp')]]
objects={}
for name in protected:
 old=subprocess.check_output(['git','rev-parse',base+':'+name],text=True).strip()
 new=subprocess.check_output(['git','rev-parse','HEAD:'+name],text=True).strip()
 objects[name]={'baseline_blob':old,'current_blob':new,'identical':old==new}
original=json.loads((out/'original-state-sha256.json').read_text(encoding='utf-8'))
mutable={'catalog.json','latest.json','cycle.lock'}
old_files={}
for relative,expected in original.items():
 if relative in mutable: continue
 path=state/relative
 actual=hashlib.sha256(path.read_bytes()).hexdigest()
 old_files[relative]={'sha256':actual,'identical':actual==expected}
result={'baseline_commit':base,'protected_source_files':objects,'historical_state_files':old_files,
        'mutable_catalog_latest_and_OS_lock_excluded':sorted(mutable),
        'passed':all(x['identical'] for x in objects.values()) and all(x['identical'] for x in old_files.values())}
(out/'protection.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
print('Protected source files:',len(objects),'historical files:',len(old_files),'passed:',result['passed'])
if not result['passed']: raise SystemExit(1)
