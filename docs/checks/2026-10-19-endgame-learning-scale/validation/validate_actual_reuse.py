import copy
import json
from pathlib import Path
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
import collect_learning_data as D
import endgame_study as S

live_state = ROOT / 'build/learning-cycle'
relative = 'runs/000005-1cf7521bd5dc'
live = live_state / relative
before = {str(path): D.sha256_file(path) for path in live.rglob('*') if path.is_file()}
for name in ('catalog.json', 'latest.json'):
    before[str(live_state / name)] = D.sha256_file(live_state / name)
catalog = json.loads((live_state / 'catalog.json').read_text(encoding='utf-8'))
origin = json.loads((live / 'cycle.json').read_text(encoding='utf-8'))
with tempfile.TemporaryDirectory(prefix='regicide-data-reuse-audit-') as temporary:
    state = Path(temporary).resolve() / 'state'
    copied = state / relative
    copied.parent.mkdir(parents=True)
    shutil.copytree(live, copied)
    target_relative = 'runs/000006-reuse-audit'
    target = state / target_relative
    target.mkdir()
    executable = 'regicide.exe' if sys.platform == 'win32' else 'regicide'
    shutil.copyfile(copied / executable, target / executable)
    trial_catalog = copy.deepcopy(catalog)
    trial_catalog['attempts_started'] = 6
    trial_catalog['runs'].append(target_relative)
    reused = S.reuse_completed_data(state, trial_catalog, relative, target,
                                   origin['binary_sha256'], origin['configuration'])
    assert trial_catalog['datasets'] == catalog['datasets']
    assert trial_catalog['endgame_holdouts'] == catalog['endgame_holdouts']
    copied_files = len(reused['files_sha256'])
for path, digest in before.items():
    assert D.sha256_file(path) == digest, path
report = {'status': 'passed', 'scope': 'Actual attempt-5 data copied only to a temporary isolated state, validated and copied into another temporary run; no native searches, no live-state writes.',
          'copied_files': copied_files, 'protected_live_files': len(before),
          'source_attempt': 5, 'new_trial_attempt': 6, 'dataset_or_holdout_duplicate_registrations': 0}
Path(__file__).with_name('actual-reuse-audit.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
print(json.dumps(report))
