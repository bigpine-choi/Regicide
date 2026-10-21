"""Broaden sibling MSE supervision while sealing a separate unseen-input gate.

Exactly two legal roots per original source family, with a bounded teacher
budget and no refill. Gate roots and ALL their legal children are forbidden
in gradients and checkpoint selection. No symmetry labels or new games.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import time

import chess
import chess.engine
import numpy as np

import neural_stm as stm
import neural_training as base
from prepare_neural_huber import read, require, sha, write
from stockfish_teacher import label_rows, source_board, _json_info
from train_neural_stm import ROOT

OLD = ROOT / 'docs/checks/2026-10-22-nnue-ordering'
SOURCE = ROOT / 'docs/checks/2026-10-20-neural-target-ab/attempt-002/trainval'
PROFILE = ROOT / 'docs/checks/2026-10-21-m2-foundation/opponents/stockfish18-profile.json'
SEED = 2026102203
VALUES = {1: 100, 2: 320, 3: 330, 4: 500, 5: 900, 6: 0}


def lines(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]


def rank(value):
    return hashlib.sha256((str(SEED) + ':' + value).encode()).hexdigest()


def jsonl(path, rows):
    with path.open('x', encoding='utf-8', newline='\n') as f:
        for row in rows:
            f.write(json.dumps(row, sort_keys=True, allow_nan=False) + '\n')


def select_roots(gate_plan, blocked):
    exposure_paths = [Path(p) for p in gate_plan['input_sha256']
                      if Path(p).name in ('train.jsonl', 'validation.jsonl', 'tuning.jsonl')]
    exposed = {base.feature_identity(chess.Board(row['fen'])) for p in exposure_paths for row in lines(p)}
    games = {game['game_id']: game for game in lines(SOURCE / 'games.jsonl')}
    buckets = defaultdict(list)
    for row in lines(SOURCE / 'queries.jsonl'):
        if row['kind'] != 'selfplay_start' or len(row['history']) < 16:
            continue
        board = chess.Board(row['fen'])
        material = sum(VALUES[p.piece_type] * (1 if p.color else -1) for p in board.piece_map().values())
        if (len(board.piece_map()) < 20 or abs(material) > 200
                or base.feature_identity(board) in exposed | blocked
                or not board.is_valid() or board.outcome(claim_draw=True) is not None):
            continue
        buckets[row['family_id']].append((row, len(board.piece_map()), board.turn))
    chosen, used = [], set()
    for family in sorted(buckets):
        candidates = sorted(buckets[family], key=lambda item: rank(item[0]['query_id']))
        picked = []
        for which in range(2):
            # First an early root, then another phase/opposite mover when available.
            order = sorted(candidates, key=lambda item: (
                (item[1] < 28) if which == 0 else (item[1] >= 28),
                item[2] == picked[0][2] if picked else False,
                rank(item[0]['query_id'])))
            for row, count, turn in order:
                identity = base.feature_identity(chess.Board(row['fen']))
                if identity in used or (picked and abs(len(row['history']) - len(picked[0][0]['history'])) < 8):
                    continue
                board = source_board(row)
                child_features = set()
                for move in board.legal_moves:
                    child = board.copy(stack=False)
                    child.push(move)
                    child_features.add(base.feature_identity(child))
                if child_features & blocked:
                    continue
                game = games[row['game_id']]
                require(row['initial_fen'] == game['initial_fen'] and row['history'] == game['moves_uci'][:len(row['history'])]
                        and row['family_id'] == game['family_id'] and row['partition'] == game['partition'],
                        'Original source game/family/history differs')
                selected = {'query_id': 'family-' + row['query_id'], 'source_query_id': row['query_id'],
                            'source_query_file': str(SOURCE / 'queries.jsonl'), 'source_game_id': row['game_id'],
                            'source_game_file': str(SOURCE / 'games.jsonl'), 'source_partition': row['partition'],
                            'partition': 'tuning' if row['partition'] == 'validation' else 'train',
                            'family_id': family, 'kind': 'family_comparison', 'initial_fen': row['initial_fen'],
                            'history': row['history'], 'fen': board.fen(en_passant='fen'), 'feature_hash': identity,
                            'piece_count': count, 'mover': 'white' if turn else 'black',
                            'source_recorded_move': row['bestmove_uci'], 'target_white_cp': 0, 'replay_weight': 1}
                picked.append((row, count, turn))
                chosen.append(selected)
                used.add(identity)
                break
            else:
                raise ValueError('Source family lacks two separated eligible roots: ' + family)
    require(len(buckets) == 32 and len(chosen) == 64, 'Expected32families and64fixed roots')
    require(Counter(r['partition'] for r in chosen) == {'train': 48, 'tuning': 16}, 'Original source split changed')
    return chosen, exposure_paths, len(exposed)


def prepare(output, gate_path):
    started = time.monotonic()
    require(not output.exists(), 'Refusing to overwrite or retry data generation')
    gate = read(gate_path)
    blocked_path = gate_path.parent / 'blocked-features.json'
    require(sha(blocked_path) == gate['blocked_features_sha256'], 'Gate exclusion changed')
    blocked = set(read(blocked_path))
    selected, exposure_paths, exposure_count = select_roots(gate, blocked)
    profile = read(PROFILE)
    sf = Path(profile['binary']['path'])
    binary, model_path = ROOT / 'build-m3-stm/regicide.exe', OLD / 'attempt-001/model.nnue'
    require(sha(sf) == profile['binary']['sha256'] and sha(model_path) == gate['warm_sha256'], 'Frozen engine/model differs')
    background = {p: lines(OLD / 'data-001' / (p + '.jsonl')) for p in ('train', 'tuning', 'test')}
    require(not {r['feature_hash'] for rows in background.values() for r in rows} & blocked, 'Background contains gate input')
    inputs = [Path(__file__), ROOT / 'scripts/stockfish_teacher.py', ROOT / 'scripts/neural_stm.py',
              ROOT / 'scripts/neural_training.py', gate_path, blocked_path, gate_path.parent / 'frozen-roots.json',
              SOURCE / 'queries.jsonl', SOURCE / 'games.jsonl', PROFILE, sf, binary, model_path,
              *exposure_paths, *[OLD / 'data-001' / (p + '.jsonl') for p in background],
              OLD / 'data-001/pairs-train.jsonl', OLD / 'data-001/pairs-tuning.jsonl']
    output.mkdir(parents=True)
    protocol = {'created_utc': datetime.now(timezone.utc).isoformat(), 'seed': SEED,
                'roots': 64, 'root_families': 32, 'roots_per_family': 2,
                'requested_split': {'train': 48, 'tuning': 16}, 'selection': 'Unseen exact inputs; materialwithin200cp,pieces>=20,history>=16; early/middle and mover coverage; roots at least8plies apart',
                'maximum_teacher_queries': 192, 'nodes_per_teacher_query': 50000,
                'warm_searches_maximum': 64, 'nodes_per_warm_search': 10000,
                'old_train_pairs_maximum_per_family': 2, 'new_pair_per_root': 1, 'no_refill': True,
                'no_symmetry_labels': True, 'exposure_unique_features': exposure_count,
                'gate_plan': str(gate_path), 'gate_excluded_from_training_and_selection': True,
                'input_sha256': {str(p): sha(p) for p in inputs}}
    write(output / 'protocol.json', protocol)
    write(output / 'selected-roots.json', selected)
    for p in inputs:
        if p.suffix == '.py':
            dest = output / 'source-snapshot' / p.relative_to(ROOT)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(p, dest)
    print('Label64fixed roots from32families; gate16roots+alllegalchildren excluded.', flush=True)
    roots, root_report = label_rows(sf, selected, output / 'roots-teacher', nodes=50000, max_abs_cp=1500)
    queries = {q['source_query_id']: q for q in lines(output / 'roots-teacher/teacher-queries.jsonl')}
    model = stm.load_model(model_path)
    options = {**read(ROOT / 'config/engine-profiles/classical.json')['options'],
               'UseNNUEEvaluation': True, 'NeuralModelFile': str(model_path)}
    children, branches, warm_receipts = [], [], []
    with chess.engine.SimpleEngine.popen_uci([str(binary), 'uci'], timeout=30) as engine:
        engine.configure(options)
        for root in roots:
            board = source_board(root)
            best = queries[root['query_id']]['bestmove_uci']
            engine.configure({'Clear Hash': None})
            info = engine.analyse(board, chess.engine.Limit(nodes=10000), game=object(), info=chess.engine.INFO_ALL)
            warm = info['pv'][0].uci()
            warm_receipts.append({'root_id': root['query_id'], 'raw_info': _json_info(info), 'move': warm})
            alternative = warm
            if alternative == best:
                choices = [m for m in board.legal_moves if m.uci() != best]
                if not choices:
                    continue
                trial = []
                for move in choices:
                    child = board.copy(stack=False)
                    child.push(move)
                    trial.append({'fen': child.fen(), 'target_white_cp': 0})
                ix, _, turns = stm.pack_records(trial)
                scores = stm.predict(model, ix, turns) * (1 if board.turn else -1)
                alternative = choices[int(np.argmax(scores))].uci()
            sides = []
            for role, move in (('a', best), ('b', alternative)):
                child = board.copy(stack=True)
                child.push_uci(move)
                clean = {k: v for k, v in root.items() if not k.startswith('teacher_') and k != 'source_target_white_cp'}
                row = {**clean, 'query_id': root['query_id'] + '-' + role, 'source_root_id': root['query_id'],
                       'root_move': move, 'fen': child.fen(en_passant='fen'), 'history': root['history'] + [move],
                       'feature_hash': base.feature_identity(child), 'target_white_cp': 0, 'source_role': role}
                require(row['feature_hash'] not in blocked, 'A new child overlaps sealed gate')
                sides.append(row)
                children.append(row)
            branches.append((root, sides))
    write(output / 'warm-searches.json', warm_receipts)
    write(output / 'selected-children.json', children)
    require(len(selected) + len(children) <= 192, 'Teacher query budget exceeded')
    labels, child_report = label_rows(sf, children, output / 'children-teacher', nodes=50000, max_abs_cp=1500)
    by_id = {r['query_id']: r for r in labels}
    return finalize_cached(output, gate_path)


def finalize_cached(output, gate_path, *, recovered=False):
    """Assemble only completed exact receipts; never launch another process."""
    started = time.monotonic()
    require(not (output / 'manifest.json').exists(), 'Refusing to overwrite completed assembly')
    protocol = read(output / 'protocol.json')
    selected = read(output / 'selected-roots.json')
    roots = lines(output / 'roots-teacher/labels.jsonl')
    labels = lines(output / 'children-teacher/labels.jsonl')
    children = read(output / 'selected-children.json')
    warm_receipts = read(output / 'warm-searches.json')
    root_report = read(output / 'roots-teacher/teacher-report.json')
    child_report = read(output / 'children-teacher/teacher-report.json')
    require(len(selected) == 64 and len(selected) + len(children) <= 192, 'Original query budget differs')
    by_id = {r['query_id']: r for r in labels}
    groups = defaultdict(list)
    for child in children:
        groups[child['source_root_id']].append(child)
    branches = [(root, groups[root['query_id']]) for root in roots if len(groups[root['query_id']]) == 2]
    blocked = set(read(gate_path.parent / 'blocked-features.json'))
    background = {p: lines(OLD / 'data-001' / (p + '.jsonl')) for p in ('train', 'tuning', 'test')}
    write(output / 'assembly-receipt.json', {
        'created_utc': datetime.now(timezone.utc).isoformat(), 'recovered_completed_queries': recovered,
        'source_sha256': sha(Path(__file__)), 'teacher_or_native_requeries': 0,
        'reason': ('Original assembly required16newtrainpairs but14exactpairs exist. Use all exact pairs without changing labels, gate, loss or budget; check actual batch feasibility instead of an arbitrary minimum.' if recovered else 'Finalize completed exact query receipts without repeating teacher or engine searches.'),
        'policy': 'Both partitions nonempty and total train pairs cover every point batch',
        'teacher_files': {str(output / batch / name): sha(output / batch / name)
                          for batch in ('roots-teacher', 'children-teacher')
                          for name in ('labels.jsonl', 'teacher-queries.jsonl', 'teacher-report.json')}})
    family_pairs = defaultdict(list)
    for pair in lines(OLD / 'data-001/pairs-train.jsonl'):
        family_pairs[pair['family_id']].append(pair)
    pairs = {'train': [], 'tuning': lines(OLD / 'data-001/pairs-tuning.jsonl')}
    for family, rows in sorted(family_pairs.items()):
        pairs['train'].extend(sorted(rows, key=lambda p: rank(p['root_id'] + p['move_a'] + p['move_b']))[:2])
    feature_partition = {r['feature_hash']: p for p, rows in background.items() for r in rows}
    all_new = roots + labels
    feature_splits = defaultdict(set)
    for r in all_new:
        feature_splits[r['feature_hash']].add(r['partition'])
    cross = {h for h, ps in feature_splits.items() if len(ps) > 1}
    new_pairs, rejected = [], Counter()
    for root, sides in branches:
        if not all(row['query_id'] in by_id for row in sides):
            rejected['missing_exact_child'] += 1
            continue
        a, b = [by_id[row['query_id']] for row in sides]
        if any(r['feature_hash'] in cross or feature_partition.get(r['feature_hash'], r['partition']) != r['partition'] for r in (root, a, b)):
            rejected['cross_split_input'] += 1
            continue
        pair = {'root_id': root['query_id'], 'source_game': root['source_game_id'], 'source_ply': len(root['history']) + 1,
                'family_id': root['family_id'], 'partition': root['partition'],
                'root_sign': 1 if source_board(root).turn else -1, 'root_fen': root['fen'],
                'initial_fen': root['initial_fen'], 'root_history': root['history'],
                'move_a': a['root_move'], 'move_b': b['root_move'], 'a': a, 'b': b,
                'target_gap_white_cp': a['target_white_cp'] - b['target_white_cp']}
        pairs[root['partition']].append(pair)
        new_pairs.append(pair)
    counts = Counter(p['partition'] for p in new_pairs)
    require(counts['train'] > 0 and counts['tuning'] > 0, 'Need new exact pairs in both source partitions')
    for row in all_new:
        h, part = row['feature_hash'], row['partition']
        if h in feature_partition or h in cross:
            continue
        require(h not in blocked, 'New point overlaps sealed gate')
        background[part].append({**row, 'teacher_batch': 'generalization-root-or-child', 'replay_weight': 1})
        feature_partition[h] = part
    point_batches = (sum(r.get('replay_weight', 1) for r in background['train']) + 127) // 128
    require(len(pairs['train']) >= point_batches, 'Insufficient pairs for one pass across point batches')
    for partition, rows in background.items():
        jsonl(output / (partition + '.jsonl'), rows)
    for partition, rows in pairs.items():
        jsonl(output / ('pairs-' + partition + '.jsonl'), rows)
    manifest = {'schema_version': 1, 'files': {p + '.jsonl': sha(output / (p + '.jsonl')) for p in background},
                'rows': {p: len(rows) for p, rows in background.items()}, 'pair_rows': {p: len(rows) for p, rows in pairs.items()},
                'pair_files': {'pairs-' + p + '.jsonl': sha(output / ('pairs-' + p + '.jsonl')) for p in pairs},
                'new_pair_rows': dict(counts), 'pair_families': {p: dict(Counter(row['family_id'] for row in rows)) for p, rows in pairs.items()},
                'old_train_pair_rows_retained': len(pairs['train']) - counts['train'],
                'anchor': str(ROOT / 'docs/checks/2026-10-22-nnue-diversity/data-001/tuning.jsonl'),
                'source_protocol': {'path': str(output / 'protocol.json'), 'sha256': sha(output / 'protocol.json')},
                'gate_plan': str(gate_path), 'gate_overlap': 0, 'new_pairs_rejected': dict(rejected),
                'teacher_queries': len(selected) + len(children), 'teacher_seconds': root_report['elapsed_seconds'] + child_report['elapsed_seconds'],
                'warm_searches': len(warm_receipts), 'assembly_seconds': time.monotonic() - started,
                'test_limit': 'Existing69exposed development examples; freshgate16separately excluded from all gradients and checkpoint selection'}
    for name, digest in protocol['input_sha256'].items():
        path = Path(name)
        actual = output / 'source-snapshot' / path.relative_to(ROOT) if path.suffix == '.py' else path
        require(sha(actual) == digest, 'Original generation input/snapshot changed: ' + name)
    manifest['assembly_receipt'] = {'path': str(output / 'assembly-receipt.json'), 'sha256': sha(output / 'assembly-receipt.json')}
    write(output / 'manifest.json', manifest)
    print(json.dumps(manifest, indent=2), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('--gate-plan', type=Path, required=True)
    parser.add_argument('--finalize-cached', action='store_true')
    args = parser.parse_args()
    if args.finalize_cached:
        finalize_cached(args.output.resolve(), args.gate_plan.resolve(), recovered=True)
    else:
        prepare(args.output.resolve(), args.gate_plan.resolve())
