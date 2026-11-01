from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time

root = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(root / 'scripts'))
import chess
import opening_variety as variety
from play_human import HumanGame
from game_archive import GameArchive

out = root / 'build/opening-variety-replay'
out.mkdir(parents=True, exist_ok=True)
cases = [([], 'white_first'), (['e2e4'], 'black_after_e4'),
         (['d2d4'], 'black_after_d4'), (['c2c4'], 'black_after_c4'),
         (['g1f3'], 'black_after_nf3')]
records = []
with tempfile.TemporaryDirectory(prefix='private-games-', dir=out) as folder:
    game = HumanGame(root / 'build/regicide.exe', folder)
    try:
        for history, case in cases:
            board = chess.Board()
            for uci in history:
                board.push_uci(uci)
            seeds = {}
            for index in range(1000):
                seed = f'{index:032x}'
                plan = variety.plan(board, 1000, seed)
                seeds.setdefault(plan['candidate_uci'], seed)
                if len(seeds) == len(plan['candidates']):
                    break
            assert len(seeds) == 4, (case, seeds)
            for candidate, seed in sorted(seeds.items()):
                game.new_game('black' if not history else 'white', 1000, game.revision)
                game._opening_seed = seed
                for uci in history:
                    game.human_move(uci, game.revision)
                before = game.board.copy(stack=True)
                started = time.perf_counter()
                result = game.engine_move(game.revision)
                elapsed = round((time.perf_counter() - started) * 1000, 2)
                assert not result['error'], result['error']
                actual = game.board.peek()
                assert actual in before.legal_moves
                decision = game._opening_decisions[len(game.board.move_stack)]
                assert decision['candidate_uci'] == candidate
                assert decision['selected_uci'] == actual.uci()
                assert decision['baseline_budget_ms'] + decision['candidate_budget_ms'] <= 1000
                if decision['reason'] == 'accepted_alternative':
                    assert decision['baseline']['depth'] == decision['candidate_compared']['depth'] >= 3
                    assert decision['loss_cp'] <= 35
                assert game.evaluation()['fen'] == game.board.fen(en_passant='fen')
                saved = json.loads((Path(folder) / (game.game_id + '.json')).read_text(encoding='utf-8'))
                assert saved['opening_variety']['seed'] == seed
                exported = GameArchive(folder).get_game(game.game_id)['pgn']
                assert f'[OpeningVarietySeed "{seed}"]' in exported
                row = {'case': case, 'seed': seed, 'selected_san': before.san(actual),
                       'wall_ms': elapsed, 'decision': decision}
                records.append(row)
                print(json.dumps({'case': case, 'candidate': candidate,
                                  'selected': actual.uci(), 'reason': decision['reason'],
                                  'loss_cp': decision['loss_cp'], 'depth': decision['compared_depth']}), flush=True)
                if history == ['e2e4']:
                    game.undo(game.revision)
                    game.human_move('e2e4', game.revision)
                    repeated = game.engine_move(game.revision)
                    assert not repeated['error']
                    assert game.board.peek() == actual
                    assert game._opening_decisions[2]['reused'] is True
        game.new_game('black', 100, game.revision)
        result = game.engine_move(game.revision)
        assert not result['error']
        short = game._opening_decisions[1]
        assert short['reason'] == 'short_budget' and short['baseline_budget_ms'] == 100
    finally:
        game.close()

counts = {case: dict(Counter(r['selected_san'] for r in records if r['case'] == case))
          for _, case in cases}
assert len(counts['white_first']) >= 3, counts
assert len(counts['black_after_e4']) >= 3, counts
summary = {'binary_sha256': hashlib.sha256((root / 'build/regicide.exe').read_bytes()).hexdigest(),
           'policy': variety.POLICY, 'budget_ms': 1000, 'selection_cases': len(records),
           'seed_sampling': 'One deterministic seed per proposed opening, not a frequency survey',
           'choices': counts, 'e4_reply_undo_checks': 4, 'short_budget_fallback': True,
           'pgn_headers': 'validated for all saved and archive-exported cases',
           'user_games_touched': False, 'records': records}
(out / 'live-evidence.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
print(json.dumps({k: v for k, v in summary.items() if k != 'records'}), flush=True)
