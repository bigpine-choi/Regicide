import hashlib, json, re, subprocess
from pathlib import Path
from datetime import datetime, timezone

root = Path.cwd()
folder = root / 'build/see-clean-evidence'
state = json.loads((folder / 'validation-run-state.json').read_text(encoding='utf-8-sig'))
assert len(state) == 20, f'Only {len(state)} of 20 requested suites completed'
checks = []
for item in state:
    path = Path(item['log'])
    contents = path.read_text(encoding='utf-8-sig')
    matches = re.findall(r'skipped=(\d+)', contents)
    item_skips = max([int(n) for n in matches] or [0])
    checks.append({'name': item['label'], 'result': 'passed' if item['exit_code'] == 0 else 'failed',
                   'log': str(path.relative_to(root)).replace('\\', '/'),
                   'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                   'exit_code': item['exit_code'], 'started_utc': item['started_utc'],
                   'ended_utc': item['ended_utc'], 'skipped_cases': item_skips})
scopes = ['src', 'include', 'CMakeLists.txt', 'scripts/build.ps1']
frozen = '0edc06e437b934d177fc1e9b0907600b6287be72'
diff = subprocess.run(['git', 'diff', '--exit-code', frozen, '--', *scopes], text=True, capture_output=True)
untracked = subprocess.run(['git', 'ls-files', '--others', '--exclude-standard', '--', *scopes], text=True, capture_output=True, check=True)
source_ok = diff.returncode == 0 and not untracked.stdout.strip()
source_log = folder / 'validation-frozen-engine-source.log'
source_log.write_text(f'frozen_source_commit={frozen}\nscopes={scopes!r}\ndiff_exit_code={diff.returncode}\nuntracked={untracked.stdout!r}\n{diff.stdout}{diff.stderr}', encoding='utf-8')
checks.append({'name': 'frozen-engine-source', 'result': 'passed' if source_ok else 'failed',
               'log': str(source_log.relative_to(root)).replace('\\', '/'),
               'sha256': hashlib.sha256(source_log.read_bytes()).hexdigest(),
               'exit_code': diff.returncode, 'skipped_cases': 0})
expected = {'baseline': 'c333ccfa7ec3efbaa7f52df4c007875e64f68d0816a847c1390aa3213d0e427b',
            'candidate': '2ba41c8303f266149323b55186f5c6a806c33f73d529d6911d6ca4b33bfa53db'}
binaries = {'baseline': root / 'build-see-baseline/source/build/regicide.exe',
            'candidate': root / 'build-see-native/regicide.exe'}
actual = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in binaries.items()}
assert actual == expected, (actual, expected)
native = [check for check in checks if check['name'].startswith('native-')]
groups = assertions = 0
for check in native:
    text = (root / check['log']).read_text(encoding='utf-8-sig')
    result = re.search(r'(\d+) groups, (\d+) assertions, (\d+) failures', text)
    assert result and int(result[3]) == 0, check['name']
    groups += int(result[1]); assertions += int(result[2])
skips = sum(check['skipped_cases'] for check in checks)
summary = {'all_passed': all(check['result'] == 'passed' for check in checks),
           'binary_sha256': actual, 'engine_source_commit': frozen,
           'completed_utc': datetime.now(timezone.utc).isoformat(),
           'suite_count': len(state), 'checks_count': len(checks),
           'candidate_native_groups': groups, 'candidate_native_assertions': assertions,
           'skipped_case_count': skips,
           'skip_note': 'Windows symlink permission test is expected to be skipped when unavailable; no entire suite is skipped.',
           'mode': 'Native tests, Python tests, and headless Godot scene integration; no manual GUI validation.',
           'checks': checks}
(folder / 'validation-summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print(json.dumps({key: value for key, value in summary.items() if key != 'checks'}, ensure_ascii=False, indent=2))
assert summary['all_passed']
