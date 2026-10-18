"""Read-only audit of the finished endgame study; no native searches."""
from collections import Counter, defaultdict
import datetime
import hashlib
import json
from pathlib import Path
import sys

import chess

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
import collect_learning_data as DATA
import collect_endgame_data as ENDGAME
from learning_corpus import _source

RUN = ROOT / 'build/learning-cycle/runs/000005-1cf7521bd5dc'
OUT = Path(__file__).resolve().parent / 'data-audit.json'
BASE = ROOT / 'docs/checks/2026-10-19-neural-cost-quality/run/quality/manifest.json'
checks = Counter()
frozen = {}

def require(condition, message):
    checks['assertions'] += 1
    if not condition:
        raise AssertionError(message)

def freeze(path):
    path = Path(path).resolve()
    frozen[str(path)] = DATA.sha256_file(path)

def source(directory):
    result = _source(Path(directory) / 'manifest.json', allow_empty_splits=True)
    require(result['manifest']['status'] == 'complete', 'incomplete source: ' + str(directory))
    freeze(result['path'])
    for name in result['files_sha256']:
        freeze(result['path'].parent / name)
    return result

def keys(board):
    return DATA.canonical_position_key(board), DATA.neural_feature_key(board)

def full_keys(games, splits=None):
    canonical, neural = set(), set()
    for game in games:
        if splits is not None and game['split'] not in splits:
            continue
        board = chess.Board(game['initial_fen'])
        require(board.is_valid(), 'invalid initial FEN')
        for ply in range(len(game['history']) + 1):
            a, b = keys(board)
            canonical.add(a)
            neural.add(b)
            if ply < len(game['history']):
                require(board.outcome(claim_draw=True) is None, 'game continues after rule ending')
                move = chess.Move.from_uci(game['history'][ply])
                require(move in board.legal_moves, 'illegal actual move')
                board.push(move)
                checks['actual_game_plies'] += 1
        outcome = board.outcome(claim_draw=True)
        require(not game['completed'] or outcome is not None, 'completed game not terminal')
        if game['completed']:
            require(outcome.result() == game['result'], 'actual outcome mismatch')
    return canonical, neural

def sample_keys(positions):
    canonical, neural = set(), set()
    for row in positions:
        board = DATA.replay_position(row['initial_fen'], row['history'])
        require(board.fen(en_passant='fen') == row['fen'], 'sample history/FEN mismatch')
        a, b = keys(board)
        canonical.add(a)
        neural.add(b)
    return canonical, neural

def collision_count(positions, inventory):
    canonical, neural = inventory
    counts = Counter()
    for row in positions:
        board = DATA.replay_position(row['initial_fen'], row['history'])
        a, b = keys(board)
        counts['canonical'] += a in canonical
        counts['neural'] += b in neural
    return dict(counts)

def describe(dataset):
    games, rows = dataset['games'], dataset['positions']
    return {'games': len(games), 'completed': sum(game['completed'] for game in games),
            'outcomes': dict(Counter(game['result'] for game in games)),
            'game_families': dict(sorted(Counter(game['family'] for game in games).items())),
            'sample_families': dict(sorted(Counter(row['family'] for row in rows).items())),
            'positions': len(rows), 'splits': dict(Counter(row['split'] for row in rows)),
            'actual_turns': dict(Counter(row['turn'] for row in rows)),
            'sampled_games': len({row['game_id'] for row in rows}),
            'unique_canonical': len(sample_keys(rows)[0]),
            'unique_neural': len(sample_keys(rows)[1])}

def audit_raw_rows(dataset):
    for row in dataset['positions']:
        tokens = row['teacher_score_raw_info'].split()
        index = tokens.index('score')
        require(tokens[index + 1] == 'cp' and int(tokens[index + 2]) == row['teacher_cp_stm'],
                'original raw finite cp label differs')
        require(index + 3 >= len(tokens) or tokens[index + 3] not in ('upperbound', 'lowerbound'),
                'original raw label is bounded')
        require(int(tokens[tokens.index('depth') + 1]) == row['teacher_depth'], 'original raw depth differs')
        require(int(tokens[tokens.index('nodes') + 1]) == row['teacher_nodes'], 'original raw nodes differ')
        require(tokens[tokens.index('pv') + 1:] == row['teacher_pv'], 'original raw PV differs')
        board = DATA.replay_position(row['initial_fen'], row['history'])
        require(('w' if board.turn else 'b') == row['turn'], 'original raw actual side differs')
        for text in row['teacher_pv']:
            require(board.outcome(claim_draw=False) is None, 'original raw PV continues after terminal')
            move = chess.Move.from_uci(text)
            require(move in board.legal_moves, 'original raw PV is illegal')
            board.push(move)
            checks['original_teacher_pv_plies'] += 1

def raw_stage(stage, board):
    require(stage['best_move'] in {m.uci() for m in board.legal_moves}, 'illegal teacher bestmove')
    pv = stage['pv']
    probe = board.copy(stack=True)
    for text in pv:
        require(probe.outcome(claim_draw=False) is None, 'postterminal teacher PV')
        move = chess.Move.from_uci(text)
        require(move in probe.legal_moves, 'illegal teacher PV')
        probe.push(move)
        checks['teacher_pv_plies'] += 1
    event = stage['raw_score_event']
    if event is None:
        return False
    tokens = event['raw_info'].split()
    require(tokens[0] == 'info' and 'score' in tokens, 'raw score event missing')
    index = tokens.index('score')
    kind, value = tokens[index + 1], int(tokens[index + 2])
    require(kind == event['kind'] and value == event['value'], 'raw score event differs')
    require(stage['cp_stm'] == value if kind == 'cp' else stage['mate_stm'] == value,
            'raw teacher score perspective differs')
    bounded = index + 3 < len(tokens) and tokens[index + 3] in ('upperbound', 'lowerbound')
    exact = (kind == 'cp' and not bounded and abs(value) < 29904 and
             type(stage['completed_depth']) is int and stage['completed_depth'] >= 1 and
             type(stage['reported_nodes']) is int and stage['reported_nodes'] >= stage['requested_nodes'] and
             bool(pv) and pv[0] == stage['best_move'])
    if exact:
        require(int(tokens[tokens.index('depth') + 1]) == stage['completed_depth'], 'raw completed depth differs')
        require(int(tokens[tokens.index('nodes') + 1]) == stage['reported_nodes'], 'raw node count differs')
        require(tokens[tokens.index('pv') + 1:] == pv, 'raw PV differs')
    return exact and stage['exclusion'] is None and stage['label'] is not None

def audit_quality(raw, refined, filename):
    path = refined['path'].parent / filename
    report = json.loads(path.read_text(encoding='utf-8'))
    freeze(path)
    require(report['status'] == 'complete' and report['errors'] == [], 'quality report failed')
    require(report['source_manifest_sha256'] == raw['sha256'], 'quality raw source identity differs')
    require(report['policy']['node_budgets'] == [10000, 50000, 200000], 'teacher budgets changed')
    require(report['policy']['max_last_two_cp_delta'] == 30, 'teacher stability threshold changed')
    require(report['policy']['minimum_completed_depth'] == 3, 'teacher depth criterion changed')
    require((refined['path'].parent / 'games.jsonl').read_bytes() ==
            (raw['path'].parent / 'games.jsonl').read_bytes(), 'quality rewrote actual games')
    require((refined['path'].parent / 'games.pgn').read_bytes() ==
            (raw['path'].parent / 'games.pgn').read_bytes(), 'quality rewrote actual PGNs')
    accepted = {}
    for record in report['records']:
        row = raw['positions'][record['source_row_index']]
        require(row['history'] == record['history'] and row['fen'] == record['fen'], 'quality root identity differs')
        board = DATA.replay_position(row['initial_fen'], row['history'])
        stages = record['stages']
        require([stage['requested_nodes'] for stage in stages] == [10000, 50000, 200000], 'stage budgets/order differ')
        eligible = all([raw_stage(stage, board) for stage in stages])
        previous, final = stages[-2:]
        stable = (eligible and min(previous['completed_depth'], final['completed_depth']) >= 3 and
                  previous['best_move'] == final['best_move'] and
                  abs(previous['cp_stm'] - final['cp_stm']) <= 30 and
                  final['requested_nodes'] > row['teacher_nodes'])
        require(record['accepted'] == stable, 'quality acceptance differs from independent policy')
        if stable:
            accepted[(row['game_id'], row['ply'])] = final['label']
    require(len(report['records']) == len(raw['positions']), 'quality did not consider every original row')
    require(report['queries_completed'] == 3 * len(report['records']), 'quality query count differs')
    require(len(accepted) == len(refined['positions']), 'quality accepted row count differs')
    for row in refined['positions']:
        label = accepted[(row['game_id'], row['ply'])]
        for field, expected in label.items():
            require(row[field] == expected, 'accepted final teacher label differs')
    return {'original': len(raw['positions']), 'accepted': len(accepted),
            'rejected': len(raw['positions']) - len(accepted), 'queries': report['queries_completed'],
            'exclusions': report['exclusions'], 'independent_selection_replay': True}

def main():
    state = ROOT / 'build/learning-cycle'
    raw_holdout, holdout = source(RUN / 'endgame-heldout'), source(RUN / 'endgame-heldout-quality')
    raw_train, train = source(RUN / 'endgame-train'), source(RUN / 'endgame-train-quality')
    augmentation_path = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else RUN / 'augmentation'
    augmentation = source(augmentation_path)
    baseline, bootstrap = source(BASE.parent), source(state / 'bootstrap')
    audit_raw_rows(raw_holdout)
    audit_raw_rows(raw_train)
    protected = {'bootstrap_complete_holdout_trajectories': full_keys(bootstrap['games'], {'validation', 'test'}),
                 'baseline_all_sample_inputs': sample_keys(baseline['positions']),
                 'new_full_holdout_trajectories': full_keys(raw_holdout['games'])}
    arena = ENDGAME.ExclusionInventory()
    arena.add_openings(ROOT / 'tests/data/learning_arena_openings_v1.json')
    for directory in sorted((state / 'runs').iterdir()):
        if directory.name >= RUN.name:
            continue
        path = directory / 'arena/arena.json'
        if path.is_file():
            arena.add_games(path)
    protected['reserved_pool_and_prior_arena_trajectories'] = (arena.canonical, arena.features)
    for item in arena.sources:
        freeze(item['path'])
    for dataset in (raw_train, train, augmentation):
        full_keys(dataset['games'])
    # Every class split remains constructed, and every recorded outcome remains
    # supported by an independently replayed actual game, including raw draws.
    require(len(raw_holdout['games']) == 16, 'heldout game count differs')
    require(len({g['family'] for g in raw_holdout['games']}) == 8, 'heldout family coverage differs')
    require(len(raw_train['games']) == 128, 'training game count differs')
    require(len({g['family'] for g in raw_train['games']}) == 16, 'training family coverage differs')
    require(all(g['completed'] for g in (*raw_holdout['games'], *raw_train['games'])), 'unfinished collection game')
    collisions = {}
    for name, inventory in protected.items():
        counts = collision_count(raw_train['positions'], inventory)
        require(not any(counts.values()), 'raw training input overlaps ' + name)
        refined_counts = collision_count(train['positions'], inventory)
        require(not any(refined_counts.values()), 'quality training input overlaps ' + name)
        collisions[name] = {'raw_train': counts, 'quality_train': refined_counts,
                            'inventory_canonical': len(inventory[0]), 'inventory_neural': len(inventory[1])}
    heldout_guard = {name: inventory for name, inventory in protected.items()
                     if name not in ('new_full_holdout_trajectories',)}
    for name, inventory in heldout_guard.items():
        counts = collision_count(holdout['positions'], inventory)
        require(not any(counts.values()), 'independent holdout input overlaps ' + name)
        collisions[name]['quality_holdout'] = counts
    quality = {'heldout': audit_quality(raw_holdout, holdout, 'holdout-quality-report.json'),
               'train': audit_quality(raw_train, train, 'quality-report.json')}
    for split in ('validation', 'test'):
        expected = [r for r in holdout['positions'] if r['split'] == split]
        actual = [r for r in augmentation['positions'] if r['split'] == split]
        require(expected == actual and bool(actual), 'augmentation lost new heldout identity')
    arena.verify_sources()
    for path, digest in frozen.items():
        require(DATA.sha256_file(path) == digest, 'audited source changed: ' + path)
    report = {'status': 'passed', 'finished_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'scope': 'Read-only independent game/PV/rule/teacher-policy/input-isolation replay; no native searches or fitting.',
              'augmentation_path': str(augmentation_path.relative_to(ROOT)),
              'datasets': {name: describe(value) for name, value in (
                  ('raw_heldout', raw_holdout), ('quality_heldout', holdout), ('raw_train', raw_train),
                  ('quality_train', train), ('augmentation', augmentation))},
              'collisions': collisions, 'teacher_quality': quality, 'checks': dict(checks),
              'input_files': len(frozen), 'input_sha256': {str(Path(path).relative_to(ROOT)): digest for path, digest in frozen.items()}}
    OUT.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'status': 'passed', 'checks': dict(checks), 'teacher_quality': quality}))

if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        OUT.write_text(json.dumps({'status': 'failed', 'error_type': type(error).__name__,
                                  'error': str(error), 'checks': dict(checks)}, indent=2) + '\n',
                       encoding='utf-8')
        raise
