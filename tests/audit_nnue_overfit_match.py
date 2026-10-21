"""Read-only binding and direct replay of the guarded NNUE candidate screen.

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
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)



def read_lines(path):
    return [json.loads(line) for line in Path(path).read_text(encoding='utf-8').splitlines()]


def model_binding(candidate_path):
    """Parse v3 and check its checksum independently of the production parser."""
    tokens = Path(candidate_path).read_text(encoding='ascii').split()
    require(len(tokens) == 7 + 16 * 3 + 768 * 16 + 2
            and tokens[:7] == ['REGICIDE_NNUE', '3', '769', '16', '4064', '256', '32']
            and tokens[-2] == 'checksum', 'Candidate v3 structure differs')
    words = tokens[1:-2] + tokens[-1:]
    require(all(re.fullmatch(r'-?[0-9]{1,20}', word) for word in words), 'Invalid model integer')
    values = list(map(int, words))
    checksum = 14695981039346656037
    for value in values[:-1]:
        for byte in (value & 0xFFFFFFFF).to_bytes(4, 'little'):
            checksum = ((checksum ^ byte) * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    require(checksum == values[-1], 'Independent v3 checksum differs')
    turn = values[-17:-1]
    require(any(turn) and all(-32767 <= value <= 32767 for value in turn),
            'Invalid or zero relative-turn weights')
    return {'model_checksum': checksum, 'turn_weights_integer': turn}


def training_binding(directory, plan, candidate_path):
    protocol, trained, selection, validation = (read(directory / name) for name in
        ('protocol.json', 'training.json', 'selection.json', 'validation.json'))
    for name in ('protocol.json', 'training.json', 'selection.json', 'validation.json', 'model.nnue'):
        path = directory / name
        require(str(path) in plan['inputs'] and sha(path) == plan['inputs'][str(path)],
                'Training/model receipt is not bound to preregistered match: ' + name)
    require(Path(candidate_path) == directory / 'model.nnue', 'Candidate does not use the new training output')
    require(protocol['config'] == trained['config'] and protocol['guards'] == trained['guards']
            == {'patience': 5, 'eval_interval': 5, 'min_delta_cp2': 1.0}, 'Training or guard conditions differ')
    shared = {'batch_size': 128, 'epochs': 500, 'input_bias_lr': .0002, 'input_scale': 32,
              'loss': 'mse', 'output_lr': .005, 'seed': 20261020}
    require({key: value for key, value in protocol['config'].items() if key != 'candidate_id'} == shared
            and protocol['config']['candidate_id'] == 'sf18-mse-stm-guarded-20261020'
            and trained['candidate_runs'] == 1 and trained['loss'] == 'mse',
            'Expected exactly one guarded MSE run with unchanged optimizer conditions')
    require(protocol['architecture'] == {'format': 3, 'piece_features': 768, 'relative_turn_features': 1,
                                         'hidden': 16, 'added_parameters': 0, 'changed': False}
            and trained['architecture_unchanged'] is True, 'NNUE architecture changed')
    warm_sha = 'f001f1d3bd9b710e046d37c88040c13a90e8507b28bfb2c416814b9427332f26'
    initial = protocol['initial']
    require(initial['model_sha256'] == warm_sha and initial['failed_model_used_as_initial'] is False
            and sha(initial['path']) == initial['sha256'] == sha(directory / 'initial-floats.npz'),
            'Pre-failure float initializer was not reused exactly')
    require(protocol['data']['train_rows'] == 350 and protocol['data']['tuning_rows'] == 81
            and protocol['data']['new_teacher_labels'] == 0
            and protocol['data']['fresh_independent_validation'] is False,
            'Expected the same exposed tuning data, without a fresh-test claim')
    labels_path = Path(protocol['data']['labels_path'])
    require(sha(labels_path) == protocol['data']['labels_sha256']
            == '2117ece8b2671906d4f754f9d907751df59b8404904571db9df791e1841d0fc3',
            'Frozen Stockfish labels differ')
    labels = read_lines(labels_path)
    partitions = {name: [row for row in labels if row['partition'] == name] for name in ('train', 'validation')}
    require(len(labels) == 431 and [len(partitions[name]) for name in partitions] == [350, 81],
            'Accepted train/tuning rows differ')
    for key in ('family_id', 'feature_hash'):
        require(not ({row[key] for row in partitions['train']}
                     & {row[key] for row in partitions['validation']}), 'Train/tuning leakage: ' + key)
    prior_directory = labels_path.parent.parent
    prior_protocol, prior_training = read(prior_directory / 'protocol.json'), read(prior_directory / 'training.json')
    require({key: value for key, value in prior_protocol['config'].items() if key != 'candidate_id'} == shared,
            'Old and guarded training conditions differ beyond stopping')
    from audit_stockfish_teacher_match import audit_teacher
    teacher = audit_teacher(prior_directory, prior_protocol, read(prior_directory / 'selected.json'))
    for name, expected in protocol['input_sha256'].items():
        source = Path(name)
        snapshot = directory / 'source-snapshot' / source.relative_to(ROOT) if source.suffix == '.py' else source
        require(sha(snapshot) == expected, 'Executed source or input differs: ' + name)
    require(protocol['default_changed'] is False
            and sha(protocol['diagnosis']['path']) == protocol['diagnosis']['sha256'],
            'Stable default or diagnosis binding differs')
    require(trained['selection'] == selection and selection['loss'] == 'mse'
            and selection['objective'] == 'held_out_integer_white_cp_mse'
            and selection['mode'] == 'validation_mse_early_stopping'
            and selection['validation_used_in_gradients'] is False, 'Selection policy differs')
    evaluations = selection['validation_evaluations']
    require(evaluations and evaluations[0]['epoch'] == 0
            and selection['initial_validation'] == evaluations[0]
            and selection['last_validation'] == evaluations[-1], 'Missing epoch-zero or last validation')
    stopped, interval = selection['stopped_epoch'], selection['eval_interval_epochs']
    require(selection['max_epochs'] == 500 and interval == 5 and selection['patience_evaluations'] == 5
            and selection['min_delta_cp2'] == 1.0 and 1 <= stopped <= 500,
            'Selection stopping limits differ')
    expected_epochs = [0] + [epoch for epoch in range(1, stopped + 1) if epoch % interval == 0 or epoch == 500]
    require([row['epoch'] for row in evaluations] == expected_epochs
            and evaluations[-1]['epoch'] == stopped, 'Validation evaluation schedule differs')
    best = min(evaluations, key=lambda row: row['mse_cp2'])
    require(selection['selected_validation'] == best and selection['selected_epoch'] == best['epoch'],
            'Selected model is not the earliest minimum integer tuning MSE')
    reference, stale = evaluations[0]['mse_cp2'], 0
    for index, evaluation in enumerate(evaluations[1:], 1):
        require(all(math.isfinite(evaluation[key]) and evaluation[key] >= 0
                    for key in ('mse_cp2', 'rmse_cp', 'mae_cp')), 'Nonfinite tuning metric')
        if evaluation['mse_cp2'] < reference - selection['min_delta_cp2']:
            reference, stale = evaluation['mse_cp2'], 0
        else:
            stale += 1
        require(stale < 5 or index == len(evaluations) - 1, 'Training continued after patience was exhausted')
    expected_reason = 'patience_exhausted' if stale >= 5 else 'max_epochs'
    require(selection['stop_reason'] == expected_reason and (stale >= 5 or stopped == 500)
            and selection['stopped_early'] == (stopped < 500), 'Early-stop reason differs')
    history = trained['history']
    require(history == prior_training['history'][:stopped]
            and [entry['epoch'] for entry in history] == list(range(1, stopped + 1))
            and history[-1]['optimizer_steps'] == selection['optimizer_steps'] == stopped * math.ceil(350 / 128),
            'Adam history differs from the corresponding old fixed-epoch prefix')
    sys.path.insert(0, str(ROOT / 'scripts'))
    import neural_stm as stm
    import neural_training as base
    model = stm.load_model(candidate_path)
    with np.load(directory / 'float_weights.npz', allow_pickle=False) as archive:
        floats = {key: archive[key].copy() for key in stm.PARAMETER_KEYS}
    require(stm.model_checksum(stm.quantize(floats, 32)) == stm.model_checksum(model),
            'Saved restored float model differs from the actual integer model')
    warm_path = Path(initial['path']).parent / 'model.nnue'
    failed_path = Path(protocol['prior_failure']['path'])
    require(sha(warm_path) == warm_sha and sha(failed_path) == protocol['prior_failure']['sha256'],
            'Warm or prior failed model changed')
    require(trained['model_changed_from_warm'] == (sha(candidate_path) != sha(warm_path))
            and trained['model_sha256'] == sha(candidate_path), 'Actual candidate identity differs')
    for partition, rows in partitions.items():
        name = 'train' if partition == 'train' else 'tuning'
        indices, targets, turns = stm.pack_records(rows)
        actual = {'count': len(rows), **{label: base.error_metrics(stm.predict(net, indices, turns), targets)
            for label, net in (('warm', stm.load_model(warm_path)), ('failed_500', stm.load_model(failed_path)),
                               ('guarded', model))}}
        require(trained['metrics'][name] == actual, 'Recomputed model metrics differ: ' + name)
    for name, metric in (('initial_validation', 'warm'), ('selected_validation', 'guarded')):
        require({key: selection[name][key] for key in ('mse_cp2', 'rmse_cp', 'mae_cp')}
                == trained['metrics']['tuning'][metric], 'Selection metric differs from actual restored model: ' + name)
    require(validation['corpus_positions'] == 431 and validation['special_positions'] == 46
            and validation['positions'] == 477 and validation['python_native_full_incremental_mismatches'] == 0
            and validation['native_model_returncode'] == 0, 'Trained-model native correctness failed')
    for name, expected in validation['input_sha256'].items():
        require(sha(name) == expected, 'Native validation input differs: ' + name)
    cached = validation['reused_legacy_validation']
    require(sha(cached['path']) == cached['sha256'], 'Cached legacy verification changed')
    cached_result = read(cached['path'])
    require(cached_result['legacy_old_new_python_mismatches']
            == cached_result['python_native_full_incremental_mismatches'] == 0, 'Legacy compatibility failed')
    return {'config': protocol['config'], 'guards': protocol['guards'], 'teacher': teacher,
            'added_trainable_parameters': 0, 'architecture_unchanged': True, 'candidate_runs': 1,
            'warm_model_sha256': warm_sha, 'old_fixed_epoch_history_prefix_exact': True,
            'selected_epoch': best['epoch'], 'stopped_epoch': stopped, 'stop_reason': expected_reason,
            'tuning_mse_cp2': best['mse_cp2'], 'fresh_independent_validation': False,
            'corpus_positions': 431, 'python_native_full_incremental_mismatches': 0,
            **model_binding(candidate_path)}

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
    require(changed == ['NeuralModelFile', 'UseNNUEEvaluation'],
            'An option besides evaluation enable/model differs')
    require(options['baseline']['Hash'] == plan['hash_mb'] == 16
            and options['baseline']['UseNNUEEvaluation'] is False
            and options['candidate']['UseNNUEEvaluation'] is True
            and options['baseline']['UseNeuralEvaluation'] is False
            and options['candidate']['UseNeuralEvaluation'] is False
            and options['baseline']['UseIncrementalNeuralEvaluation'] is True
            and options['baseline']['NeuralModelFile'] == '<builtin>', 'Classical/NNUE options differ')
    classical = read(ROOT / 'config/engine-profiles/classical.json')['options']
    require(options['baseline'] == classical, 'Baseline does not use the complete basic classical profile')
    model_hashes = {'baseline': None, 'candidate': sha(options['candidate']['NeuralModelFile'])}
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
                            'One validation-guarded MSE fine-tune; unchanged v3 architecture and reused Stockfish labels.',
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
