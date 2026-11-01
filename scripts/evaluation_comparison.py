"""Offline, bounded comparison of two engines on a saved local game's history.

Scores from different engines are NOT a common calibrated scale. Disagreement
ranking only selects positions to inspect; it is not accuracy, regret or Elo.
Output contains the personal game, and should stay in an ignored build folder.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import time

import chess
import chess.engine

from engine_profile import load_profile


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def positions(record):
    """Replay full history, rejecting corrupt/non-legal archived games."""
    board = chess.Board(record['initial_fen'])
    if not board.is_valid():
        raise ValueError('Invalid initial position')
    rows = []
    for ply, uci in enumerate(record['uci_moves'], 1):
        move = chess.Move.from_uci(uci)
        if move not in board.legal_moves:
            raise ValueError(f'Illegal archived move at ply {ply}: {uci}')
        rows.append((ply, board.copy(stack=True), uci, board.san(move)))
        board.push(move)
    if board.fen(en_passant='fen') != chess.Board(record['final_fen']).fen(en_passant='fen'):
        raise ValueError('Final FEN does not match game history')
    if not rows:
        raise ValueError('Game has no moves')
    return rows


def query(engine, board, milliseconds, root_move=None):
    """Reset hash per query; validate every published principal-variation move."""
    engine.configure({'Clear Hash': None})
    started = time.perf_counter()
    args = {'game': object(), 'info': chess.engine.INFO_ALL}
    if root_move is not None:
        move = chess.Move.from_uci(root_move)
        if move not in board.legal_moves:
            raise ValueError('Illegal constrained root move')
        args['root_moves'] = [move]
    info = engine.analyse(board, chess.engine.Limit(time=milliseconds / 1000), **args)
    wall_ms = round((time.perf_counter() - started) * 1000, 2)
    score = info.get('score')
    if not isinstance(score, chess.engine.PovScore) or score.turn != board.turn:
        raise ValueError('Missing or inconsistent score perspective')
    replay, san, pv = board.copy(stack=True), [], info.get('pv', [])
    if not pv:
        raise ValueError('Missing principal variation')
    for move in pv:
        if move not in replay.legal_moves:
            raise ValueError('Illegal principal variation')
        san.append(replay.san(move))
        replay.push(move)
    if root_move is not None and pv[0].uci() != root_move:
        raise ValueError('Engine ignored constrained root move')
    return {'cp_white': score.white().score(), 'mate_white': score.white().mate(),
            'bound': 'lower' if info.get('lowerbound') else 'upper' if info.get('upperbound') else 'exact',
            'depth': info.get('depth'), 'nodes': info.get('nodes'),
            'engine_time_ms': round(info.get('time', 0) * 1000), 'wall_ms': wall_ms,
            'requested_ms': milliseconds, 'best_uci': pv[0].uci(),
            'pv_uci': [move.uci() for move in pv], 'pv_san': san,
            'wdl_white': list(info['wdl'].white()) if info.get('wdl') else None}


def disagreement(left, right):
    """Inspection priority only, never a common-unit error metric."""
    if left['bound'] != 'exact' or right['bound'] != 'exact':
        return -1
    lm, rm = left['mate_white'], right['mate_white']
    if lm is not None or rm is not None:
        return 100000 if (lm is None) != (rm is None) else 0
    lc, rc = left['cp_white'], right['cp_white']
    opposed = lc * rc < 0 and min(abs(lc), abs(rc)) >= 50
    return abs(lc - rc) + (10000 if opposed else 0)


def run(args):
    output = args.output.resolve()
    if output.exists():
        raise ValueError('Use a fresh output directory')
    profiles = [load_profile(p) for p in (args.regicide_profile, args.reference_profile)]
    inputs = []
    for path in args.game:
        raw = path.read_bytes()
        record = json.loads(raw.decode('utf-8-sig'))
        inputs.append((path.resolve(), hashlib.sha256(raw).hexdigest(), positions(record)))
    output.mkdir(parents=True)
    manifest = {'created_at': datetime.now(timezone.utc).isoformat(),
                'python': platform.python_version(), 'chess': chess.__version__,
                'platform': platform.platform(), 'cpu': args.cpu,
                'script_sha256': sha256(__file__), 'profiles': profiles,
                'conditions': {'ms_per_position': args.time_ms, 'hash_mb': 16,
                               'threads_per_engine': 1, 'multipv': 1,
                               'history': 'full archived history', 'hash': 'cold per query',
                               'order': 'sequential, alternating engine order each ply',
                               'external_load': 'not controlled; no speed or Elo claim'},
                'sources': [{'path': str(p), 'sha256': digest, 'positions': len(rows)}
                            for p, digest, rows in inputs],
                'limitations': ['Raw cp from these engines are not calibrated to the same scale.',
                                'Finite Stockfish search is a reference, not a proof.',
                                'Selected games are a convenience sample, not a strength benchmark.']}
    records, boards = [], {}
    with ExitStack() as stack:
        engines = []
        for profile in profiles:
            extra = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
            engine = stack.enter_context(chess.engine.SimpleEngine.popen_uci(
                [profile['binary']['path'], *profile['args']], timeout=20, **extra))
            options = {**profile['options'], 'Hash': 16}
            if 'Threads' in engine.options:
                options['Threads'] = 1
            if 'Move Overhead' in engine.options:
                options['Move Overhead'] = 20
            if 'UCI_ShowWDL' in engine.options:
                options['UCI_ShowWDL'] = True
            engine.configure(options)
            profile['observed_id'], profile['effective_options'] = dict(engine.id), options
            engines.append(engine)
        (output / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
        with (output / 'positions.jsonl').open('x', encoding='utf-8') as log:
            for game_index, (_, _, source_rows) in enumerate(inputs):
                for ply, board, uci, san in source_rows:
                    key = f'g{game_index + 1}-ply{ply}'
                    values = {}
                    for index in ([0, 1] if ply % 2 else [1, 0]):
                        values[['regicide', 'reference'][index]] = query(engines[index], board, args.time_ms)
                    row = {'key': key, 'game': game_index + 1, 'ply': ply,
                           'fen': board.fen(en_passant='fen'), 'actual_uci': uci, 'actual_san': san,
                           'turn': 'white' if board.turn else 'black', **values}
                    row['inspection_priority'] = disagreement(values['regicide'], values['reference'])
                    records.append(row)
                    boards[key] = board
                    log.write(json.dumps(row) + '\n')
                    log.flush()
                    if ply % 10 == 0:
                        print(f'game {game_index + 1}: {ply}/{len(source_rows)} positions', flush=True)
        # Use the same reference engine to judge both choices, rather than
        # calling the difference between two unrelated CP scales a move loss.
        candidates = sorted([r for r in records if r['regicide']['best_uci'] != r['reference']['best_uci']],
                            key=lambda r: r['inspection_priority'], reverse=True)[:args.confirm_count]
        confirmations = []
        for row in candidates:
            board = boards[row['key']]
            confirmations.append({'key': row['key'], 'regicide_move': row['regicide']['best_uci'],
                'reference_unrestricted': query(engines[1], board, args.confirm_ms),
                'reference_regicide_move': query(engines[1], board, args.confirm_ms, row['regicide']['best_uci']),
                'regicide_longer': query(engines[0], board, args.confirm_ms)})
        (output / 'confirmations.json').write_text(json.dumps(confirmations, indent=2), encoding='utf-8')
    for path, digest, _ in inputs:
        if sha256(path) != digest:
            raise ValueError('Source game changed during comparison')
    summary = {'positions': len(records),
               'same_first_choice': sum(r['regicide']['best_uci'] == r['reference']['best_uci'] for r in records),
               'mate_visibility_disagreements': sum((r['regicide']['mate_white'] is None) !=
                                                   (r['reference']['mate_white'] is None) for r in records),
               'confirmed_keys': [r['key'] for r in confirmations], 'sources_unchanged': True}
    (output / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(json.dumps(summary), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--game', type=Path, action='append', required=True)
    parser.add_argument('--regicide-profile', type=Path, default=Path('config/engine-profiles/classical.json'))
    parser.add_argument('--reference-profile', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--time-ms', type=int, default=1000)
    parser.add_argument('--confirm-ms', type=int, default=3000)
    parser.add_argument('--confirm-count', type=int, default=3)
    parser.add_argument('--cpu', default=platform.processor())
    args = parser.parse_args()
    if not 100 <= args.time_ms <= 5000 or not 100 <= args.confirm_ms <= 10000 or not 0 <= args.confirm_count <= 10:
        parser.error('Use bounded 100..5000ms queries, 100..10000ms confirmation, and 0..10 confirmations')
    run(args)
