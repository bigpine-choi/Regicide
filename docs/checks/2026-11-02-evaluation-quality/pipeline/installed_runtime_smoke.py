"""Owned temporary game only; no GUI/server or personal game interaction."""
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
import chess
import chess.pgn
from play_human import HumanGame
from runtime_engine import resolve_runtime_engine

binary = resolve_runtime_engine(ROOT)
expected = '5011da4c5e6ef01c452bd7125e4e22b372d2c9cfb858ed8759057325acb0e6ba'
assert hashlib.sha256(binary.read_bytes()).hexdigest() == expected
with tempfile.TemporaryDirectory(prefix='regicide-installed-smoke-') as directory:
    game = HumanGame(binary, directory)
    try:
        game._opening_seed = 'evaluation-activity-installed-smoke-v1'
        state = game.state()
        assert state['engine']['evaluation'] == 'classical_activity_v1'
        assert state['engine']['binary_sha256'] == expected
        deadline = time.monotonic() + 8
        while game.evaluation()['status'] != 'ready' and time.monotonic() < deadline:
            time.sleep(.02)
        evaluation = game.evaluation()
        assert evaluation['status'] == 'ready', evaluation
        assert evaluation['budget_ms'] == 3000
        assert evaluation['source_fen'] == state['fen']
        after_human = game.human_move('e2e4', state['revision'])
        after_engine = game.engine_move(after_human['revision'])
        assert after_engine['engine']['movetime_ms'] == 1000
        saved = chess.pgn.read_game(io.StringIO(game.pgn()))
        assert saved is not None and not saved.errors
        assert saved.headers['Evaluation'] == 'classical_activity_v1'
        board = saved.board()
        moves = []
        for move in saved.mainline_moves():
            assert move in board.legal_moves
            moves.append(move.uci())
            board.push(move)
        assert len(moves) == 2 and moves[0] == 'e2e4'
        result = {'status': 'passed', 'binary': str(binary), 'binary_sha256': expected,
                  'evaluation_id': after_engine['engine']['evaluation'],
                  'evaluation': evaluation, 'moves': moves,
                  'ai_budget_ms': after_engine['engine']['movetime_ms'],
                  'opening_policy': game._opening_metadata()['policy'],
                  'scope': 'Installed runtime; one temporary round trip; no strength or GUI claim'}
        Path(__file__).with_name('installed-runtime-smoke.json').write_text(
            json.dumps(result, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({key: result[key] for key in ('status', 'evaluation_id', 'moves', 'ai_budget_ms', 'opening_policy')}))
    finally:
        game.close()
