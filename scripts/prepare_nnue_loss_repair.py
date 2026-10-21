"""Label a bounded set of actual loss positions and their sibling/leaf positions.

The background bank is immutable. New game pairs 1..6 train, 7..8 tune;
canonical mirrored piece inputs may never cross any partition. No match or
teacher result determines that split or causes an automatic refill.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import argparse
import json
from pathlib import Path
import shutil
import sys
import time

import chess

import neural_stm as stm
import neural_training as base
from prepare_neural_huber import read, require, sha, write
from stockfish_teacher import label_rows
from train_neural_stm import ROOT

PREVIOUS = ROOT / 'docs/checks/2026-10-22-nnue-diversity'
SCREEN = ROOT / 'docs/checks/2026-10-22-nnue-loss-repair/diagnosis/loss-screen.json'
PROFILE = ROOT / 'docs/checks/2026-10-21-m2-foundation/opponents/stockfish18-profile.json'


def lines(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]


def make_row(game, case, history, role):
    board = chess.Board(game['initial_fen'])
    for uci in history:
        move = chess.Move.from_uci(uci)
        require(move in board.legal_moves, 'Illegal loss source history')
        board.push(move)
    pair = game['pair']
    return {'query_id': f"loss-{game['game']:02d}-{case['ply']:03d}-{role}",
            'fen': board.fen(en_passant='fen'), 'initial_fen': game['initial_fen'],
            'history': history, 'feature_hash': base.feature_identity(board),
            'family_id': f'loss-pair-{pair:02d}', 'partition': 'train' if pair <= 6 else 'tuning',
            'kind': 'loss_replay', 'source_game': game['game'], 'source_ply': case['ply'],
            'source_pair': pair, 'source_role': role, 'played_move': case['move'],
            'candidate_sign': case['candidate_sign'], 'source_search_cp': case['recorded_search_candidate_cp'],
            'target_white_cp': 0, 'replay_weight': 4}


def prepare(output):
    started = time.monotonic()
    require(not output.exists(), 'Refusing to overwrite loss repair output')
    screen, match = read(SCREEN), read(PREVIOUS / 'attempt-001/games.json')
    require(screen['source_games_sha256'] == sha(PREVIOUS / 'attempt-001/games.json'), 'Screen source differs')
    model_path = PREVIOUS / 'attempt-001/model.nnue'
    require(screen['source_model_sha256'] == sha(model_path), 'Screen model differs')
    background = {p: lines(PREVIOUS / 'data-001' / (p + '.jsonl')) for p in ('train', 'tuning', 'test')}
    require([len(background[p]) for p in background] == [1294, 263, 69], 'Background count differs')
    require(sum(len(loss['candidates']) for loss in screen['losses']) == 30, 'Expected thirty frozen cases')
    profile = read(PROFILE)
    engine = Path(profile['binary']['path'])
    require(sha(engine) == profile['binary']['sha256'], 'Stockfish identity differs')
    output.mkdir(parents=True)
    inputs = [Path(__file__), SCREEN, model_path, PREVIOUS / 'attempt-001/games.json',
              PREVIOUS / 'attempt-001/games.pgn', PREVIOUS / 'data-001/manifest.json',
              *[PREVIOUS / 'data-001' / (p + '.jsonl') for p in background], PROFILE, engine,
              ROOT / 'scripts/stockfish_teacher.py', ROOT / 'scripts/neural_stm.py', ROOT / 'scripts/neural_training.py']
    protocol = {'created_utc': datetime.now(timezone.utc).isoformat(), 'maximum_queries': 150,
                'root_cases': 30, 'nodes_per_query': 50000, 'max_abs_cp': 1500,
                'roles': ['root', 'played', 'teacher_best', 'pv_leaf', 'reply'],
                'split': 'Original loss opening pairs1..6 train, pairs7..8 tuning; test69 unchanged',
                'replay_weight': 4, 'loss': 'mse', 'no_refill': True,
                'background': str(PREVIOUS / 'data-001'), 'teacher_profile': profile,
                'input_sha256': {str(p): sha(p) for p in inputs},
                'limitations': ['Retrospective failure mining; teacher search is finite and may be wrong.',
                               'Original test69 was already inspected in development; no independent confirmation claim.',
                               'Final16games will use different declared starts; no direct old-score pooling.']}
    write(output / 'protocol.json', protocol)
    for p in inputs:
        if p.suffix == '.py':
            dest = output / 'source-snapshot' / p.relative_to(ROOT)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(p, dest)
    games = {g['game']: g for g in match['games']}
    cases, roots = [], []
    for loss in screen['losses']:
        game = games[loss['game']]
        require(game['candidate_points'] == 0 and game['completed'], 'Case must come from a completed loss')
        for case in sorted(loss['candidates'], key=lambda c: c['ply']):
            require(game['uci_moves'][case['ply'] - 1] == case['move'], 'Case move differs')
            history = game['uci_moves'][:case['ply'] - 1]
            row = make_row(game, case, history, 'root')
            require(row['fen'] == case['before']['fen'], 'Case FEN differs')
            cases.append((game, case, history))
            roots.append(row)
    write(output / 'root-selected.json', roots)
    print('Stockfish:30 frozen loss roots, then at most120 sibling/leaf positions.', flush=True)
    root_labels, root_report = label_rows(engine, roots, output / 'roots-teacher', nodes=50000, max_abs_cp=1500)
    root_queries = {q['source_query_id']: q for q in lines(output / 'roots-teacher/teacher-queries.jsonl')}
    children = []
    for row, (game, case, history) in zip(roots, cases, strict=True):
        query = root_queries[row['query_id']]
        alternatives = {'played': history + [case['move']], 'pv_leaf': history + case['pv'],
                        'reply': game['uci_moves'][:case['ply'] + 1]}
        if query.get('bestmove_uci'):
            alternatives['teacher_best'] = history + [query['bestmove_uci']]
        for role, new_history in alternatives.items():
            children.append(make_row(game, case, new_history, role))
    # Pre-label exact/mirror duplicate control. If a new feature appears in both
    # new split families, discard every copy rather than choose its partition.
    background_features = {r['feature_hash'] for rows in background.values() for r in rows}
    feature_partitions = {}
    for row in roots + children:
        feature_partitions.setdefault(row['feature_hash'], set()).add(row['partition'])
    cross = {h for h, ps in feature_partitions.items() if len(ps) > 1}
    seen = {row['feature_hash'] for row in roots}
    child_selected, selection_exclusions = [], Counter()
    for row in children:
        h = row['feature_hash']
        if h in background_features or h in cross or h in seen:
            selection_exclusions['background' if h in background_features else 'cross_split' if h in cross else 'duplicate'] += 1
            continue
        seen.add(h)
        child_selected.append(row)
    require(len(roots) + len(child_selected) <= 150, 'Query budget exceeded')
    write(output / 'child-selected.json', child_selected)
    child_labels, child_report = label_rows(engine, child_selected, output / 'children-teacher', nodes=50000, max_abs_cp=1500)
    accepted, accepted_features, merge_exclusions = [], set(), Counter()
    for batch, labels in (('roots', root_labels), ('children', child_labels)):
        for row in labels:
            h = row['feature_hash']
            if h in background_features or h in cross or h in accepted_features:
                merge_exclusions['background' if h in background_features else 'cross_split' if h in cross else 'duplicate'] += 1
                continue
            accepted_features.add(h)
            accepted.append({**row, 'teacher_batch': 'loss-' + batch})
    rows = {p: [{**r, 'replay_weight': 1} for r in source] + [r for r in accepted if r['partition'] == p]
            for p, source in background.items()}
    require(sum(r['kind'] == 'loss_replay' for r in rows['train']) >= 12, 'Insufficient train repair labels; no automatic retry')
    require(sum(r['kind'] == 'loss_replay' for r in rows['tuning']) >= 4, 'Insufficient tuning repair labels; no automatic retry')
    for p, records in rows.items():
        with (output / (p + '.jsonl')).open('x', encoding='utf-8', newline='\n') as f:
            for row in records:
                f.write(json.dumps(row, sort_keys=True, allow_nan=False) + '\n')
    model = stm.load_model(model_path)
    evidence = []
    all_queries = [*root_queries.values(), *lines(output / 'children-teacher/teacher-queries.jsonl')]
    for query in all_queries:
        board = chess.Board(query['fen'])
        cp = stm.quantized_white(board, model)
        score = query.get('score', {})
        evidence.append({'query_id': query['source_query_id'], 'fen': query['fen'], 'nnue_white_cp': cp,
                         'stockfish_white_cp': score.get('white_cp'), 'stockfish_white_mate': score.get('mate_white'),
                         'status': query['status'], 'reasons': query['reasons'],
                         'abs_error_cp': abs(cp - score['white_cp']) if score.get('white_cp') is not None else None})
    write(output / 'teacher-diagnosis.json', evidence)
    manifest = {'schema_version': 1, 'files': {p + '.jsonl': sha(output / (p + '.jsonl')) for p in rows},
                'rows': {p: len(r) for p, r in rows.items()}, 'new_repair_rows': dict(Counter(r['partition'] for r in accepted)),
                'effective_weighted_rows': {p: sum(r['replay_weight'] for r in rws) for p, rws in rows.items()},
                'selected_roots': len(roots), 'selected_children': len(child_selected),
                'actual_teacher_queries': len(roots) + len(child_selected), 'teacher_seconds': root_report['elapsed_seconds'] + child_report['elapsed_seconds'],
                'selection_exclusions': dict(selection_exclusions), 'merge_exclusions': dict(merge_exclusions),
                'generation_protocol': {'path': str(output / 'protocol.json'), 'sha256': sha(output / 'protocol.json')},
                'background': {'path': str(PREVIOUS / 'data-001/manifest.json'), 'sha256': sha(PREVIOUS / 'data-001/manifest.json')},
                'test_limit': protocol['limitations'][1], 'total_wall_seconds': time.monotonic() - started}
    require(all(sha(Path(p)) == h for p, h in protocol['input_sha256'].items()), 'Input changed during generation')
    write(output / 'manifest.json', manifest)
    print(json.dumps(manifest, indent=2), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    prepare(parser.parse_args().output.resolve())
