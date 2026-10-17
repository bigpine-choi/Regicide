import datetime
import hashlib
import json
from pathlib import Path
import chess
import chess.engine

ROOT = next(p for p in Path(__file__).resolve().parents if (p / 'CMakeLists.txt').exists())
OUT = Path(__file__).resolve().parent
SOURCE = ROOT / 'docs/matches/2026-10-17-hash-memory/time100-retry.json'
BINARY = OUT / 'stockfish/extracted/usr/games/stockfish'
COMMAND = ['wsl', '-d', 'Ubuntu-24.04', '--', '/mnt/c/' + BINARY.as_posix()[3:]]
games = json.loads(SOURCE.read_text(encoding='utf-8'))['games']
losses = [g for g in games if g['completed'] and g['candidate_points'] == 0]
document = {'started_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'purpose': 'external diagnostic reference only; never Regicide move selection',
            'source_sha256': hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
            'reference_binary_sha256': hashlib.sha256(BINARY.read_bytes()).hexdigest(),
            'nodes_per_position': 50000, 'deep_nodes': 500000,
            'configured_options': {'Threads': 1, 'Hash': 32},
            'limitations': 'Reference scores are finite-search Stockfish 16 judgments, not ground truth or Regicide cp calibration.',
            'loss_games': [g['game'] for g in losses], 'records': [], 'deep_records': []}

def save():
    (OUT / 'coach-analysis.json').write_text(json.dumps(document, indent=2) + '\n', encoding='utf-8')

def analyze(engine, board, color, nodes, root_moves=None):
    info = engine.analyse(board, chess.engine.Limit(nodes=nodes), game=object(), root_moves=root_moves)
    score = info['score'].pov(color)
    check = board.copy()
    pv = []
    for move in info.get('pv', []):
        assert move in check.legal_moves, (board.fen(), move)
        pv.append(move.uci())
        check.push(move)
    return {'cp': score.score(), 'mate': score.mate(), 'numeric': score.score(mate_score=100000),
            'nodes': info.get('nodes'), 'depth': info.get('depth'), 'pv': pv}

with chess.engine.SimpleEngine.popen_uci(COMMAND, timeout=20) as engine:
    engine.configure(document['configured_options'])
    document['reference_id'] = dict(engine.id)
    for game in losses:
        board = chess.Board()
        candidate_color = game['white'] == 'candidate'
        saved = {m['ply']: m for m in game['search_moves']}
        for ply, text in enumerate(game['uci_moves'], 1):
            move = chess.Move.from_uci(text)
            if ply in saved and saved[ply]['role'] == 'candidate':
                san = board.san(move)
                before = analyze(engine, board, candidate_color, 50000)
                record = {'game': game['game'], 'ply': ply, 'fen': board.fen(en_passant='fen'),
                          'history': list(game['uci_moves'][:ply-1]), 'played': text, 'san': san,
                          'recorded_search': saved[ply], 'before': before}
                board.push(move)
                after = analyze(engine, board, candidate_color, 50000)
                record.update(after=after, drop=before['numeric'] - after['numeric'])
                document['records'].append(record)
            else:
                board.push(move)
        save()
        print('completed loss', game['game'], 'records', len(document['records']), flush=True)
    selected = []
    for game in losses:
        records = [r for r in document['records'] if r['game'] == game['game'] and r['before']['numeric'] > -450]
        # Earliest substantial errors and largest drops, not late forced losses.
        early = [r for r in records if r['drop'] >= 100][:2]
        worst = sorted(records, key=lambda r: r['drop'], reverse=True)[:2]
        seen = set()
        for record in early + worst:
            if record['ply'] not in seen:
                selected.append(record)
                seen.add(record['ply'])
    for record in selected:
        board = chess.Board()
        for text in record['history']:
            board.push_uci(text)
        color = board.turn
        best = analyze(engine, board, color, 500000)
        forced = analyze(engine, board, color, 500000, [chess.Move.from_uci(record['played'])])
        item = {'game': record['game'], 'ply': record['ply'], 'fen': record['fen'],
                'played': record['played'], 'san': record['san'], 'best': best,
                'forced_played': forced, 'drop': best['numeric'] - forced['numeric']}
        document['deep_records'].append(item)
        save()
        print('deep', item['game'], item['ply'], item['san'], 'drop', item['drop'], 'best', best['pv'][:5], flush=True)
document['finished_utc'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
save()
