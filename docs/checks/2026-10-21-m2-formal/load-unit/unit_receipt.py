"""Preserve the exact mock-test invocation, output and before/after hashes."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[4]


def hashes():
    return {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in (
        'scripts/m2_load_guard.py', 'tests/m2_load_guard_tests.py', 'scripts/see_clean_guard.py')}


parser = argparse.ArgumentParser()
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
args.output.mkdir(parents=True, exist_ok=False)
command = [sys.executable, 'tests/m2_load_guard_tests.py', '-v']
before = hashes()
with (args.output / 'tests.txt').open('x', encoding='utf-8', newline='\n') as stream:
    result = subprocess.run(command, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT)
receipt = {'command': command, 'cwd': str(ROOT), 'python_version': sys.version,
           'helper_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
           'exit_code': result.returncode, 'before': before, 'after': hashes(),
           'scope': 'Synthetic process identities/CPU counters only; no native process launched by unit tests.'}
(args.output / 'execution.json').write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
print(json.dumps({'exit_code': result.returncode, 'before': before, 'after': receipt['after']}, indent=2))
raise SystemExit(result.returncode)
