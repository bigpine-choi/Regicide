"""Read original fixed-node evidence only; never launch an engine or a match."""
from collections import Counter
import datetime
import hashlib
import io
import json
import math
from pathlib import Path
import sys

import chess
import chess.pgn

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from collect_learning_data import OPTIONS

RUN = ROOT / 'build/learning-cycle/runs/000006-39915e221a26'
OUT = Path(__file__).with_name('development-audit-study6.json')
BINARY = 'e2b218e7b22f3f4f166d999f011ed08cb66120cc6a6f3265448537d72b8254e3'
EXPECTED = {0: (4, 19, 9), 128: (4, 19, 9), 512: (3, 24, 5), 1024: (2, 14, 16)}
checks = Counter()
frozen = {}

def require(condition, text):
    checks['conditions'] += 1
    if not condition:
        raise AssertionError(text)

def sha(path):
    path = Path(path).resolve()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if path in frozen:
        require(frozen[path] == digest, 'input changed during read: ' + str(path))
    else:
        frozen[path] = digest
    return digest

def read_json(path):
    sha(path)
    return json.loads(Path(path).read_text(encoding='utf-8'))

def actual_replay(initial, moves):
    board = chess.Board(initial)
    require(board.is_valid(), 'invalid initial position')
    for text in moves:
        require(board.outcome(claim_draw=True) is None, 'actual history continues after rule ending')
        move = chess.Move.from_uci(text)
        require(move in board.legal_moves, 'illegal actual move')
        board.push(move)
    return board

def audit_arm(count, model, starts):
    directory = RUN / f'development-{count:06d}'
    metadata = read_json(directory / 'match.json')
    require(metadata['schema'] == 'regicide.endgame.development-match.v1' and
            metadata['status'] == 'complete' and metadata['errors'] == [] and metadata['completed'] is True and
            metadata['valid_for_comparison'] is True, 'incomplete or invalid development metadata')
    require(metadata['resource_mode'] == 'fixed_nodes' and metadata['nodes_per_move'] == 1000 and
            metadata['movetime_ms'] is None and metadata['move_response_allowance_seconds'] is None and
            metadata['hash_mb'] == 16 and metadata['threads'] == 1 and metadata['draw_policy'] == 'claim',
            'fixed work, hash, thread or draw settings differ')
    require(metadata['resource_guard']['required'] is False and metadata['resource_guard']['performed'] is False and
            metadata['resource_samples_sha256'] is None, 'fixed-node game makes an unsupported idle-host claim')
    require(metadata['binary_sha256'] == BINARY and metadata['model_sha256'] == model['model_sha256'],
            'model or native binary identity differs')
    for name, digest in metadata['implementation_sha256'].items():
        require(sha(ROOT / 'scripts' / name) == digest, 'recorded development implementation bytes differ')
    require(sha(RUN / 'curve' / model['model_path']) == model['model_sha256'] == sha(directory / 'candidate.nn'),
            'fitted model and actual match snapshot differ')
    require(metadata['native_model_validation']['model_sha256'] == model['model_sha256'] and
            type(metadata['native_model_validation']['score_cp']) is int, 'native parser evidence differs')
    require((directory / 'openings.json').read_bytes() == (RUN / 'development-starts.json').read_bytes(),
            'copied development starts differ')
    sha(directory / 'openings.json')
    require(metadata['opening_data']['document'] == starts and metadata['opening_data']['sha256'] == sha(RUN / 'development-starts.json'),
            'development opening document/hash differs')
    openings = metadata['opening_data']['openings']
    require(len(openings) == 16 and metadata['requested_pairs'] == 16 and len(metadata['games']) == 32,
            'development starts or pair/game count differ')
    for actual, declared in zip(openings, starts['positions']):
        require(actual['name'] == declared['name'] and actual['fen'] == declared['fen'] and actual['moves'] == declared['moves'],
                'starting family, FEN or declared history differs')
        board = actual_replay(actual['fen'], actual['moves'])
        require(board.fen(en_passant='fen') == actual['fen_after_opening'], 'opening FEN disagrees with history')
    require(set(metadata['engines']) == {'baseline', 'candidate'}, 'wrong engine role set')
    for role, neural in (('baseline', False), ('candidate', True)):
        identity = metadata['engines'][role]
        expected_options = dict(OPTIONS, UseNeuralEvaluation=neural)
        if neural:
            expected_options['EvalFile'] = str((directory / 'candidate.nn').resolve())
        require(identity['options'] == expected_options and identity['binary_sha256'] == BINARY and
                identity['id']['name'] == 'Regicide 0.6.5' and
                identity['model_sha256'] == (model['model_sha256'] if neural else None),
                'actual role options, model or native version differ')
    pgn_path = directory / 'games.pgn'
    require(sha(pgn_path) == metadata['pgn_sha256'], 'original PGN hash differs')
    stream = io.StringIO(pgn_path.read_text(encoding='utf-8'))
    points, actual_moves, nodes, pv_moves = [], 0, 0, 0
    terminations = Counter()
    for index, game in enumerate(metadata['games']):
        pair = index // 2
        opening = openings[pair]
        candidate_white = index % 2 == 0
        white = 'candidate' if candidate_white else 'baseline'
        black = 'baseline' if candidate_white else 'candidate'
        require(game['game'] == index + 1 and game['pair'] == pair + 1 and
                game['white'] == white and game['black'] == black and
                game['opening'] == opening['name'] and game['initial_fen'] == opening['fen'] and
                game['opening_uci_moves'] == opening['moves'] and game['opening_fen'] == opening['fen_after_opening'] and
                game['uci_moves'][:len(opening['moves'])] == opening['moves'] and
                game['completed'] is True and game['error'] is None, 'pair schedule, color or start identity differs')
        board = chess.Board(game['initial_fen'])
        search_moves = game['search_moves']
        require(len(search_moves) == len(game['uci_moves']) - len(opening['moves']), 'actual searched history length differs')
        for ply, text in enumerate(game['uci_moves']):
            require(board.outcome(claim_draw=True) is None, 'actual game continued after a rule ending')
            if ply >= len(opening['moves']):
                item = search_moves[ply - len(opening['moves'])]
                role = white if board.turn else black
                require(item['ply'] == ply + 1 and item['role'] == role and
                        item['color'] == ('white' if board.turn else 'black') and item['move'] == text and
                        item['fen_before'] == board.fen(en_passant='fen'), 'search role, side, full-history FEN or move differs')
                require(type(item['nodes']) is int and 1 <= item['nodes'] <= 1000, 'search exceeds its 1000-node maximum')
                require(type(item['elapsed_seconds']) in (float, int) and math.isfinite(item['elapsed_seconds']) and
                        item['elapsed_seconds'] >= 0, 'invalid informational elapsed time')
                require(item['pv'] and item['pv'][0] == text, 'selected bestmove differs from recorded PV')
                pv = board.copy(stack=True)
                for uci in item['pv']:
                    require(pv.outcome(claim_draw=False) is None, 'PV continued after automatic ending')
                    move = chess.Move.from_uci(uci)
                    require(move in pv.legal_moves, 'illegal PV')
                    pv.push(move)
                    pv_moves += 1
                nodes += item['nodes']
            move = chess.Move.from_uci(text)
            require(move in board.legal_moves, 'illegal actual game history')
            board.push(move)
            actual_moves += 1
        outcome = board.outcome(claim_draw=True)
        require(outcome is not None and outcome.result() == game['result'] and
                outcome.termination.name.lower() == game['termination'] and
                board.fen(en_passant='fen') == game['final_fen'] and len(game['uci_moves']) == game['plies'],
                'actual final FEN, rule termination, result or move count differs')
        score = .5 if outcome.winner is None else float(outcome.winner == candidate_white)
        require(game['candidate_points'] == score, 'candidate score disagrees with actual chess outcome')
        points.append(score)
        terminations[game['termination']] += 1
        for role in ('baseline', 'candidate'):
            items = [item for item in search_moves if item['role'] == role]
            expected_resources = {'searched_moves': len(items), 'reported_nodes': sum(item['nodes'] for item in items),
                                  'moves_without_node_report': 0,
                                  'elapsed_seconds': round(sum(item['elapsed_seconds'] for item in items), 6)}
            require(game['resources'][role] == expected_resources, 'role work total differs from per-move evidence')
        parsed = chess.pgn.read_game(stream)
        require(parsed is not None and not parsed.errors, 'missing or invalid original PGN game')
        require(parsed.board().fen(en_passant='fen') == game['initial_fen'] and
                [move.uci() for move in parsed.mainline_moves()] == game['uci_moves'] and
                parsed.end().board().fen(en_passant='fen') == game['final_fen'] and
                parsed.headers['Result'] == game['result'] and parsed.headers['White'] == white and parsed.headers['Black'] == black,
                'original PGN differs from recorded full game')
        require(parsed.headers['SearchBudgetMode'] == 'fixed_nodes' and parsed.headers['RequestedNodes'] == '1000' and
                parsed.headers['BinarySHA256'] == BINARY and parsed.headers['CandidateModelSHA256'] == model['model_sha256'],
                'original PGN budget or identity headers differ')
    require(chess.pgn.read_game(stream) is None, 'unregistered extra PGN game')
    wins, draws, losses = points.count(1), points.count(.5), points.count(0)
    require((wins, draws, losses) == EXPECTED[count], 'actual W/D/L differs from reported target')
    summary = metadata['summary']
    require((summary['candidate_wins'], summary['candidate_draws'], summary['candidate_losses']) == (wins, draws, losses) and
            summary['candidate_points'] == sum(points) and summary['candidate_score'] == sum(points) / 32 and
            summary['pair_scores'] == [(points[i] + points[i+1]) / 2 for i in range(0, 32, 2)],
            'summary differs from independently replayed games')
    checks['actual_game_plies'] += actual_moves
    checks['actual_pv_plies'] += pv_moves
    checks['games'] += 32
    return {'added_train': count, 'candidate_wins': wins, 'candidate_draws': draws, 'candidate_losses': losses,
            'candidate_score': sum(points) / 32, 'actual_game_plies': actual_moves,
            'actual_pv_plies': pv_moves, 'recorded_search_nodes': nodes,
            'terminations': dict(terminations), 'model_sha256': model['model_sha256']}

def main():
    require(sha(RUN / 'regicide.exe') == BINARY and sha(ROOT / 'build-neural-v5/regicide.exe') == BINARY,
            'original and study-snapshot executable bytes differ')
    curve = read_json(RUN / 'curve/report.json')
    starts = read_json(RUN / 'development-starts.json')
    require(starts['schema'] == 'regicide.endgame.development.openings.v1' and len(starts['positions']) == 16,
            'wrong declared starting distribution')
    results = []
    require([arm['added_train'] for arm in curve['arms']] == list(EXPECTED), 'arm ordering differs')
    for arm in curve['arms']:
        model = arm['runs'][0]
        require(model['seed'] == curve['training_seeds'][0] == 2026101901, 'different training seed entered development')
        results.append(audit_arm(arm['added_train'], model, starts))
    require(curve['candidate']['added_train'] == 1024 and
            curve['candidate']['model_sha256'] == results[-1]['model_sha256'], 'primary fixed candidate differs')
    for path, digest in frozen.items():
        require(hashlib.sha256(path.read_bytes()).hexdigest() == digest, 'audited source changed: ' + str(path))
    report = {'status': 'passed', 'finished_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'scope': 'Independent replay of original fixed-node synthetic-endgame evidence only; browser/CPU wall time uncontrolled; no equal-time strength or Elo claim; cannot authorize adoption.',
              'binary_sha256': BINARY, 'source_run': RUN.relative_to(ROOT).as_posix(),
              'same_starts': 16, 'pairs_per_arm': 16, 'nodes_per_move_maximum': 1000,
              'arms': results, 'checks': dict(checks), 'inputs': len(frozen),
              'input_sha256': {path.relative_to(ROOT).as_posix(): digest for path, digest in frozen.items()}}
    OUT.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'status': 'passed', 'checks': dict(checks), 'arms': results}))

if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        OUT.write_text(json.dumps({'status': 'failed', 'error_type': type(error).__name__, 'error': str(error),
                                  'checks': dict(checks)}, indent=2) + '\n', encoding='utf-8')
        raise
