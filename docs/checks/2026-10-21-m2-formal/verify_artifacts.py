"""Validate preserved raw archives and write a new byte integrity manifest."""
import argparse
from datetime import datetime,timezone
import gzip
import hashlib
import json
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[3]
HERE=Path(__file__).resolve().parent
def sha(data):return hashlib.sha256(data).hexdigest()
def verify(output):
 archives=[]
 for path in sorted((HERE/'attempts').glob('*/archive.json')):
  record=json.loads(path.read_text());count=0
  for row in record['files']:
   target=ROOT/row.get('archive',row.get('canonical_committed_file',''))
   assert target.is_file(),str(target)
   data=target.read_bytes()
   if row.get('compression'):
    assert row['compression']=='gzip-mtime0'
    assert sha(data)==row['archive_sha256'],str(target)
    data=gzip.decompress(data)
   assert sha(data)==row['sha256'],str(target)
   if 'size' in row:assert len(data)==row['size'],str(target)
   count+=1
  if 'canonical_exposure_index' in record:
   entry=record['canonical_exposure_index']
   assert sha((HERE/entry['path']).read_bytes())==entry['sha256']
  archives.append({'attempt':path.parent.name,'verified_files':count})
 files=set(HERE.rglob('*'))|set((HERE.parent/'2026-10-21-m3-huber-preparation').rglob('*'))
 names=('scripts/m2_formal.py','scripts/m2_load_guard.py','scripts/m2_formal_summary.py',
  'scripts/prepare_m2_formal_openings.py','scripts/neural_precision.py','scripts/prepare_neural_huber.py',
  'tests/m2_formal_tests.py','tests/m2_load_guard_tests.py','tests/m2_formal_openings_tests.py','tests/neural_huber_tests.py',
  'tests/data/m2_formal_openings_2026-10-21.json','tests/data/m2_preflight_openings_2026-10-21.json',
  'docs/experiments/2026-10-21-m2-formal-plan.json','docs/experiments/2026-10-21-m2-formal-plan.md',
  'docs/experiments/2026-10-21-m2-formal-outcome.md','docs/experiments/2026-10-21-m3-huber-preparation.md',
  'docs/ROADMAP.md')
 files.update(ROOT/name for name in names)
 selected=[path for path in sorted(files) if path.is_file() and path.resolve()!=output.resolve() and '__pycache__' not in path.parts]
 names=[path.relative_to(ROOT).as_posix() for path in selected]
 ignored=subprocess.run(['git','check-ignore','--stdin'],cwd=ROOT,input='\n'.join(names)+'\n',capture_output=True,text=True)
 if ignored.returncode not in (0,1):raise RuntimeError(ignored.stderr)
 excluded=set(ignored.stdout.splitlines())
 entries={path.relative_to(ROOT).as_posix():{'sha256':sha(path.read_bytes()),'size':path.stat().st_size}
          for path in selected if path.relative_to(ROOT).as_posix() not in excluded}
 return {'status':'PASS_FOR_ARTIFACT_INTEGRITY_ONLY','verified_utc':datetime.now(timezone.utc).isoformat(),
  'main_status':'STOPPED','main_valid_for_comparison':False,'main_completed_games':176,'main_scheduled_games':768,
  'archived_raw_files':archives,'files':entries,'intentionally_local_ignored_files':sorted(excluded),
  'scope':'Checks preserved byte identities and gzip round trips. This is not a strength pass or a complete hermetic engine/runtime distribution.'}

if __name__=='__main__':
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
 if args.output.exists():raise ValueError('preserve the previous manifest')
 result=verify(args.output)
 with args.output.open('x',encoding='utf-8') as stream:json.dump(result,stream,indent=2);stream.write('\n')
 print(json.dumps({'status':result['status'],'manifest_files':len(result['files']),'archives':result['archived_raw_files']}))
