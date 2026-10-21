"""Exploratory, frozen same-position diagnostics of completed A/B games.

No weights, original artifacts, or match results are changed. Classical
100k child scores are a finite own-engine reference, not optimal chess.
"""
from __future__ import annotations
import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time
from unittest.mock import patch

for key in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS'):
    os.environ[key] = '1'
import chess
import numpy as np
import neural_training as base
import neural_precision as precision
from neural_search_data import engine as bridge
from run_neural_strength_comparison import Awake


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def rows(path):
    return [json.loads(line) for line in Path(path).read_text(encoding='utf-8').splitlines() if line]


def entry(path):
    p = Path(path).resolve()
    return {'path': str(p), 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}


def write(path, value):
    with Path(path).open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, sort_keys=True, indent=2, allow_nan=False)
        stream.write('\n')


def board_for(row):
    board = chess.Board(row['initial_fen'])
    for move in row['moves_before']:
        board.push_uci(move)
    assert board.fen(en_passant='fen') == row['fen_before']
    assert board.is_valid() and board.outcome(claim_draw=True) is None
    return board


def exact_white(record, board):
    if (record['score']['kind'] == 'cp' and not record['lowerbound']
            and not record['upperbound'] and record['completed'] and record['depth'] > 0):
        return record['score']['stm'] * (1 if board.turn else -1)
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--selection', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    run, output = args.run.resolve(), args.output.resolve()
    if output.exists():
        raise ValueError('Preserve existing diagnostics; use a fresh output')
    cases = rows(args.selection)
    assert len(cases) == 76 and len({r['game'] for r in cases}) == 76
    frozen, manifest = read(run/'freeze.json'), read(run/'training/models.json')
    models = {r['model_id']: r for r in manifest['models']}
    for r in cases:
        board_for(r)
        assert r['played_uci'] in [m.uci() for m in board_for(r).legal_moves]
    # One deterministic legal counterfactual pair per heldout source family.
    # Both boards start from FEN, so neither has privileged repetition history.
    by_family = defaultdict(list)
    for r in rows(run/'heldout/test.jsonl'):
        by_family[r['family_id']].append(r)
    turn_pairs = []
    for family, group in sorted(by_family.items()):
        for row in sorted(group, key=lambda r: r['query_id']):
            if chess.Board(row['fen']).ep_square is not None:
                continue
            pair = []
            for color in (chess.WHITE, chess.BLACK):
                b = chess.Board(row['fen'])
                b.turn = color
                if not b.is_valid() or b.is_check() or b.outcome(claim_draw=True):
                    break
                pair.append(b)
            if len(pair) == 2:
                assert base.feature_indices(pair[0]) == base.feature_indices(pair[1])
                turn_pairs.append({'family_id': family, 'source_query_id': row['query_id'],
                    'kind': row['kind'], 'fens': [b.fen(en_passant='fen') for b in pair]})
                break
    assert len(turn_pairs) == 16
    output.mkdir(parents=True)
    write(output/'turn-pairs.json', turn_pairs)
    sources = [Path(__file__), Path(base.__file__), Path(precision.__file__), Path(bridge.__file__),
        run/'freeze.json', run/'training/models.json', run/'training/model-freeze.json',
        run/'heldout/test.jsonl', args.selection, args.selection.parent/'selection-manifest.json',
        output/'turn-pairs.json', Path(frozen['engine']['path'])]
    inputs = {str(p.resolve()): entry(p) for p in sources}
    inputs.update({r['path']: {'path': r['path'], 'sha256': r['sha256']} for r in models.values()})
    for r in inputs.values():
        assert entry(r['path']) == r
    plan = {'created_utc': datetime.now(timezone.utc).isoformat(), 'inputs': inputs,
        'cases': 76, 'A_loss_cases': 27, 'B_loss_cases': 49, 'turn_pairs': 16,
        'classical_nodes': 100000, 'neural_nodes': 10000, 'wall_budget_seconds': 600,
        'max_queries': 564, 'max_requested_nodes': 42720000,
        'selection': 'All decisive games, predetermined material/mate mechanical pivots; no outcome-based position cherry-picking',
        'candidates': 'Actual played move plus A10k, B10k, classical100k root bestmoves; each unique child queried at100k',
        'limits': 'Same teacher family, finite horizon, selected losing positions only, turn flips artificial; exploratory only'}
    write(output/'diagnostic-freeze.json', plan)
    start, deadline = time.monotonic(), time.monotonic()+600
    query_records, queries = {}, []
    awake = Awake()
    awake.start()
    try:
        def query(teacher, board, key):
            assert len(queries) < 564
            result = teacher.query(board, key)
            record = {'query_id': key, 'initial_fen': board.root().fen(en_passant='fen'),
                'history': [m.uci() for m in board.move_stack], 'fen': board.fen(en_passant='fen'),
                'options': teacher.options, 'result': result, 'exact_white_cp': exact_white(result, board)}
            log.write(json.dumps(record, sort_keys=True)+'\n')
            log.flush()
            queries.append(record)
            query_records[key] = record
            return result

        with (output/'queries.jsonl').open('x', encoding='utf-8', newline='\n') as log:
            neural = {}
            for model_id, descriptor in sorted(models.items()):
                selected = [r for r in cases if r['seed'] == descriptor['seed']]
                opts = {'UseNNUEEvaluation': True, 'UseIncrementalNeuralEvaluation': True,
                        'NeuralModelFile': descriptor['path']}
                raw_path = output/(model_id+'-uci.jsonl')
                with patch.dict(bridge.CLASSICAL_OPTIONS, opts), bridge.EngineTeacher(
                        frozen['engine']['path'], raw_path, 10000, deadline, search_timeout=10) as teacher:
                    for r in selected:
                        key = r['query_id']+'/'+descriptor['arm']+'10k'
                        neural[r['query_id'], descriptor['arm']] = query(teacher, board_for(r), key)
                ack = 'info string neural model checksum '+str(descriptor['checksum'])
                assert any(r['direction'] == 'recv' and r['line'] == ack for r in rows(raw_path)), model_id
            game_records, turn_results = [], []
            with bridge.EngineTeacher(frozen['engine']['path'], output/'classical-uci.jsonl',
                                     100000, deadline, search_timeout=10) as teacher:
                for r in cases:
                    b, mover = board_for(r), 1 if r['loser_color'] == 'white' else -1
                    ref = query(teacher, b, r['query_id']+'/C100k')
                    candidates = {'played': r['played_uci'], 'A10k': neural[r['query_id'], 'A']['bestmove_uci'],
                        'B10k': neural[r['query_id'], 'B']['bestmove_uci'], 'C100k': ref['bestmove_uci']}
                    values = {}
                    for move in sorted(set(candidates.values())):
                        assert move and chess.Move.from_uci(move) in b.legal_moves
                        child = b.copy(stack=True)
                        child.push_uci(move)
                        terminal = child.outcome(claim_draw=True)
                        key = r['query_id']+'/child/'+move
                        if terminal is None:
                            query(teacher, child, key)
                            val = query_records[key]['exact_white_cp']
                            values[move] = {'white_cp': val, 'mover_cp': None if val is None else mover*val,
                                            'query_id': key}
                        else:
                            values[move] = {'white_cp': 0 if terminal.winner is None else None,
                                'mover_cp': 0 if terminal.winner is None else None,
                                'terminal': str(terminal.termination), 'winner': terminal.winner}
                    game_records.append({'query_id': r['query_id'], 'game': r['game'], 'seed': r['seed'],
                        'losing_arm': r['losing_arm'], 'family_id': r['family_id'],
                        'fen_before': r['fen_before'], 'original_played_san': r['played_san'],
                        'classical_root_white_cp': query_records[r['query_id']+'/C100k']['exact_white_cp'],
                        'candidate_moves': candidates, 'child_reference': values})
                    if len(game_records) % 10 == 0:
                        print(json.dumps({'loss_positions_complete': len(game_records), 'planned': 76}), flush=True)
                for pair in turn_pairs:
                    pair_values = []
                    for i, fen in enumerate(pair['fens']):
                        b = chess.Board(fen)
                        key = 'turn/'+pair['family_id']+('/white' if b.turn else '/black')
                        query(teacher, b, key)
                        pair_values.append(query_records[key]['exact_white_cp'])
                    turn_results.append({**pair, 'search100k_white_cp': pair_values,
                        'pair_mae_lower_bound_cp': abs(pair_values[0]-pair_values[1])/2
                        if None not in pair_values else None})
            write(output/'game-comparisons.json', game_records)
            write(output/'turn-comparisons.json', turn_results)
    finally:
        awake.close()
    for r in inputs.values():
        assert entry(r['path']) == r
    summary = {'status': 'PASS', 'created_utc': datetime.now(timezone.utc).isoformat(),
        'queries': len(queries), 'requested_nodes': sum(r['result']['requested_nodes'] for r in queries),
        'actual_nodes': sum(r['result']['nodes'] or 0 for r in queries),
        'wall_seconds': time.monotonic()-start, 'original_inputs_unchanged': True,
        'wake_state': awake.record, 'scope': plan['limits']}
    write(output/'execution.json', summary)
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == '__main__':
    main()
