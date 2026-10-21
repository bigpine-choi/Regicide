"""Actual Windows background sampling while orchestration hashes and waits."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[4]
sys.path.insert(0,str(ROOT/'scripts'))
from m2_load_guard import GuardMonitor,LoadPolicy

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--output',type=Path,required=True)
args=parser.parse_args()
args.output.mkdir(parents=True,exist_ok=False)
monitor=GuardMonitor(args.output/'load.jsonl',LoadPolicy(max_nonworker_cpu_fraction=.2))
error=None
try:
    monitor.require_idle(2.1)
    monitor.start_timer()
    data=b'bounded orchestration workload'*400000
    start=time.monotonic()
    while time.monotonic()-start<.75:
        hashlib.sha256(data).digest()
    time.sleep(2.4)
    monitor.assert_clean()
except Exception as exc:
    error=f'{type(exc).__name__}: {exc}'
finally:
    monitor.close()
summary=monitor.summary
valid=bool(error is None and summary['valid_for_load_check'] and summary['timer']['samples']>=5)
receipt={'status':'PASS' if valid else 'FAIL','summary':summary,'error':error,
         'source_sha256':hashlib.sha256((ROOT/'scripts/m2_load_guard.py').read_bytes()).hexdigest(),
         'probe_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
         'scope':'Actual Windows idle/hash/no-poll wait; no engine or user process started/stopped.'}
(args.output/'result.json').write_text(json.dumps(receipt,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'status':receipt['status'],'timer_samples':summary['timer']['samples'],
                  'max_gap_seconds':summary['max_sample_gap_seconds'],
                  'max_nonworker_cpu_fraction':summary['max_nonworker_cpu_fraction']}))
sys.exit(0 if valid else 2)
