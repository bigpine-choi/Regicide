import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import time

root = Path(__file__).resolve().parents[2]
out = Path(__file__).resolve().parent
python = Path(sys.executable)
godot = Path('<user>/Tools/Godot/Godot_v4.7.2-stable_win64.exe')
binary = root / 'build-neural-v5/regicide.exe'
node = Path('<user>/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe')
commands = [('web-syntax', [node, '--check', root / 'web/human-play/app.js']),
            ('web-hints', [node, root / 'tests/web_hint_tests.js'])]
for name in ('godot_integration', 'handicap_godot', 'archive_godot', 'hint_godot'):
    commands.append((name, [python, root / f'tests/{name}_tests.py', '--godot', godot,
                           '--binary', binary, '--output', out / ('client-' + name)]))
summary = {'started_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
           'scope': 'Existing automated native headless scene and isolated real backend checks; no manual GUI game validation or strength timing claim.',
           'binary': str(binary), 'checks': []}
for name, command in commands:
    print('START', name, flush=True)
    start = time.monotonic()
    with (out / f'client-{name}.log').open('w', encoding='utf-8', newline='\n') as stream:
        result = subprocess.run(list(map(str, command)), cwd=root, stdout=stream,
                                stderr=subprocess.STDOUT, timeout=600,
                                env={**os.environ, 'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'})
    summary['checks'].append({'name': name, 'returncode': result.returncode,
                              'seconds': time.monotonic() - start,
                              'log': f'client-{name}.log'})
    (out / 'client-checks-summary.json').write_text(json.dumps(summary, indent=2) + '\n', encoding='utf-8')
    print('PASS' if result.returncode == 0 else 'FAIL', name, flush=True)
    if result.returncode:
        raise SystemExit(result.returncode)
summary['finished_utc'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
summary['all_passed'] = True
(out / 'client-checks-summary.json').write_text(json.dumps(summary, indent=2) + '\n', encoding='utf-8')
