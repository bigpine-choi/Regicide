import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

import chess
import numpy

ROOT = Path(__file__).resolve().parents[2]
binary = ROOT/'build-neural-v5/regicide.exe'
cpu = subprocess.check_output(['powershell', '-NoProfile', '-Command',
    'Get-CimInstance Win32_Processor | Select-Object Name,NumberOfCores,NumberOfLogicalProcessors | ConvertTo-Json -Compress'], text=True)
record = {
    'checked_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
    'platform': platform.platform(), 'machine': platform.machine(),
    'cpu': json.loads(cpu), 'logical_cpus': os.cpu_count(),
    'python': sys.version, 'numpy': numpy.__version__, 'python_chess': chess.__version__,
    'training_thread_environment': {name: os.environ.get(name) for name in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS')},
    'engine_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
    'source_commit': subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip(),
    'source_status': subprocess.check_output(['git', '-C', str(ROOT), 'status', '--porcelain'], text=True).splitlines(),
    'preparation': 'Fixed work, other applications allowed; no timing or playing-strength claim from preparation wall time.',
    'timed_validation': 'Separate GitHub Windows runner with native resource guard; local PC use is unrestricted.',
}
target = ROOT/'build/endgame-learning-scale/environment-study6.json'
target.write_text(json.dumps(record, indent=2)+'\n', encoding='utf-8')
print(json.dumps(record, indent=2))
