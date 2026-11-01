"""Own-engine ranked advice reproduced independently at a fixed depth.

Run: python tests/ranked_hint_real_tests.py [path/to/regicide]
Uses disposable helpers only; never reads personal games or existing processes.
"""
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import chess
import chess.engine
from move_hints import MoveHints

BINARY = Path(sys.argv[1]).resolve(strict=True) if len(sys.argv) > 1 else ROOT / 'build/regicide.exe'
CASES = [
    ('white-opening', chess.STARTING_FEN, 100),
    ('white-opening-default', chess.STARTING_FEN, 1000),
    ('black-opening', 'rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 2', 100),
    ('en-passant', '4k3/8/8/3pP3/8/8/8/4K3 w - d6 0 2', 100),
    ('promotion', '7k/P7/8/8/8/8/8/7K w - - 0 1', 100),
    ('one-legal-evasion', '8/8/8/8/8/8/5k2/r6K w - - 0 1', 100),
    ('two-legal-evasions', '7r/8/8/8/5k2/8/8/7K w - - 0 1', 100),
]
results = []
for name, fen, milliseconds in CASES:
    board = chess.Board(fen)
    assert board.is_valid() and not board.is_game_over()
    before = board.fen(en_passant='fen')
    hints = MoveHints(BINARY)
    hints.update_position(game_id=name, revision=3, board=board,
                          human_color=board.turn, movetime_ms=milliseconds,
                          engine_name='Regicide', active=True)
    start = time.monotonic()
    hints.set_enabled(True)
    try:
        deadline = start + 6
        while time.monotonic() < deadline:
            packet = hints.snapshot()
            if packet['status'] in ('ready', 'unavailable') and not packet.get('calculating', False):
                break
            time.sleep(.005)
        assert packet['status'] == 'ready', (name, packet)
        elapsed = time.monotonic() - start
        candidates = packet['candidates']
        assert len(candidates) == min(3, board.legal_moves.count()), (name, packet)
        assert len({row['move'] for row in candidates}) == len(candidates)
        assert [row['rank'] for row in candidates] == list(range(1, len(candidates) + 1))
        depth = candidates[0]['depth']
        assert depth >= 1 and all(row['depth'] == depth for row in candidates)
        assert packet['move'] == candidates[0]['move']
        for row in candidates:
            move = chess.Move.from_uci(row['move'])
            assert move in board.legal_moves
            assert row['san'] == board.san(move)
        # Reproduce the same-depth selection independently via UCI and compare
        # own-engine scores. This is not proof of objective chess superiority.
        peer = chess.engine.SimpleEngine.popen_uci([str(BINARY), 'uci'], timeout=3)
        remaining = list(board.legal_moves)
        expected = []
        try:
            for rank in range(len(candidates)):
                kwargs = {'root_moves': remaining} if rank else {}
                reply = peer.play(board.copy(stack=True), chess.engine.Limit(depth=depth, time=5),
                                  game=object(), info=chess.engine.INFO_ALL, **kwargs)
                info = reply.info
                assert info.get('depth') == depth, (name, depth, info)
                assert not info.get('lowerbound') and not info.get('upperbound')
                score = info['score'].pov(board.turn)
                expected.append((reply.move.uci(), score))
                remaining.remove(reply.move)
        finally:
            peer.close()
            peer.returncode.result(timeout=2)
        expected.sort(key=lambda row: row[0])
        expected.sort(key=lambda row: row[1], reverse=True)
        assert [row['move'] for row in candidates] == [row[0] for row in expected], (name, candidates, expected)
        assert board.fen(en_passant='fen') == before
        off = hints.set_enabled(False)
        assert off['candidates'] == [] and off['move'] is None
        results.append({'case': name, 'budget_ms': milliseconds,
                        'wall_seconds_including_launch': round(elapsed, 4),
                        'legal_moves': board.legal_moves.count(), 'depth': depth,
                        'candidates': candidates, 'fixed_depth_reproduced': True})
    finally:
        hints.close()

output = {'engine_sha256': hashlib.sha256(BINARY.read_bytes()).hexdigest(),
          'cases': results, 'count': len(results),
          'note': 'Own-engine fixed-depth reproduction, legality and lifecycle only. No strength claim.'}
output_dir = ROOT / 'build/ranked-hints-qa'
output_dir.mkdir(parents=True, exist_ok=True)
(output_dir / 'real-results.json').write_text(
    json.dumps(output, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print(json.dumps(output, ensure_ascii=False, indent=2))
