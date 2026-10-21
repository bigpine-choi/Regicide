"""Read-only diagnosis of the frozen small Stockfish fine-tune.

No matches, fitting, epoch selection, or additional training labels.  Optional
bounded teacher probes explain a few recorded moves, not general strength.
"""
from __future__ import annotations

import os
for _name in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS'):
    os.environ[_name] = '1'

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import time

import chess
import chess.engine
import numpy as np

import neural_stm as stm
import neural_training as base
from stockfish_teacher import assess_info, OPTIONS

ROOT = Path(__file__).resolve().parents[1]
FAILED = ROOT / 'docs/checks/2026-10-21-m3-stockfish-teacher/attempt-001'
WARM = ROOT / 'docs/checks/2026-10-21-m3-nnue-stm/attempt-001'
VALUES = {chess.PAWN: 100, chess.KNIGHT: 320, chess.BISHOP: 330,
          chess.ROOK: 500, chess.QUEEN: 900, chess.KING: 0}


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def lines(path):
    return [json.loads(line) for line in Path(path).read_text(encoding='utf-8').splitlines()]


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def summary(values):
    array = np.asarray(values, dtype=np.float64)
    return {'count': len(values), 'min': float(array.min()), 'median': float(np.median(array)),
            'mean': float(array.mean()), 'max': float(array.max())}


def material(board):
    return sum(VALUES[p.piece_type] * (1 if p.color else -1) for p in board.piece_map().values())


def distribution(rows):
    boards = [chess.Board(r['fen']) for r in rows]
    features = set()
    for board in boards:
        for view in base.feature_indices(board):
            features.update(int(index) for index in view)
    return {'count': len(rows), 'family_count': len({r.get('family_id') for r in rows}),
            'kinds': dict(Counter(r.get('kind') for r in rows)),
            'turns': dict(Counter('white' if b.turn else 'black' for b in boards)),
            'pieces': summary([len(b.piece_map()) for b in boards]),
            'fullmove': summary([b.fullmove_number for b in boards]),
            'opening_le_10_fullmove': sum(b.fullmove_number <= 10 for b in boards),
            'near_start_ge_28_pieces': sum(len(b.piece_map()) >= 28 for b in boards),
            'abs_material_cp': summary([abs(material(b)) for b in boards]),
            'target_cp': summary([r['target_white_cp'] for r in rows]),
            'unique_piece_features_of_768': len(features),
            'features': sorted(features)}


def predict(model, boards):
    rows = [{'fen': b.fen(en_passant='fen'), 'target_white_cp': 0} for b in boards]
    indices, _, turns = stm.pack_records(rows)
    return stm.predict(model, indices, turns)


def run(output, teacher_probes):
    output.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    labels = lines(FAILED / 'teacher/labels.jsonl')
    queries = lines(FAILED / 'teacher/teacher-queries.jsonl')
    selected = read(FAILED / 'selected.json')
    models = {'warm_stm': stm.load_model(WARM / 'model.nnue'),
              'failed_sf500': stm.load_model(FAILED / 'model.nnue')}
    partitions = {name: [r for r in labels if r['partition'] == name] for name in selected}
    report = {'input_sha256': {str(p.relative_to(ROOT)): sha(p) for p in (
                  FAILED / 'model.nnue', WARM / 'model.nnue', FAILED / 'teacher/labels.jsonl',
                  FAILED / 'teacher/teacher-queries.jsonl', FAILED / 'games.json', FAILED / 'training.json')},
              'selection_distribution': {name: distribution(rows) for name, rows in selected.items()},
              'accepted_distribution': {name: distribution(rows) for name, rows in partitions.items()},
              'rejection_by_partition': {name: dict(Counter(reason for q in queries if q['partition'] == name
                                                    for reason in q['reasons'])) for name in selected},
              'errors_by_kind': {}, 'feature_coverage': {}, 'material_counterfactuals': {},
              'first_game': {}, 'search_cost': {},
              'caveat': 'This is diagnosis, not a causal proof or independent strength test. Removing a piece is a valid board counterfactual but need not be a reachable adjacent move.'}
    for name, rows in partitions.items():
        report['errors_by_kind'][name] = {}
        for kind in sorted({r['kind'] for r in rows}):
            chosen = [r for r in rows if r['kind'] == kind]
            indices, targets, turns = stm.pack_records(chosen)
            report['errors_by_kind'][name][kind] = {model_name: base.error_metrics(stm.predict(model, indices, turns), targets)
                                                  for model_name, model in models.items()}

    # The initial position and all single non-king removals retain both kings;
    # FEN validity is checked and fullmove/turn are held fixed.
    initial = chess.Board()
    probes = [initial]
    removals = []
    for square, piece in sorted(initial.piece_map().items()):
        if piece.piece_type == chess.KING:
            continue
        board = initial.copy(stack=False)
        board.remove_piece_at(square)
        board.castling_rights = board.clean_castling_rights()
        if not board.is_valid():
            raise ValueError('Invalid material counterfactual')
        probes.append(board)
        removals.append({'square': chess.square_name(square), 'piece': piece.symbol(),
                         'piece_type': chess.piece_name(piece.piece_type), 'owner': 'white' if piece.color else 'black',
                         'fen': board.fen(en_passant='fen'), 'expected_material_cp': VALUES[piece.piece_type]})
    for model_name, model in models.items():
        values = predict(model, probes)
        rows = []
        for removal, value in zip(removals, values[1:]):
            # Positive means removal hurts its owner, as expected for material.
            owner_sign = 1 if removal['owner'] == 'white' else -1
            rows.append({**removal, 'white_cp_before': int(values[0]), 'white_cp_after': int(value),
                         'owner_loss_cp': int((values[0] - value) * owner_sign)})
        report['material_counterfactuals'][model_name] = {
            'initial_white_cp': int(values[0]), 'rows': rows,
            'by_piece': {piece: summary([r['owner_loss_cp'] for r in rows if r['piece_type'] == piece])
                         for piece in sorted({r['piece_type'] for r in rows})},
            'wrong_sign_count': sum(r['owner_loss_cp'] < 0 for r in rows)}

    games = read(FAILED / 'games.json')['games']
    search_moves = [move for game in games for move in game['search_moves']]
    for role in ('baseline', 'candidate'):
        moves = [m for m in search_moves if m['role'] == role]
        report['search_cost'][role] = {
            'count': len(moves), 'depth': summary([m['depth'] for m in moves]),
            'nodes': summary([m['nodes'] for m in moves]),
            'elapsed_seconds': summary([m['elapsed_seconds'] for m in moves]),
            'nodes_per_engine_second': summary([m['nodes'] / max(.001, m['engine_time_seconds']) for m in moves])}
    training_features = set(report['accepted_distribution']['train']['features'])
    opening_features = set()
    first = games[0]
    before_boards = [chess.Board(m['fen_before']) for m in first['search_moves'][:24]]
    for board in before_boards:
        for view in base.feature_indices(board):
            opening_features.update(int(index) for index in view)
    report['feature_coverage'] = {
        'trained_unique_piece_features': len(training_features),
        'first_game_first24plies_unique_piece_features': len(opening_features),
        'of_those_absent_from_accepted_train': len(opening_features - training_features),
        'missing_features': sorted(opening_features - training_features)}
    game_rows = []
    preds = {name: predict(model, before_boards) for name, model in models.items()}
    native = base.evaluate_engine(ROOT / 'build-m3-stm/regicide.exe', [b.fen(en_passant='fen') for b in before_boards])
    for index, (move, board, scores) in enumerate(zip(first['search_moves'][:24], before_boards, native)):
        row = {'ply': move['ply'], 'role': move['role'], 'move': move['move'], 'fen_before': move['fen_before'],
               'material_white_cp': material(board),
               'classical_static_white_cp': scores[0] * (1 if board.turn else -1),
               'recorded_search_mover_cp': move['score']['cp']}
        row.update({name + '_white_cp': int(values[index]) for name, values in preds.items()})
        game_rows.append(row)
    report['first_game'] = {'opening': first['opening'], 'rows_first24plies': game_rows}

    # A maximum of 16 score queries from one recorded game: before and after its
    # first eight candidate moves. These are not added to training or validation.
    if teacher_probes:
        teacher = Path(read(FAILED / 'protocol.json')['teacher']['profile']['binary']['path'])
        candidates = [m for m in first['search_moves'] if m['role'] == 'candidate'][:8]
        teacher_rows = []
        with chess.engine.SimpleEngine.popen_uci(str(teacher), timeout=30) as engine:
            engine.configure(OPTIONS)
            for move in candidates:
                before = chess.Board(move['fen_before'])
                after = before.copy(stack=False)
                after.push_uci(move['move'])
                row = {'ply': move['ply'], 'move': move['move'], 'candidate_color': move['color']}
                for label, board in [('before', before), ('after', after)]:
                    engine.configure({'Clear Hash': None})
                    last = {}
                    with engine.analysis(board, chess.engine.Limit(nodes=10000), game=object(),
                                         info=chess.engine.INFO_ALL) as analysis:
                        for packet in analysis:
                            if 'score' in packet:
                                last = packet.copy()
                        result = analysis.wait()
                    score, reasons = assess_info(board, last, result.move)
                    row[label] = {'fen': board.fen(en_passant='fen'), 'score': score, 'rejections': reasons,
                                  'depth': last.get('depth'), 'nodes': last.get('nodes'),
                                  'pv': [m.uci() for m in last.get('pv', [])],
                                  'lowerbound': last.get('lowerbound', False),
                                  'upperbound': last.get('upperbound', False)}
                if not row['before']['rejections'] and not row['after']['rejections']:
                    sign = 1 if move['color'] == 'white' else -1
                    row['candidate_score_drop_cp'] = sign * (row['before']['score']['white_cp'] - row['after']['score']['white_cp'])
                teacher_rows.append(row)
        report['recorded_move_teacher_probes'] = {
            'requested_queries': len(candidates) * 2, 'nodes_requested_per_query': 10000,
            'input_binary_sha256': sha(teacher), 'history': 'FEN-only diagnosis; no repetition-history scoring claim',
            'caveat': 'Finite-node before/after root scores vary with search; bounds and mates excluded from numeric drop.',
            'rows': teacher_rows}
    report['elapsed_seconds'] = time.perf_counter() - started
    (output / 'diagnosis.json').write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + '\n', encoding='utf-8')
    print(json.dumps({'accepted_counts': {k: v['count'] for k, v in report['accepted_distribution'].items()},
                      'material_owner_loss_means': {name: {piece: entry['mean'] for piece, entry in result['by_piece'].items()}
                                                    for name, result in report['material_counterfactuals'].items()},
                      'first24plies_unseen_piece_features': report['feature_coverage']['of_those_absent_from_accepted_train'],
                      'elapsed_seconds': report['elapsed_seconds']}, indent=2))
    print(f'Diagnosis written: {output / "diagnosis.json"}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('--teacher-probes', action='store_true')
    args = parser.parse_args()
    run(args.output.resolve(), args.teacher_probes)
