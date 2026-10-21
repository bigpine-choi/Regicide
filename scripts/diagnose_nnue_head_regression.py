"""Bounded, exposed-position audit of the output-head regression; never trains."""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
from pathlib import Path
import subprocess
import time

import chess
import chess.engine
import numpy as np

import neural_stm as stm
from stockfish_teacher import _json_info

ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / 'docs/checks/2026-10-22-nnue-output-only/attempt-001/move-choice-gate.json'
ENGINE = ROOT / 'build-m3-leaf/regicide.exe'
COLLECTOR = ROOT / 'build-m3-choice/regicide_choice_data.exe'
MOVES = ('f6g4', 'c8f5', 'f6e4')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def board_from(row):
    board = chess.Board(row['initial_fen'])
    for move in row['history']:
        board.push_uci(move)
    assert board.fen(en_passant='fen') == row['fen']
    return board


def vector(board, model):
    indices, _, turns = stm.pack_records([{'fen': board.fen(), 'target_white_cp': 0}])
    score, cache = stm._integer_forward(model, indices, turns)
    difference = cache['differences'][0]
    return {'white_cp': int(score[0]), 'hidden_difference': difference.tolist(),
            'per_hidden_white_cp_before_truncation':
            (difference * model['output_weights'] / model['output_scale']).tolist()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    assert not args.output.exists(), 'Do not overwrite a bounded diagnostic run'
    gate = json.loads(GATE.read_text(encoding='utf-8'))
    case = next(row for row in gate['cases'] if row['source']['query_id'] == 'choice-gate-opening-30')
    source = case['source']
    board = board_from(source)
    model_paths = {key: Path(case['searches'][key]['options']['NeuralModelFile'])
                   for key in ('warm', 'candidate')}
    models = {key: stm.load_model(path) for key, path in model_paths.items()}
    for key in ('input_weights', 'hidden_bias', 'turn_weights'):
        assert np.array_equal(models['warm'][key], models['candidate'][key])
    bindings = {str(path): digest(path) for path in [GATE, ENGINE, COLLECTOR, Path(__file__),
                                                    ROOT / 'scripts/neural_stm.py', *model_paths.values()]}
    plan = {'maximum_native_searches': 32, 'maximum_nodes_per_search': 10000,
            'new_teacher_queries': 0, 'training_runs': 0, 'fresh_gate_access': False,
            'forced_grid': {'models': ['warm', 'candidate'], 'moves': list(MOVES),
                            'depths': [2, 3], 'profiles': ['default', 'exact_no_tt_pvs_lmr']},
            'full_root_default_reproductions': 2, 'collector_depth2_searches': 6}
    result = {'schema_version': 1, 'started_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'plan': plan, 'input_sha256': bindings, 'source_case': case,
              'output_weights': {key: model['output_weights'].tolist() for key, model in models.items()},
              'searches': [], 'support_receipts': [], 'static_lines': []}
    started = time.perf_counter()
    engine = chess.engine.SimpleEngine.popen_uci([str(ENGINE), 'uci'])
    try:
        for name in ('warm', 'candidate'):
            for profile in ('default', 'exact_no_tt_pvs_lmr'):
                options = dict(case['searches'][name]['options'])
                if profile != 'default':
                    options.update(UseTranspositionTable=False, PVS=False, UseLMR=False)
                engine.configure(options)
                if profile == 'default':
                    grid = [(None, None)] + [(move, depth) for depth in (2, 3) for move in MOVES]
                else:
                    grid = [(move, depth) for depth in (2, 3) for move in MOVES]
                for move, depth in grid:
                    engine.configure({'Clear Hash': None})
                    last, packets = {}, []
                    limit = chess.engine.Limit(nodes=10000, depth=depth)
                    kwargs = {'root_moves': [chess.Move.from_uci(move)]} if move else {}
                    with engine.analysis(board, limit, game=object(), info=chess.engine.INFO_ALL, **kwargs) as analysis:
                        for packet in analysis:
                            if 'score' in packet:
                                last = packet.copy()
                                packets.append(_json_info(packet))
                        best = analysis.wait().move
                        aggregate = analysis.info
                    assert best in board.legal_moves
                    pv = last.get('pv', [])
                    leaf = board.copy(stack=True)
                    for step in pv:
                        assert step in leaf.legal_moves
                        leaf.push(step)
                    score = last['score'].white().score()
                    result['searches'].append({'model': name, 'profile': profile, 'forced_move': move,
                        'requested_depth': depth, 'requested_nodes': 10000,
                        'completed_depth': last.get('depth'), 'bestmove': best.uci(), 'white_cp': score,
                        'nodes': aggregate.get('nodes'), 'pv_uci': [x.uci() for x in pv],
                        'endpoint_fen': leaf.fen(en_passant='fen'), 'endpoint_static': vector(leaf, models[name]),
                        'endpoint_is_backed_score': score == stm.quantized_white(leaf, models[name]),
                        'score_packets': packets, 'raw_info': _json_info(aggregate), 'options': options})
    finally:
        engine.quit()
    for name, path in model_paths.items():
        requests = [f'{name}-{move}\t{source["initial_fen"]}\t{" ".join(source["history"])}\t{move}'
                    for move in MOVES]
        completed = subprocess.run([str(COLLECTOR), '--model', str(path), '--depth', '2', '--nodes', '10000'],
                                   input='\n'.join(requests)+'\n', capture_output=True, text=True, check=True,
                                   timeout=30, cwd=ROOT)
        receipts = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
        assert len(receipts) == 3
        result['support_receipts'].extend({'model': name, 'receipt': row} for row in receipts)
    # Exposed teacher and native continuations only; no new teacher calls.
    lines = {'root': []}
    for key, receipt in case['child_teacher_receipts'].items():
        first = case['searches'][key]['move_uci']
        lines[f'teacher_after_{first}'] = [first] + receipt['pv_uci']
    lines['teacher_root'] = case['teacher_receipt']['pv_uci']
    for move in MOVES:
        lines['child_'+move] = [move]
    for key, moves in lines.items():
        replay = board.copy(stack=True)
        line = []
        for ply in range(len(moves)+1):
            snapshots = {name: vector(replay, model) for name, model in models.items()}
            assert snapshots['warm']['hidden_difference'] == snapshots['candidate']['hidden_difference']
            line.append({'ply': ply, 'fen': replay.fen(en_passant='fen'), 'models': snapshots,
                         'white_cp_delta': snapshots['candidate']['white_cp']-snapshots['warm']['white_cp']})
            if ply < len(moves):
                replay.push_uci(moves[ply])
        result['static_lines'].append({'name': key, 'moves': moves, 'positions': line})
    result['actual_native_searches'] = len(result['searches']) + len(result['support_receipts'])
    assert result['actual_native_searches'] <= plan['maximum_native_searches']
    result['elapsed_seconds'] = time.perf_counter()-started
    result['completed_utc'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8', newline='\n')
    print(json.dumps({'native_searches': result['actual_native_searches'], 'elapsed_seconds': result['elapsed_seconds']}))


if __name__ == '__main__':
    main()
