"""Read-only replay of the matched MSE/H100 16-game comparison; no engine invocation."""
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys
import time

import chess
import chess.pgn


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def audit(directory):
    plan = json.loads((directory / 'plan.json').read_text(encoding='utf-8'))
    match = json.loads((directory / 'games.json').read_text(encoding='utf-8'))
    checked_inputs = {}
    for name, expected in plan['inputs'].items():
        actual = sha(name)
        require(actual == expected, 'Current preregistered input differs: ' + name)
        checked_inputs[name] = actual
    require(datetime.fromisoformat(plan['created_utc']) < datetime.fromisoformat(match['started_at']),
            'Plan was not registered before match start')
    require(plan['loss_only_strength_comparison'] is True, 'Expected loss-only matched comparison')
    pair_binding = plan['loss_pair_verification']
    pair_path = Path(pair_binding['path'])
    require(sha(pair_path) == pair_binding['sha256'], 'Training-pair receipt differs')
    pair = json.loads(pair_path.read_text(encoding='utf-8'))
    require(pair['status'] == 'PASS' and pair['loss_only_training_comparison'] is True
            and pair['training_invoked'] is False and pair['engine_invoked'] is False,
            'Training-pair verification failed or retraining was used')
    require(pair['baseline_id'] == 'B-mse-20261020' and pair['candidate_id'] == 'B-h100-20261020'
            and pair['shared_config'] == {'batch_size': 128, 'epochs': 120, 'input_bias_lr': 0.00002,
                                        'input_scale': 32, 'output_lr': 0.005, 'seed': 20261020}
            and pair['model_header'] == 'REGICIDE_NNUE 2 768 16 4064 256 32',
            'Training-pair conditions differ')
    require(pair['initial_sha256'] == '5ce9ca28e3436b125db9b307944d28c1fe7e58bbd7415048ec986aa570d4bd05'
            and pair['batch_order_sha256'] == '82aa541252591983612b19d383f40feb3d00a41782ff835828ecbc3f6d0ac967'
            and pair['partitions']['train']['rows'] == 2209 and pair['partitions']['validation']['rows'] == 723,
            'Fixed initial/batch/data conditions differ')
    require(datetime.fromisoformat(pair['created_utc']) <= datetime.fromisoformat(plan['created_utc']),
            'Training-pair verification was not recorded before plan')
    verifier = pair_path.parent / 'verify_loss_pair.py'
    require(sha(verifier) == pair['verifier_source_sha256'], 'Training-pair verifier changed')
    for name, expected in pair['checked_inputs'].items():
        require(sha(name) == expected, 'Training-pair input changed: ' + name)
    require(plan['scheduled_games'] == match['games_requested'] == len(match['games']) == 16,
            'Expected exactly 16 games')
    require(match['completed'] is True and match['exit_code'] == 0 and match['errors'] == [],
            'Match has errors or incomplete status')
    require(match['limits'] == {'nodes': None, 'depth': None, 'movetime_ms': 100}
            and plan['movetime_ms'] == 100, 'Time control differs')
    require(match['draw_policy'] == plan['draw_policy'] == 'claim'
            and match['max_plies'] == plan['max_plies'] == 500, 'Ending policy differs')

    command = plan['command']
    def argument(flag):
        return command[command.index(flag) + 1]
    require(argument('--movetime') == '100' and argument('--opening-count') == '8'
            and argument('--draw-policy') == 'claim' and argument('--max-plies') == '500',
            'Registered command conditions differ')
    baseline, candidate = (match['engines'][role] for role in ('baseline', 'candidate'))
    require(baseline['command'] == candidate['command'], 'Binaries/arguments differ')
    binary = Path(baseline['command'][0])
    require(baseline['command'] == [str(binary), 'uci'], 'Unexpected engine command')
    require(baseline['sha256'] == candidate['sha256'] == sha(binary) == plan['inputs'][str(binary)],
            'Binary SHA mismatch')
    options = {}
    for role, engine in (('baseline', baseline), ('candidate', candidate)):
        options[role] = engine['requested_options']
        require(options[role] == engine['validated_options'] == json.loads(argument('--' + role + '-options')),
                'Requested/validated/registered options differ: ' + role)
        require(engine['provided_source_commit'] == argument('--' + role + '-commit')
                and engine['source_commit_object_verified'] is True, 'Declared source binding differs')
    changed = [key for key in set(options['baseline']) | set(options['candidate'])
               if options['baseline'].get(key) != options['candidate'].get(key)]
    require(changed == ['NeuralModelFile'], 'An engine option besides model differs')
    require(options['baseline']['Hash'] == plan['hash_mb'] == 16
            and options['baseline']['UseNNUEEvaluation'] is True
            and options['baseline']['UseNeuralEvaluation'] is False
            and options['baseline']['UseIncrementalNeuralEvaluation'] is True,
            'NNUE options differ')
    model_hashes = {role: sha(option['NeuralModelFile']) for role, option in options.items()}
    require(model_hashes == {
        'baseline': '267f65646c24292efa28f11a5f5990311ed7da4f6c134d7c4a6b6c1e0f7be1eb',
        'candidate': '9610be208a160ca60d593a4b35d3bd92d201b146311b65e2e68e8e6b79897210'},
        'Actual model file differs from fixed baseline/candidate')

    require(model_hashes == pair['model_sha256'], 'Compared models differ from verified training pair')

    opening_file = Path(match['opening_source'])
    require(str(opening_file) == argument('--openings')
            and sha(opening_file) == match['opening_file_sha256'], 'Opening file identity differs')
    openings = json.loads(opening_file.read_text(encoding='utf-8'))['openings'][:8]
    require(len(openings) == len(match['selected_openings']) == 8, 'Expected eight starts')
    for source, saved in zip(openings, match['selected_openings'], strict=True):
        require(source['name'] == saved['name'] and source['moves'] == saved['moves'],
                'Opening selection/order differs')

    with (directory / 'games.pgn').open(encoding='utf-8') as stream:
        pgns = []
        while (game := chess.pgn.read_game(stream)) is not None:
            require(not game.errors, 'PGN parser errors')
            pgns.append(game)
    require(len(pgns) == 16, 'PGN must contain exactly 16 games')
    wdl, terminations = Counter(), Counter()
    totals = Counter()
    game_receipts = []
    for number, (record, pgn) in enumerate(zip(match['games'], pgns, strict=True), 1):
        source = openings[(number - 1) // 2]
        pair = (number + 1) // 2
        white, black = ('candidate', 'baseline') if number % 2 else ('baseline', 'candidate')
        require(record['game'] == number and record['pair'] == pair
                and record['white'] == white and record['black'] == black,
                f'Game {number}: schedule/color differs')
        require(record['opening'] == source['name'] and record['opening_uci_moves'] == source['moves']
                and record['uci_moves'][:len(source['moves'])] == source['moves'],
                f'Game {number}: opening differs')
        require(record['completed'] is True and record['error'] is None
                and record['initial_fen'] == chess.STARTING_FEN, f'Game {number}: incomplete/start differs')
        board = chess.Board()
        for text in source['moves']:
            move = chess.Move.from_uci(text)
            require(board.outcome(claim_draw=True) is None and move in board.legal_moves,
                    f'Game {number}: illegal/terminal opening')
            board.push(move)
        require(board.fen(en_passant='fen') == record['opening_fen'], f'Game {number}: opening FEN differs')
        require(len(record['search_moves']) + len(source['moves']) == len(record['uci_moves']) == record['plies'],
                f'Game {number}: move/search counts differ')
        for offset, search in enumerate(record['search_moves'], len(source['moves'])):
            require(board.outcome(claim_draw=True) is None, f'Game {number}: searched after rule ending')
            require(search['ply'] == offset + 1 and search['fen_before'] == board.fen(en_passant='fen')
                    and search['move'] == record['uci_moves'][offset], f'Game {number}: search alignment differs')
            require(search['color'] == ('white' if board.turn else 'black')
                    and search['role'] == (white if board.turn else black), f'Game {number}: search role differs')
            require(math.isfinite(search['elapsed_seconds']) and search['elapsed_seconds'] >= 0,
                    f'Game {number}: invalid recorded response duration')
            pv_board = board.copy(stack=False)
            require(isinstance(search['pv'], list) and len(search['pv']) > 0,
                    f'Game {number}: missing recorded PV')
            for text in search['pv']:
                move = chess.Move.from_uci(text)
                require(move in pv_board.legal_moves, f'Game {number}: illegal recorded PV')
                pv_board.push(move)
                totals['pv_plies'] += 1
            totals['pv_first_moves_matching_played'] += int(search['pv'][0] == search['move'])
            move = chess.Move.from_uci(search['move'])
            require(move in board.legal_moves, f'Game {number}: illegal mainline')
            board.push(move)
            totals['searched_plies'] += 1
        outcome = board.outcome(claim_draw=True)
        require(outcome is not None and outcome.result() == record['result']
                and outcome.termination.name.lower() == record['termination'],
                f'Game {number}: rule ending/result differs')
        require(board.fen(en_passant='fen') == record['final_fen'], f'Game {number}: final FEN differs')
        points = .5 if outcome.winner is None else float((outcome.winner == chess.WHITE) == (white == 'candidate'))
        require(points == record['candidate_points'], f'Game {number}: candidate points differ')
        wdl[{1.: 'wins', .5: 'draws', 0.: 'losses'}[points]] += 1
        terminations[record['termination']] += 1
        require([move.uci() for move in pgn.mainline_moves()] == record['uci_moves']
                and pgn.end().board().fen(en_passant='fen') == record['final_fen'],
                f'Game {number}: PGN/mainline/final FEN differs')
        headers = pgn.headers
        require(headers['Result'] == record['result'] and headers['Round'] == str(number)
                and headers['Pair'] == str(pair) and headers['Opening'] == source['name']
                and headers['White'].startswith(white + ':') and headers['Black'].startswith(black + ':')
                and headers['DrawPolicy'] == 'claim' and headers['RequestedMoveTimeMS'] == '100'
                and headers['Termination'] == 'normal' and headers['TerminationReason'] == record['termination']
                and headers['WhiteSHA256'] == headers['BlackSHA256'] == sha(binary),
                f'Game {number}: PGN headers differ')
        totals['mainline_plies'] += record['plies']
        game_receipts.append({'game': number, 'pair': pair, 'candidate_color': 'white' if white == 'candidate' else 'black',
                              'result': record['result'], 'termination': record['termination'], 'plies': record['plies'],
                              'final_fen': record['final_fen']})
    summary = match['summary']
    for label in ('wins', 'draws', 'losses'):
        require(summary['candidate_' + label] == wdl[label], 'Summary WDL differs')
    points = wdl['wins'] + .5 * wdl['draws']
    require(summary['candidate_points'] == points and summary['candidate_score'] == points / 16
            and summary['completed_games'] == 16 and summary['incomplete_games'] == summary['unplayed_games'] == 0,
            'Summary score/completion differs')
    return {'status': 'PASS', 'verified_input_count': len(checked_inputs), 'verified_inputs': checked_inputs,
            'games': 16, 'paired_starts': 8, 'candidate_white_games': 8, 'candidate_black_games': 8,
            'requested_movetime_ms': 100, 'same_binary_sha256': sha(binary), 'model_sha256': model_hashes,
            'loss_pair_verification': pair_binding, 'loss_only_training_comparison': True,
            'option_difference': changed, 'totals': dict(totals), 'terminations': dict(terminations),
            'candidate_wdl': dict(wdl), 'candidate_points': points, 'candidate_score': points / 16,
            'reported_match_elapsed_seconds': match['total_elapsed_seconds'], 'game_receipts': game_receipts,
            'limitations': ['Recorded option acceptance is not independent UCI state readback.',
                            'Binary source is bound by hashes and prior build receipts, not rebuilt by this audit.',
                            'Same training data, architecture, initialization, seed and optimizer settings; one prepared MSE/H100 seed pair only.',
                            'Fixed exposed development openings and 16 games support a short descriptive screen, not Elo or promotion.',
                            'No process CPU accounting, isolation, system-load measurement or timeout re-execution is performed.']}


if __name__ == '__main__':
    directory = Path(sys.argv[1]).resolve()
    started = time.perf_counter()
    output = {'schema_version': 1, 'auditor': 'Independent direct python-chess replay; no match-runner imports or engine invocation',
              'created_utc': datetime.now(timezone.utc).isoformat(), 'auditor_source_sha256': sha(__file__),
              'raw_sha256': {name: sha(directory / name) for name in ('plan.json', 'games.json', 'games.pgn')}}
    try:
        output.update(audit(directory))
    except Exception as exc:
        output.update(status='FAIL', error=f'{type(exc).__name__}: {exc}')
    output['audit_elapsed_seconds'] = time.perf_counter() - started
    with (directory / 'peer-audit.json').open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(output, stream, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        stream.write('\n')
    print(json.dumps({key: output[key] for key in ('status', 'audit_elapsed_seconds')}
                     | {key: output[key] for key in ('games', 'totals', 'candidate_wdl', 'verified_input_count', 'error') if key in output}))
    sys.exit(0 if output['status'] == 'PASS' else 1)
