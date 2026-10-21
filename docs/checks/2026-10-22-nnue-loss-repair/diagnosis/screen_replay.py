import sys, json, hashlib, io
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / 'scripts'))
import chess
import chess.pgn
import neural_stm as stm

OLD = ROOT / 'docs/checks/2026-10-22-nnue-diversity/attempt-001'
OUT = Path(__file__).parent
r = json.loads((OLD / 'games.json').read_text())
model = stm.load_model(OLD / 'model.nnue')
values = {1: 100, 2: 320, 3: 330, 4: 500, 5: 900, 6: 0}

def material(b):
    return sum(values[p.piece_type] * (1 if p.color else -1) for p in b.piece_map().values())

def score(b, sign):
    value = stm.quantized_white(b, model)
    return {'fen': b.fen(en_passant='fen'), 'nnue_white_cp': value,
            'nnue_candidate_cp': sign * value, 'material_white_cp': material(b),
            'material_candidate_cp': sign * material(b), 'piece_count': len(b.piece_map()),
            'in_check': b.is_check()}

pgn = io.StringIO((OLD / 'games.pgn').read_text())
pgn_games = []
while True:
    game = chess.pgn.read_game(pgn)
    if game is None:
        break
    assert not game.errors, game.errors
    pgn_games.append(game)
losses, pvbad, legalmoves = [], [], 0
for gi, g in enumerate(r['games']):
    board = chess.Board(g['initial_fen'])
    boards = [board.copy()]
    searches = {s['ply']: s for s in g['search_moves']}
    assert len(searches) + len(g['opening_uci_moves']) == len(g['uci_moves'])
    for ply, uci in enumerate(g['uci_moves'], 1):
        move = chess.Move.from_uci(uci)
        assert move in board.legal_moves
        if ply in searches:
            assert searches[ply]['fen_before'] == board.fen(en_passant='fen'), (g['game'], ply, 'fen')
            assert searches[ply]['move'] == uci
        board.push(move)
        boards.append(board.copy())
        legalmoves += 1
    assert board.fen(en_passant='fen') == g['final_fen']
    assert [m.uci() for m in pgn_games[gi].mainline_moves()] == g['uci_moves']
    sign = 1 if g['white'] == 'candidate' else -1
    if g['result'] != ('0-1' if sign == 1 else '1-0'):
        continue
    candidates = []
    for ply, s in searches.items():
        if s['role'] != 'candidate':
            continue
        cp = s['score'].get('cp')
        if cp is None or cp < -800 or ply > 120:
            continue
        before, after = boards[ply - 1], boards[ply]
        future = boards[min(ply + 5, len(boards) - 1)]
        reply = searches.get(ply + 1)
        other = -reply['score']['cp'] if reply and reply['score'].get('cp') is not None else None
        drift = sign * (material(future) - material(before))
        disagree = cp - other if other is not None else 0
        leaf, pvlegal = before.copy(), True
        for uci in s.get('pv', []):
            move = chess.Move.from_uci(uci)
            if move not in leaf.legal_moves:
                pvlegal = False
                pvbad.append([g['game'], ply, uci])
                break
            leaf.push(move)
        leaf_score = score(leaf, sign) if pvlegal else None
        leaf_over = max(0, leaf_score['nnue_candidate_cp'] - leaf_score['material_candidate_cp']) if leaf_score else 0
        priority = max(0, disagree) + max(0, -drift) + .5 * leaf_over - 2 * max(0, ply - 60) - max(0, -cp - 200)
        candidates.append({'game': g['game'], 'opening': g['opening'],
            'candidate_color': 'white' if sign == 1 else 'black', 'candidate_sign': sign,
            'ply': ply, 'move': s['move'], 'depth': s.get('depth'),
            'recorded_search_candidate_cp': cp, 'recorded_next_baseline_as_candidate_cp': other,
            'search_disagreement_cp': disagree, 'six_ply_material_change_candidate_cp': drift,
            'before': score(before, sign), 'after_played': score(after, sign),
            'after_reply': score(boards[min(ply + 1, len(boards) - 1)], sign),
            'six_plies_later': score(future, sign), 'pv': s.get('pv', []),
            'pv_leaf': leaf_score, 'priority': priority})
    chosen = []
    for c in sorted(candidates, key=lambda x: (-x['priority'], x['ply'])):
        if all(abs(c['ply'] - p['ply']) >= 6 for p in chosen):
            chosen.append(c)
        if len(chosen) == 3:
            break
    losses.append({'game': g['game'], 'opening': g['opening'],
        'candidate_color': 'white' if sign == 1 else 'black', 'result': g['result'],
        'plies': g['plies'], 'candidates': sorted(chosen, key=lambda x: x['ply'])})

result = {'source_games_sha256': hashlib.sha256((OLD / 'games.json').read_bytes()).hexdigest(),
    'source_model_sha256': hashlib.sha256((OLD / 'model.nnue').read_bytes()).hexdigest(),
    'method': 'Legal game and PGN replay, original search FEN/move binding, unchanged integer STM static scores. Material P100/N320/B330/R500/Q900. Screen only: material and opponent search are not teacher truth. Top3 spaced>=6plies per loss, ply<=120 and recorded candidate search cp>=-800. No engine, teacher, training or games executed.',
    'games_replayed': len(r['games']), 'pgn_games_replayed': len(pgn_games),
    'legal_moves_replayed': legalmoves, 'candidate_losses': len(losses), 'pv_illegal': pvbad,
    'losses': losses, 'resources': r['summary']['resources']}
(OUT / 'loss-screen.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
for g in losses:
    print('GAME', g['game'], g['opening'], g['candidate_color'])
    for c in g['candidates']:
        p = c['pv_leaf']
        print(c['ply'], c['move'], 'search', c['recorded_search_candidate_cp'], 'other',
              c['recorded_next_baseline_as_candidate_cp'], 'material6',
              c['six_ply_material_change_candidate_cp'], 'PV static/material',
              p['nnue_candidate_cp'] if p else None, p['material_candidate_cp'] if p else None)
print('REPLAY', len(r['games']), legalmoves, 'PVbad', len(pvbad))
