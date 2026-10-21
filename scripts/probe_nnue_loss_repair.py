"""One bounded, post-selection move-choice regression on recorded loss roots.

The selected positions are development examples, including repair training
rows. This probe is diagnostic evidence, not an independent strength test.
It performs at most twenty Regicide searches and never changes a model.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time

import chess
import chess.engine

import neural_stm as stm
from stockfish_teacher import source_board, _json_info

ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT = ROOT / 'docs/checks/2026-10-22-nnue-loss-repair'
DATA = EXPERIMENT / 'data-001'
OLD = ROOT / 'docs/checks/2026-10-22-nnue-diversity/attempt-001/model.nnue'
NEW = EXPERIMENT / 'attempt-001/model.nnue'
BINARY = ROOT / 'build-m3-stm/regicide.exe'
OUTPUT = EXPERIMENT / 'diagnosis/search-recheck.json'
NODES = 10000


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def lines(path):
    return [json.loads(row) for row in path.read_text(encoding='utf-8').splitlines()]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(result):
    OUTPUT.write_text(json.dumps(result, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def main():
    if OUTPUT.exists():
        raise FileExistsError('Refuse to rerun or overwrite a completed/partial search probe')
    assert sha(OLD) == 'c73b510cd83ca221829eb9e1bf781b86d28c5b9c47ed922d8f9fa30e08385c86'
    assert sha(NEW).startswith('02ccdf')
    assert sha(BINARY) == '97140c4ad1c40a9f2c67e2fb9a8c0718fe70722e83dcbe50a7011b71c97558d1'
    old_model = stm.load_model(OLD)
    new_model = stm.load_model(NEW)
    labels = lines(DATA / 'roots-teacher/labels.jsonl')
    receipts = {row['source_query_id']: row
                for row in lines(DATA / 'roots-teacher/teacher-queries.jsonl')}
    games = read(ROOT / 'docs/checks/2026-10-22-nnue-diversity/attempt-001/games.json')
    loss_games = {g['game']: g for g in games['games']
                  if g['result'] == ('0-1' if g['white'] == 'candidate' else '1-0')}
    assert len(loss_games) == 10
    candidates = {}
    for row in labels:
        receipt = receipts[row['query_id']]
        assert receipt['status'] == 'accepted' and receipt['reasons'] == []
        assert receipt['score']['kind'] == 'cp'
        assert receipt['raw_score_info_packet'].get('lowerbound', False) is False
        assert receipt['raw_score_info_packet'].get('upperbound', False) is False
        assert row['teacher_white_cp'] == receipt['score']['white_cp']
        board = source_board(row)
        assert row['source_game'] in loss_games and row['source_role'] == 'root'
        original = loss_games[row['source_game']]
        assert row['initial_fen'] == original['initial_fen']
        assert row['history'] == original['uci_moves'][:row['source_ply'] - 1]
        static = stm.quantized_white(board, old_model)
        error = abs(static - row['teacher_white_cp'])
        if error < 150:
            continue
        candidates.setdefault(row['source_game'], []).append({
            'source': row, 'teacher_receipt': receipt,
            'old_static_white_cp': static, 'new_static_white_cp': stm.quantized_white(board, new_model),
            'original_abs_static_error_cp': error,
            'teacher_bestmove_uci': receipt['bestmove_uci'],
            'searches': {}})
    selected = [min(rows, key=lambda item: item['source']['source_ply'])
                for _, rows in sorted(candidates.items())]
    assert 0 < len(selected) <= 10
    options = read(ROOT / 'config/engine-profiles/classical.json')['options']
    options['UseNNUEEvaluation'] = True
    inputs = [Path(__file__), BINARY, OLD, NEW,
              DATA / 'roots-teacher/labels.jsonl',
              DATA / 'roots-teacher/teacher-queries.jsonl',
              ROOT / 'config/engine-profiles/classical.json',
              EXPERIMENT / 'attempt-001/selection.json']
    result = {
        'schema_version': 1, 'started_at': datetime.now(timezone.utc).isoformat(),
        'purpose': 'Post-selection local move-choice regression; known development loss positions, not strength evidence',
        'selection': 'Earliest accepted exact root teacher CP per each recorded loss, absolute old static teacher error >=150cp; no refill',
        'selection_uses_new_model': False, 'model_selection_finished_before_probe': True,
        'scope': 'No Stockfish queries, training, games, retries, model selection or promotion',
        'state': 'Original initial_fen plus complete move history; Clear Hash then distinct game object per search',
        'requested_nodes_per_search': NODES, 'maximum_searches': 20,
        'requested_nodes_total': NODES * len(selected) * 2,
        'node_limit_note': 'go nodes request; completed nodes may overshoot slightly',
        'command': [str(BINARY), 'uci'], 'common_options': options,
        'model_paths': {'before': str(OLD), 'after': str(NEW)},
        'input_sha256': {str(path): sha(path) for path in inputs},
        'selected_positions': len(selected),
        'skipped_loss_games': sorted(set(loss_games) - set(candidates)),
        'cases': selected, 'completed': False}
    write(result)
    started = time.perf_counter()
    for model_name, path in [('before', OLD), ('after', NEW)]:
        configured = dict(options, NeuralModelFile=str(path))
        with chess.engine.SimpleEngine.popen_uci([str(BINARY), 'uci'], timeout=30.0) as engine:
            engine.configure(configured)
            for case in selected:
                board = source_board(case['source'])
                engine.configure({'Clear Hash': None})
                tick = time.perf_counter()
                info = engine.analyse(board, chess.engine.Limit(nodes=NODES),
                                      game=object(), info=chess.engine.INFO_ALL)
                elapsed = time.perf_counter() - tick
                pv = info.get('pv', [])
                assert pv and pv[0] in board.legal_moves
                check = board.copy(stack=True)
                for move in pv:
                    assert move in check.legal_moves
                    check.push(move)
                score = info.get('score')
                assert isinstance(score, chess.engine.PovScore)
                case['searches'][model_name] = {
                    'model_sha256': sha(path), 'options': configured,
                    'move_uci': pv[0].uci(), 'move_san': board.san(pv[0]),
                    'matches_teacher_bestmove': pv[0].uci() == case['teacher_bestmove_uci'],
                    'white_cp': score.white().score(), 'white_mate': score.white().mate(),
                    'lowerbound': info.get('lowerbound', False), 'upperbound': info.get('upperbound', False),
                    'depth': info.get('depth'), 'nodes': info.get('nodes'),
                    'pv_uci': [move.uci() for move in pv], 'elapsed_seconds': elapsed,
                    'raw_info': _json_info(info)}
                write(result)
    result['summary'] = {
        'searches': 2 * len(selected),
        'before_teacher_move_matches': sum(case['searches']['before']['matches_teacher_bestmove'] for case in selected),
        'after_teacher_move_matches': sum(case['searches']['after']['matches_teacher_bestmove'] for case in selected),
        'move_changes': sum(case['searches']['before']['move_uci'] != case['searches']['after']['move_uci'] for case in selected),
        'elapsed_seconds': time.perf_counter() - started}
    result['completed'] = True
    write(result)


if __name__ == '__main__':
    main()
