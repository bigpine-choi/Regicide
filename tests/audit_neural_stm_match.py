"""Read-only training binding and independent replay of the 16-game STM screen.

No engine invocation, training, runner import, model selection or extra games.
"""
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import time

import chess
import chess.pgn

ROOT = Path(__file__).resolve().parents[1]
BASELINE_SHA = '267f65646c24292efa28f11a5f5990311ed7da4f6c134d7c4a6b6c1e0f7be1eb'
INITIAL_SHA = '5ce9ca28e3436b125db9b307944d28c1fe7e58bbd7415048ec986aa570d4bd05'
FEATURE_SHA = '479fe32c161773c713edc59bd94a41606fecb4bd8066914a65c11e8adcb1cdc8'
ORDER_SHA = '82aa541252591983612b19d383f40feb3d00a41782ff835828ecbc3f6d0ac967'
SHARED_CONFIG = {'batch_size': 128, 'epochs': 120, 'input_bias_lr': .00002,
                 'input_scale': 32, 'loss': 'mse', 'output_lr': .005, 'seed': 20261020}


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def training_binding(directory, plan, candidate_path):
    protocol, trained, validation = (read(directory / name) for name in
                                    ('protocol.json', 'training.json', 'validation.json'))
    for receipt in ('protocol.json', 'training.json', 'validation.json', 'model.nnue'):
        path = directory / receipt
        require(str(path) in plan['inputs'] and sha(path) == plan['inputs'][str(path)],
                'Training/model receipt is not bound to preregistered match: ' + receipt)
    require(Path(candidate_path) == directory / 'model.nnue', 'Candidate does not use the new training output')
    require(protocol['config'] == trained['config']
            and {k: v for k, v in protocol['config'].items() if k != 'candidate_id'} == SHARED_CONFIG,
            'Candidate training conditions differ')
    require(protocol['config']['candidate_id'] == 'B-mse-stm-20261020'
            and protocol['baseline']['id'] == 'B-mse-20261020'
            and protocol['baseline']['sha256'] == BASELINE_SHA
            and protocol['baseline']['retrained'] is False and trained['baseline_retrained'] is False,
            'Expected one new STM candidate and the prepared MSE baseline')
    require(protocol['architecture']['added_trainable_parameters'] == 16
            and protocol['architecture']['turn_weight_initialization'] == 'all zero'
            and trained['common_initial_predictions_exact'] is True,
            'New feature initialization or architecture differs')
    data = protocol['data']
    require({name: data['rows'][name] for name in ('train', 'validation', 'test')}
            == {'train': 2209, 'validation': 723, 'test': 0}
            and data['initial_sha256'] == INITIAL_SHA and data['feature_sha256'] == FEATURE_SHA
            and data['batch_order_sha256'] == ORDER_SHA, 'Fixed training corpus/initial/order differs')
    require(trained['model_sha256'] == sha(candidate_path), 'Training report model SHA differs')
    require([entry['epoch'] for entry in trained['history']] == list(range(1, 121))
            and trained['history'][-1]['optimizer_steps'] == 120 * math.ceil(2209 / 128),
            'Expected exactly the fixed 120 final training epochs')
    for name, expected in protocol['input_sha256'].items():
        require(sha(name) == expected, 'Training source or data changed after training: ' + name)
    original = read(ROOT / 'docs/checks/2026-10-21-m3-huber-preparation/attempt-001/protocol.json')
    for name in ('manifest.json', 'train.jsonl', 'validation.jsonl'):
        path = Path(data['source']) / name
        require(sha(path) == original['input_sha256'][str(path)], 'Training data differs from prepared MSE: ' + name)
    require(validation['corpus_positions'] == 2932
            and validation['python_native_full_incremental_mismatches'] == 0
            and validation['legacy_old_new_python_mismatches'] == 0
            and validation['actual_trained_model_tests_returncode'] == 0
            and validation['same_placement_turn_fixtures'] > 0,
            'Trained-model or old-model compatibility validation failed')
    for name, expected in validation['input_sha256'].items():
        require(sha(name) == expected, 'Native validation input changed: ' + name)
    tokens = Path(candidate_path).read_text(encoding='ascii').split()
    require(len(tokens) == 7 + 16 * 3 + 768 * 16 + 2
            and tokens[:7] == ['REGICIDE_NNUE', '3', '769', '16', '4064', '256', '32']
            and tokens[-2] == 'checksum', 'Candidate v3 format differs')
    words = tokens[1:-2] + tokens[-1:]
    require(all(re.fullmatch(r'-?[0-9]{1,20}', word) for word in words), 'Invalid model integer')
    values = list(map(int, words))
    checksum = 14695981039346656037
    for value in values[:-1]:
        for byte in (value & 0xFFFFFFFF).to_bytes(4, 'little'):
            checksum = ((checksum ^ byte) * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    require(checksum == values[-1], 'Independent v3 checksum differs')
    turn = values[-17:-1]
    require(turn == trained['turn_weights_integer'] and any(turn)
            and all(-32767 <= value <= 32767 for value in turn), 'New turn weights differ or remain zero')
    return {'config': protocol['config'], 'added_trainable_parameters': 16,
            'common_initial_predictions_exact': True, 'baseline_retrained': False,
            'corpus_positions': 2932, 'model_checksum': checksum, 'turn_weights_integer': turn,
            'python_native_full_incremental_mismatches': 0, 'legacy_old_new_python_mismatches': 0}


def audit(directory):
    plan, match = read(directory / 'plan.json'), read(directory / 'games.json')
    checked_inputs = {}
    for name, expected in plan['inputs'].items():
        require(sha(name) == expected, 'Current preregistered input differs: ' + name)
        checked_inputs[name] = expected
    require(datetime.fromisoformat(plan['created_utc']) < datetime.fromisoformat(match['started_at']),
            'Plan was not registered before match start')
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
    require(baseline['command'] == candidate['command'], 'Binaries or arguments differ')
    binary = Path(baseline['command'][0])
    require(baseline['command'] == [str(binary), 'uci'], 'Unexpected engine command')
    require(baseline['sha256'] == candidate['sha256'] == sha(binary) == plan['inputs'][str(binary)],
            'Binary SHA differs')
    options = {}
    for role, engine in (('baseline', baseline), ('candidate', candidate)):
        options[role] = engine['requested_options']
        require(options[role] == engine['validated_options'] == json.loads(argument('--' + role + '-options')),
                'Requested, validated or registered options differ: ' + role)
        require(engine['provided_source_commit'] == argument('--' + role + '-commit')
                and engine['source_commit_object_verified'] is True, 'Declared source binding differs')
    changed = sorted(key for key in set(options['baseline']) | set(options['candidate'])
                     if options['baseline'].get(key) != options['candidate'].get(key))
    require(changed == ['NeuralModelFile'], 'An engine option besides model differs')
    require(options['baseline']['Hash'] == plan['hash_mb'] == 16
            and options['baseline']['UseNNUEEvaluation'] is True
            and options['baseline']['UseNeuralEvaluation'] is False
            and options['baseline']['UseIncrementalNeuralEvaluation'] is True, 'NNUE options differ')
    model_hashes = {role: sha(option['NeuralModelFile']) for role, option in options.items()}
    require(model_hashes['baseline'] == BASELINE_SHA, 'Baseline is not the prepared matched MSE model')
    require(model_hashes == plan['model_sha256'], 'Actual model SHA differs from the registered pair')
    training = training_binding(directory, plan, options['candidate']['NeuralModelFile'])
    opening_file = Path(match['opening_source'])
    require(str(opening_file) == argument('--openings')
            and sha(opening_file) == match['opening_file_sha256'], 'Opening file identity differs')
    openings = read(opening_file)['openings'][:8]
    require(len(openings) == len(match['selected_openings']) == 8, 'Expected eight starts')
    for source, saved in zip(openings, match['selected_openings'], strict=True):
        require(source['name'] == saved['name'] and source['moves'] == saved['moves'], 'Opening order differs')
    with (directory / 'games.pgn').open(encoding='utf-8') as stream:
        pgns = []
        while (game := chess.pgn.read_game(stream)) is not None:
            require(not game.errors, 'PGN parser errors')
            pgns.append(game)
    require(len(pgns) == 16, 'PGN must contain exactly 16 games')
    wdl, terminations, totals = Counter(), Counter(), Counter()
    game_receipts = []
    for number, (record, pgn) in enumerate(zip(match['games'], pgns, strict=True), 1):
        source, pair = openings[(number - 1) // 2], (number + 1) // 2
        white, black = ('candidate', 'baseline') if number % 2 else ('baseline', 'candidate')
        require(record['game'] == number and record['pair'] == pair
                and record['white'] == white and record['black'] == black, f'Game {number}: schedule differs')
        require(record['opening'] == source['name'] and record['opening_uci_moves'] == source['moves']
                and record['uci_moves'][:len(source['moves'])] == source['moves'], f'Game {number}: opening differs')
        require(record['completed'] is True and record['error'] is None
                and record['initial_fen'] == chess.STARTING_FEN, f'Game {number}: incomplete or wrong start')
        board = chess.Board()
        for text in source['moves']:
            move = chess.Move.from_uci(text)
            require(board.outcome(claim_draw=True) is None and move in board.legal_moves,
                    f'Game {number}: illegal or terminal opening')
            board.push(move)
        require(board.fen(en_passant='fen') == record['opening_fen'], f'Game {number}: opening FEN differs')
        require(len(record['search_moves']) + len(source['moves']) == len(record['uci_moves']) == record['plies'],
                f'Game {number}: move counts differ')
        for offset, search in enumerate(record['search_moves'], len(source['moves'])):
            require(board.outcome(claim_draw=True) is None, f'Game {number}: searched after rule ending')
            require(search['ply'] == offset + 1 and search['fen_before'] == board.fen(en_passant='fen')
                    and search['move'] == record['uci_moves'][offset], f'Game {number}: search alignment differs')
            require(search['color'] == ('white' if board.turn else 'black')
                    and search['role'] == (white if board.turn else black), f'Game {number}: search role differs')
            require(math.isfinite(search['elapsed_seconds']) and search['elapsed_seconds'] >= 0,
                    f'Game {number}: invalid response duration')
            require(isinstance(search['pv'], list) and search['pv'], f'Game {number}: missing PV')
            pv_board = board.copy(stack=False)
            for text in search['pv']:
                move = chess.Move.from_uci(text)
                require(move in pv_board.legal_moves, f'Game {number}: illegal PV')
                pv_board.push(move)
                totals['pv_plies'] += 1
            totals['pv_first_moves_matching_played'] += int(search['pv'][0] == search['move'])
            move = chess.Move.from_uci(search['move'])
            require(move in board.legal_moves, f'Game {number}: illegal mainline')
            board.push(move)
            totals['searched_plies'] += 1
        outcome = board.outcome(claim_draw=True)
        require(outcome is not None and outcome.result() == record['result']
                and outcome.termination.name.lower() == record['termination'], f'Game {number}: ending differs')
        require(board.fen(en_passant='fen') == record['final_fen'], f'Game {number}: final FEN differs')
        points = .5 if outcome.winner is None else float((outcome.winner == chess.WHITE) == (white == 'candidate'))
        require(points == record['candidate_points'], f'Game {number}: candidate points differ')
        wdl[{1.: 'wins', .5: 'draws', 0.: 'losses'}[points]] += 1
        terminations[record['termination']] += 1
        require([move.uci() for move in pgn.mainline_moves()] == record['uci_moves']
                and pgn.end().board().fen(en_passant='fen') == record['final_fen'], f'Game {number}: PGN differs')
        headers = pgn.headers
        require(headers['Result'] == record['result'] and headers['Round'] == str(number)
                and headers['Pair'] == str(pair) and headers['Opening'] == source['name']
                and headers['White'].startswith(white + ':') and headers['Black'].startswith(black + ':')
                and headers['DrawPolicy'] == 'claim' and headers['RequestedMoveTimeMS'] == '100'
                and headers['Termination'] == 'normal' and headers['TerminationReason'] == record['termination']
                and headers['WhiteSHA256'] == headers['BlackSHA256'] == sha(binary), f'Game {number}: PGN headers differ')
        totals['mainline_plies'] += record['plies']
        game_receipts.append({'game': number, 'pair': pair, 'candidate_color': 'white' if white == 'candidate' else 'black',
                              'result': record['result'], 'termination': record['termination'], 'plies': record['plies']})
    summary = match['summary']
    for label in ('wins', 'draws', 'losses'):
        require(summary['candidate_' + label] == wdl[label], 'Summary WDL differs')
    points = wdl['wins'] + .5 * wdl['draws']
    require(summary['candidate_points'] == points and summary['candidate_score'] == points / 16
            and summary['completed_games'] == 16 and summary['incomplete_games'] == summary['unplayed_games'] == 0,
            'Summary score or completion differs')
    return {'status': 'PASS', 'verified_input_count': len(checked_inputs), 'verified_inputs': checked_inputs,
            'games': 16, 'paired_starts': 8, 'candidate_white_games': 8, 'candidate_black_games': 8,
            'requested_movetime_ms': 100, 'same_binary_sha256': sha(binary), 'model_sha256': model_hashes,
            'training_binding': training, 'option_difference': changed, 'totals': dict(totals),
            'terminations': dict(terminations), 'candidate_wdl': dict(wdl), 'candidate_points': points,
            'candidate_score': points / 16, 'reported_match_elapsed_seconds': match['total_elapsed_seconds'],
            'game_receipts': game_receipts,
            'limitations': ['Recorded option acceptance is not independent UCI state readback.',
                            'Binary/source identity is bound by hashes and prior build receipts; this audit does not rebuild.',
                            'One fixed MSE seed; only one relative turn input and its sixteen parameters are added.',
                            'Exposed development openings and 16 games provide a descriptive screen, not Elo or promotion.',
                            'No process CPU accounting, system-load isolation, extra training or match retry is performed.']}


if __name__ == '__main__':
    directory = Path(sys.argv[1]).resolve()
    started = time.perf_counter()
    output = {'schema_version': 1, 'auditor': 'Independent direct python-chess replay; no runner imports or engine invocation',
              'created_utc': datetime.now(timezone.utc).isoformat(), 'auditor_source_sha256': sha(__file__)}
    try:
        output['raw_sha256'] = {name: sha(directory / name) for name in ('plan.json', 'games.json', 'games.pgn')}
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
