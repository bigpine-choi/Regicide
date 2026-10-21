"""Rerun only own-Job termination after fixing the test's wait/read ordering."""
import hashlib
import json
from pathlib import Path
import runpy
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT/'scripts'))
import m2_job_accounting as accounting

test = ROOT/'tests/m2_job_load_guard_integration.py'
namespace = runpy.run_path(str(test))
hashes = lambda: {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in
                  (test, ROOT/'scripts/m2_job_accounting.py', ROOT/'scripts/m2_job_load_guard.py',
                   ROOT/'scripts/m2_load_guard.py', ROOT/'scripts/see_clean_guard.py')}
before = hashes()
result = namespace['termination_case'](Path(__file__).parent/'native-termination-attempt-002', accounting)
after = hashes()
receipt = {'command': [sys.executable, *sys.argv], 'python_version': sys.version,
           'scope': 'Own single sleeping process termination only; no engine games or formal load validation.',
           'original_failure_preserved': 'native-integration-attempt-001/termination/result.json',
           'test_fix': 'Wait for the exact Popen process handle before reading the final per-process exit timestamp.',
           'source_sha256_before': before, 'source_sha256_after': after,
           'source_unchanged_during_execution': before == after, 'result': result}
namespace['write'](Path(__file__).parent/'termination-retry-execution.json', receipt)
print(json.dumps({'passed': result['passed'], 'source_unchanged': before == after,
                  'active_processes': result['cleanup']['active_processes']}))
raise SystemExit(0 if result['passed'] and before == after else 1)
