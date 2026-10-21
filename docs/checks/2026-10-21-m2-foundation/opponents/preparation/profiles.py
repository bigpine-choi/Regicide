import hashlib
import json
import os
from pathlib import Path
import sys

import chess
import chess.engine

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from engine_profile import load_profile, snapshot_profile, verify_profile, compare_arguments

BASE = ROOT / 'build-m2-opponents'
PROFILES = BASE / 'profiles'
PROFILES.mkdir(exist_ok=True)
EVIDENCE = ROOT / 'docs/checks/2026-10-21-m2-foundation/opponents'
report_path = EVIDENCE / 'provenance.json'
report = json.loads(report_path.read_text(encoding='utf-8'))
assert report['status'] == 'PASS'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def relative(path):
    return os.path.relpath(path, PROFILES).replace('\\', '/')


def artifact(path, role, target):
    return {'path': relative(path), 'sha256': sha(path), 'role': role, 'relative_path': target}


profiles = {}
sf = report['stockfish']
sf_binary = ROOT / sf['binary_path']
sf_license = next((BASE / 'stockfish18/package').rglob('Copying.txt'))
profiles['stockfish18'] = {'schema_version': 1, 'id': 'stockfish18-x86-64', 'kind': 'external',
    'evaluation': 'external', 'binary': {'path': relative(sf_binary), 'sha256': sha(sf_binary)},
    'args': [], 'options': {'Threads': 1, 'Hash': 16, 'UCI_LimitStrength': False, 'Skill Level': 20, 'SyzygyPath': ''},
    'source': {key: sf[key] for key in ('name', 'version', 'source_url', 'revision', 'license', 'license_url')},
    'models': {}, 'artifacts': [artifact(sf_license, 'license', 'artifacts/Copying.txt')]}
sun = report['sunfish']
sun_binary = ROOT / sun['binary_path']
source_dir = BASE / 'sunfish/source'
sun_artifacts = [artifact(source_dir / path, 'license' if path == 'LICENSE.md' else 'engine-source', 'artifacts/engine/' + path)
                 for path in ['sunfish.py', 'sunfish_ui/uci.py', 'sunfish_ui/__init__.py', 'LICENSE.md']]
sun_artifacts.append(artifact(sun_binary.parent / 'pyvenv.cfg', 'runtime-config', 'binary/pyvenv.cfg'))
profiles['sunfish'] = {'schema_version': 1, 'id': 'sunfish-e396948-cpython313', 'kind': 'external',
    'evaluation': 'external', 'binary': {'path': relative(sun_binary), 'sha256': sha(sun_binary)},
    'args': ['-u', relative(source_dir / 'sunfish.py')], 'options': {'Hash': 16},
    'source': {key: sun[key] for key in ('name', 'version', 'source_url', 'revision', 'license', 'license_url')},
    'models': {}, 'artifacts': sun_artifacts}
summary = {'status': 'RUNNING', 'profiles': {}}
(EVIDENCE / 'profile-smoke.json').write_text(json.dumps(summary, indent=2) + '\n', encoding='utf-8')
for key, literal in profiles.items():
    profile_path = PROFILES / (key + '.json')
    profile_path.write_text(json.dumps(literal, indent=2) + '\n', encoding='utf-8')
    profile = load_profile(profile_path)
    snapshot = snapshot_profile(profile, BASE / 'smoke-snapshots' / key)
    verify_profile(profile)
    verify_profile(snapshot)
    arguments = compare_arguments(snapshot)
    engine = chess.engine.SimpleEngine.popen_uci([arguments['binary'], *arguments['args']], timeout=15)
    try:
        engine.configure(arguments['options'])
        engine.ping()
        board = chess.Board()
        answer = engine.play(board, chess.engine.Limit(time=.05), info=chess.engine.INFO_ALL)
        assert answer.move is not None and answer.move in board.legal_moves
        engine_id = dict(engine.id)
    finally:
        engine.quit()
        engine.close()
    verify_profile(profile)
    verify_profile(snapshot)
    summary['profiles'][key] = {'input_profile': str(profile_path.relative_to(ROOT)),
        'input_profile_sha256': sha(profile_path), 'normalized_sha256': profile['normalized_sha256'],
        'snapshot_profile': str(Path(snapshot['manifest_path']).relative_to(ROOT)),
        'snapshot_profile_sha256': snapshot['manifest_sha256'], 'snapshot_binary_sha256': snapshot['binary']['sha256'],
        'snapshot_artifacts': [{'relative_path': entry['relative_path'], 'sha256': entry['sha256'], 'role': entry['role']} for entry in snapshot['artifacts']],
        'snapshot_args': snapshot['args'], 'engine_id': engine_id, 'legal_bestmove': answer.move.uci(),
        'validated_requested_options': arguments['options'], 'verification_before_and_after': True,
        'scope': 'copied profile snapshot UCI handshake/configuration/legal move only; not a strength measurement'}
    evidence_profile = {name: value for name, value in profile.items()
                        if name not in {'manifest_path', 'manifest_sha256', 'normalized_sha256'}}
    (EVIDENCE / (key + '-profile.json')).write_text(json.dumps(evidence_profile, indent=2) + '\n', encoding='utf-8')
    print(key + ' profile + snapshot + UCI smoke: PASS', flush=True)
summary['status'] = 'PASS'
summary['sunfish_runtime_dependency'] = json.loads(report['sunfish']['runtime_probe_stdout'])
summary['sunfish_runtime_dependency']['base_executable_sha256'] = sha(Path(summary['sunfish_runtime_dependency']['base_executable']))
summary['sunfish_known_environment_observed'] = {name: os.environ.get(name) for name in ['TM_MANAGER', 'TM_OVERHEAD_MS', 'TM_SOFT_SCALE', 'TM_PHASE_M', 'TM_DYNAMIC']}
summary['resource_note'] = 'Hash 16 is the engines advertised setting; Sunfish maps MB approximately to Python entries and this is not a measured equal physical-memory cap.'
(EVIDENCE / 'profile-smoke.json').write_text(json.dumps(summary, indent=2) + '\n', encoding='utf-8')
