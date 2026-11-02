"""Independent v1 replay/v2 migration audit using only synthetic records."""
from pathlib import Path
from contextlib import closing
import copy, hashlib, json, sqlite3, subprocess, sys, tempfile, types, datetime, re

root = Path(__file__).resolve().parents[2]
out = Path(__file__).resolve().parent
sys.path[:0] = [str(root / 'scripts'), str(root / 'tests')]
import sparring_rating as current
from sparring_rating_tests import fixture
import chess

source = subprocess.check_output(['git', 'show', '32d9e644bd0c971c332ecbded47debe0404784d4:scripts/sparring_rating.py'], cwd=root)
legacy = types.ModuleType('rating_legacy_audit_reference')
exec(compile(source, '<pinned-v1-rating>', 'exec'), legacy.__dict__)
paths = ['scripts/sparring_rating.py', 'scripts/boss_play.py', 'scripts/rating_evidence.py',
         'scripts/play_human.py', 'tests/rating_observation_host_tests.py']
def hashes():
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in paths}

before = hashes()
checks, cases = [], 0
for color in (chess.WHITE, chess.BLACK):
    for losses in ([0], [0, 300, 25, 800, 40], [0] * 43, [500, 20, 1200, 10] * 12):
        board, rows = fixture(losses, color)
        for offset in (-250.5, 0., 300.):
            model = {**legacy.prior('audit-eval'), 'calibration_games': 3 if offset else 0, 'offset': offset}
            data = legacy.metadata(board, rows, color, model)
            original = copy.deepcopy(data)
            for length in sorted({0, 1, 2, len(board.move_stack) // 2, len(board.move_stack) - 1, len(board.move_stack)}):
                prefix = board.copy(stack=True)
                while len(prefix.move_stack) > length:
                    prefix.pop()
                assert current.archive_snapshot(prefix, data) == legacy.archive_snapshot(prefix, data), (color, losses, offset, length)
                cases += 1
            assert data == original
checks.append({'case': 'legacy_archive_exact_equivalence', 'prefix_offset_color_cases': cases, 'passed': True})

board, rows = fixture([0, 50, 400, 10, 30])
data = current.metadata(board, rows, chess.WHITE, current.prior('audit-eval'))
prefix = board.copy(stack=True)
for _ in range(4):
    prefix.pop()
expected = current.snapshot(prefix, rows, chess.WHITE, current.prior('audit-eval'))
assert current.archive_snapshot(prefix, data) == expected
future = copy.deepcopy(data)
for row in future['curve']:
    if row['available_ply'] > len(prefix.move_stack):
        row.update(loss_cp=9999, estimate=3999, information_weight=999)
assert current.archive_snapshot(prefix, future) == expected
checks.append({'case': 'v2_prefix_excludes_future_loss_and_derived_scores', 'passed': True})

for payload in (None, {}, {'method': current.EVIDENCE_METHOD, 'valid': False}):
    bad = copy.deepcopy(rows)
    bad[0]['opponent_observation']['rating_evidence'] = payload
    assert len(current.observations(board, bad, chess.WHITE)) == len(rows) - 1
bad = copy.deepcopy(rows)
bad[0]['opponent_observation'].pop('valid')
assert len(current.observations(board, bad, chess.WHITE)) == len(rows) - 1
checks.append({'case': 'invalid_nested_evidence_and_missing_legacy_valid_rejected', 'passed': True})

board2, rows2 = fixture([800, 0])
row = rows2[-1]['opponent_observation']
played = row['played_uci']
row['rating_evidence'] = {'method': current.EVIDENCE_METHOD, 'valid': True, 'kind': 'already_lost', 'depth': 6,
    'best_cp': None, 'played_cp': None, 'best_mate': -1, 'played_mate': -1,
    'best_uci': played, 'played_uci': played, 'reliability': 'stable'}
short = board2.copy(stack=True)
short.pop(); short.pop()
assert current.snapshot(board2, rows2, chess.WHITE, current.prior()) == current.snapshot(short, rows2, chess.WHITE, current.prior())
row['rating_evidence'].update(kind='mate_preserved', best_mate=1, played_mate=1)
assert current.snapshot(board2, rows2, chess.WHITE, current.prior()) == current.snapshot(short, rows2, chess.WHITE, current.prior())
checks.append({'case': 'zero_information_mate_events_do_not_decay_errors_or_advance_rating_stage', 'passed': True})

with tempfile.TemporaryDirectory(prefix='regicide-independent-rating-store-') as folder:
    oldstore, store = legacy.RatingStore(folder), current.RatingStore(folder)
    for gid, target, losses in [('synthetic-a', 1350, [100, 40, 300, 50] * 3),
                                ('synthetic-b', 1475, [0, 15, 20, 50, 80, 200, 10, 40])]:
        b, rs = fixture(losses)
        sample = {'completed': True, 'handicap': False, 'fingerprint': gid,
                  'features': legacy.metadata(b, rs, chess.WHITE, legacy.prior('audit-eval'))}
        oldstore.upsert(gid, target, sample)
    oldbytes = store.path.read_bytes()
    model = store.calibration('audit-eval')
    assert store.path.read_bytes() == oldbytes
    assert model['method'] == current.METHOD and model['calibration_games'] == 2
    assert len(model['offset_by_observation']) == 40
    assert model['offset_by_observation'][0] != model['offset_by_observation'][-1]
    assert store.calibration('other-eval')['calibration_games'] == 0
    with closing(sqlite3.connect(store.path)) as c, c:
        saved = c.execute('SELECT game_id,sample_json FROM labels').fetchall()
        for gid, encoded in saved:
            sample = json.loads(encoded)
            for row in sample['features']['curve']:
                row.update(prior_estimate=3999, estimate=100)
            c.execute('UPDATE labels SET sample_json=? WHERE game_id=?', (json.dumps(sample), gid))
    mutated = store.path.read_bytes()
    assert store.calibration('audit-eval') == model
    assert store.path.read_bytes() == mutated
    checks.append({'case': 'legacy_labels_recomputed_without_db_writes_or_saved_estimate_trust', 'synthetic_games': 2, 'passed': True})
    original_json = saved[0][1]
    for malformed in ('null', '[]', '{}', '{"features":null}', '{"features":{"curve":42}}'):
        with closing(sqlite3.connect(store.path)) as c, c:
            c.execute('UPDATE labels SET sample_json=? WHERE game_id=?', (malformed, 'synthetic-a'))
        assert store.calibration('audit-eval')['calibration_games'] == 1
    with closing(sqlite3.connect(store.path)) as c, c:
        c.execute('UPDATE labels SET sample_json=? WHERE game_id=?', (original_json, 'synthetic-a'))
    store.invalidate('synthetic-a')
    assert store.calibration('audit-eval')['calibration_games'] == 1
checks.append({'case': 'bad_label_shapes_other_evaluator_and_revoked_games_do_not_calibrate', 'passed': True})

p = subprocess.run([sys.executable, 'tests/rating_observation_host_tests.py'], cwd=root,
                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
(out / 'rating-observation-host-final.log').write_bytes(p.stdout)
assert p.returncode == 0, p.stdout.decode(errors='replace')
count = int(re.search(rb'Ran (\d+) tests?', p.stdout).group(1))
checks.append({'case': 'real_host_fake_uci_bounded_rating_observation', 'tests': count, 'passed': True})
after = hashes()
assert before == after, 'Source changed during audit'
report = {'created_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'all_passed': True, 'checks': checks,
          'source_sha256': before, 'source_sha256_after': after, 'sources_unchanged': True,
          'legacy_reference_commit': '32d9e644bd0c971c332ecbded47debe0404784d4',
          'legacy_reference_sha256': hashlib.sha256(source).hexdigest(),
          'scope': 'Independent controlled fixtures and temporary databases only; no personal records, native engines, human calibration accuracy or playing-strength claim'}
(out / 'final-independent-audit.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
print(json.dumps({'all_passed': True, 'legacy_cases': cases, 'checks': len(checks), 'host_tests': count}))
