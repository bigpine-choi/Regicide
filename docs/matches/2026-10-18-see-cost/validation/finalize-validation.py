"""Finalize preserved twenty-suite logs plus the timestamped source gate.

No engine is launched here. All final evidence files use exclusive creation.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate-commit')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    folder = Path(__file__).resolve().parent
    freeze = json.loads((root / 'tests/data/see_cost_freeze_2026-10-18.json').read_text(encoding='utf-8-sig'))
    frozen = args.candidate_commit or freeze['candidate']['commit']
    assert re.fullmatch(r'[0-9a-f]{40}', frozen or ''), 'Supply the frozen source commit'
    assert not freeze['candidate']['commit'] or frozen == freeze['candidate']['commit'], 'Wrong frozen source'
    summary_path = folder / 'validation-summary.json'
    source_log = folder / 'validation-frozen-engine-source.log'
    assert not summary_path.exists() and not source_log.exists(), 'Preserve prior final evidence'
    state = json.loads((folder / 'validation-run-state.json').read_text(encoding='utf-8-sig'))
    suites = state['suites']
    assert state['schema_version'] == 1 and state['engine_source_commit'] == frozen
    required = set(freeze['required_validation_checks'])
    assert len(suites) == 20 and len({item['label'] for item in suites}) == 20
    assert {item['label'] for item in suites} == required - {'frozen-engine-source'}
    assert state['completed_utc'] is not None
    checks = []
    for item in suites:
        path = Path(item['log'])
        contents = path.read_text(encoding='utf-8-sig')
        skips = max([int(n) for n in re.findall(r'skipped=(\d+)', contents)] or [0])
        checks.append({'name': item['label'], 'result': 'passed' if item['exit_code'] == 0 else 'failed',
                       'log': path.relative_to(root).as_posix(), 'sha256': sha256(path),
                       'exit_code': item['exit_code'], 'started_utc': item['started_utc'],
                       'ended_utc': item['ended_utc'], 'skipped_cases': skips})
    scopes = ['src', 'include', 'CMakeLists.txt', 'scripts/build.ps1']
    source_started = datetime.now(timezone.utc).isoformat()
    diff = subprocess.run(['git', 'diff', '--exit-code', frozen, '--', *scopes], cwd=root,
                          text=True, capture_output=True)
    untracked = subprocess.run(['git', 'ls-files', '--others', '--exclude-standard', '--', *scopes],
                               cwd=root, text=True, capture_output=True, check=True)
    source_ok = diff.returncode == 0 and not untracked.stdout.strip()
    source_ended = datetime.now(timezone.utc).isoformat()
    with source_log.open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(f'frozen_source_commit={frozen}\nscopes={scopes!r}\nstarted_utc={source_started}\n'
                     f'ended_utc={source_ended}\ndiff_exit_code={diff.returncode}\n'
                     f'untracked={untracked.stdout!r}\n{diff.stdout}{diff.stderr}')
    checks.append({'name': 'frozen-engine-source', 'result': 'passed' if source_ok else 'failed',
                   'log': source_log.relative_to(root).as_posix(), 'sha256': sha256(source_log),
                   'exit_code': 0 if source_ok else 1, 'skipped_cases': 0,
                   'started_utc': source_started, 'ended_utc': source_ended})
    binaries = {'baseline': root / 'build-see-native/regicide.exe',
                'candidate': root / 'build-see-cost/regicide.exe'}
    actual = {role: sha256(path) for role, path in binaries.items()}
    assert actual == state['binary_sha256'], 'Binaries changed during validation'
    assert actual['baseline'] == '2ba41c8303f266149323b55186f5c6a806c33f73d529d6911d6ca4b33bfa53db'
    if freeze['candidate']['sha256']:
        assert actual['candidate'] == freeze['candidate']['sha256'], 'Wrong frozen candidate binary'
    groups = assertions = 0
    for check in checks:
        if not check['name'].startswith('native-'):
            continue
        text = (root / check['log']).read_text(encoding='utf-8-sig')
        summaries = re.findall(r'(?m)^(\d+) groups, (\d+) assertions, (\d+) failures\s*$', text)
        assert len(summaries) == 1 and int(summaries[0][2]) == 0, check['name']
        groups += int(summaries[0][0])
        assertions += int(summaries[0][1])
    summary = {'all_passed': all(check['result'] == 'passed' for check in checks),
               'binary_sha256': actual, 'engine_source_commit': frozen,
               'completed_utc': datetime.now(timezone.utc).isoformat(),
               'suite_count': len(suites), 'checks_count': len(checks),
               'candidate_native_groups': groups, 'candidate_native_assertions': assertions,
               'skipped_case_count': sum(check['skipped_cases'] for check in checks),
               'skip_note': 'Windows symlink permission case is expected to be skipped when unavailable; no entire suite is skipped.',
               'mode': 'Native, Python and headless Godot regression; no manual GUI validation.',
               'checks': checks}
    with summary_path.open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({key: value for key, value in summary.items() if key != 'checks'}, indent=2))
    return 0 if summary['all_passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
