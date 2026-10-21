"""Read-only binding and direct replay of the Stockfish-teacher candidate screen.

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
WARM_MODEL_SHA = 'f001f1d3bd9b710e046d37c88040c13a90e8507b28bfb2c416814b9427332f26'
TEACHER_SHA = '9bde420202717ce083412027fbfb8c5c935b537591d712be8a8a8bae92f6e8d6'
SHARED_CONFIG = {'batch_size': 128, 'epochs': 500, 'input_bias_lr': .0002,
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
    for receipt in ('protocol.json', 'training.json', 'validation.json', 'model.nnue', 'selected.json',
                    'teacher/teacher-report.json', 'teacher/teacher-queries.jsonl', 'teacher/labels.jsonl'):
        path = directory / receipt
        require(str(path) in plan['inputs'] and sha(path) == plan['inputs'][str(path)],
                'Training/model receipt is not bound to preregistered match: ' + receipt)
    require(Path(candidate_path) == directory / 'model.nnue', 'Candidate does not use the new training output')
    require(protocol['config'] == trained['config']
            and {k: v for k, v in protocol['config'].items() if k != 'candidate_id'} == SHARED_CONFIG,
            'Candidate training conditions differ')
    require(protocol['config']['candidate_id'] == 'sf18-mse-stm-small-20261020'
            and protocol['training']['candidate_runs'] == trained['candidate_runs'] == 1
            and trained['loss'] == 'mse', 'Expected one fixed final-epoch MSE fine-tune')
    require(protocol['architecture'] == {'format': 3, 'piece_features': 768, 'relative_turn_features': 1,
                                         'hidden': 16, 'added_parameters': 0, 'changed': False}
            and trained['architecture_unchanged'] is True, 'NNUE architecture changed')
    initial = protocol['initial']
    require(initial['model_sha256'] == WARM_MODEL_SHA
            and sha(initial['path']) == initial['sha256'] == sha(directory / 'initial-floats.npz'),
            'Existing STM float checkpoint was not reused exactly')
    sampling = protocol['sampling']
    require(sampling['seed'] == 20261021 and sampling['requested'] == {'train': 512, 'validation': 128}
            and sampling['selected_before_teacher'] is True and sampling['replacement_after_rejection'] is False
            and sampling['holdout_read'] is False, 'Small source selection policy changed')
    source = ROOT / 'docs/checks/2026-10-20-neural-target-ab/attempt-002/trainval'
    selected = read(directory / 'selected.json')
    rng = np.random.default_rng(20261021)
    require(set(selected) == {'train', 'validation'}, 'Unexpected selection partitions')
    for partition, count in (('train', 512), ('validation', 128)):
        original = read_lines(source / (partition + '.jsonl'))
        expected = [original[int(index)] for index in rng.permutation(len(original))[:count]]
        require(selected[partition] == expected, 'Selected rows do not match preregistered permutation: ' + partition)
    require(not ({row['family_id'] for row in selected['train']}
                 & {row['family_id'] for row in selected['validation']})
            and not ({row['feature_hash'] for row in selected['train']}
                     & {row['feature_hash'] for row in selected['validation']}), 'Train/validation leakage')
    teacher = audit_teacher(directory, protocol, selected)
    train_count = teacher['accepted_by_partition']['train']
    require(trained['model_sha256'] == sha(candidate_path), 'Training report model SHA differs')
    require([entry['epoch'] for entry in trained['history']] == list(range(1, 501))
            and trained['history'][-1]['optimizer_steps'] == 500 * math.ceil(train_count / 128),
            'Expected exactly the fixed 500 final training epochs')
    for name, expected in protocol['input_sha256'].items():
        path = Path(name)
        snapshot = directory / 'source-snapshot' / path.relative_to(ROOT) if path.suffix == '.py' else path
        require(sha(snapshot) == expected, 'Executed training source or data snapshot differs: ' + name)
    require(validation['corpus_positions'] == teacher['accepted_rows']
            and validation['python_native_full_incremental_mismatches'] == 0
            and validation['native_model_returncode'] == 0
            and validation['positions'] == validation['corpus_positions'] + validation['special_positions']
            and validation['special_positions'] > 0, 'Trained-model correctness validation failed')
    prior = validation['reused_legacy_validation']
    require(sha(prior['path']) == prior['sha256'], 'Prior native/legacy validation changed')
    old = read(prior['path'])
    require(old['legacy_old_new_python_mismatches'] == old['python_native_full_incremental_mismatches'] == 0,
            'Prior legacy/native compatibility failed')
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
    require(any(turn) and all(-32767 <= value <= 32767 for value in turn), 'Invalid or zero relative-turn weights')
    return {'config': protocol['config'], 'added_trainable_parameters': 0, 'architecture_unchanged': True,
            'warm_model_sha256': WARM_MODEL_SHA, 'teacher': teacher,
            'corpus_positions': teacher['accepted_rows'], 'model_checksum': checksum, 'turn_weights_integer': turn,
            'python_native_full_incremental_mismatches': 0, 'legacy_old_new_python_mismatches': 0}


def read_lines(path):
    return [json.loads(line) for line in Path(path).read_text(encoding='utf-8').splitlines()]


def audit_teacher(directory, protocol, selected):
    report = read(directory / 'teacher/teacher-report.json')
    queries = read_lines(directory / 'teacher/teacher-queries.jsonl')
    labels = read_lines(directory / 'teacher/labels.jsonl')
    expected_options = {'Threads': 1, 'Hash': 16, 'Skill Level': 20, 'UCI_LimitStrength': False,
                        'SyzygyPath': '', 'MultiPV': 1, 'UCI_AnalyseMode': True}
    profile = protocol['teacher']['profile']
    require(report['binary_sha256'] == profile['binary']['sha256'] == TEACHER_SHA
            and sha(report['engine_path']) == TEACHER_SHA, 'Offline teacher binary differs')
    require(profile['source']['version'] == '18' and profile['source']['license'] == 'GPL-3.0-or-later'
            and profile['source']['source_url'] == 'https://github.com/official-stockfish/Stockfish',
            'Teacher source or license provenance differs')
    require(report['requested_nodes'] == protocol['teacher']['requested_nodes'] == 10000
            and report['max_abs_cp'] == protocol['teacher']['max_abs_cp'] == 1500
            and report['options'] == expected_options, 'Offline teacher search/label conditions differ')
    for name, expected in report['files'].items():
        require(sha(directory / 'teacher' / name) == expected, 'Teacher output SHA differs: ' + name)
    bank = selected['train'] + selected['validation']
    require(len(bank) == len(queries) == report['requested_rows'] == 640, 'Queries differ from frozen small selection')
    accepted, reasons = [], Counter()
    for index, (row, query) in enumerate(zip(bank, queries, strict=True)):
        label = f'Query {index + 1}'
        board = chess.Board(row['initial_fen'])
        require(board.is_valid(), label + ': invalid initial FEN')
        for text in row['history']:
            move = chess.Move.from_uci(text)
            require(board.outcome(claim_draw=True) is None and move in board.legal_moves,
                    label + ': invalid or postterminal source history')
            board.push(move)
        require(board.fen(en_passant='fen') == row['fen'] == query['fen']
                and len(board.move_stack) == query['history_plies'], label + ': source state differs')
        require(query['index'] == index and query['query_id'] == f'sf-{index + 1:06d}'
                and query['source_query_id'] == row['query_id'] and query['partition'] == row['partition']
                and query['family_id'] == row['family_id'], label + ': source metadata differs')
        require(query['requested_nodes'] == 10000 and query['binary_sha256'] == TEACHER_SHA
                and query['options'] == expected_options and query['engine_id'] == report['engine_id'],
                label + ': teacher identity/options differ')
        if query['status'] == 'accepted':
            require(query['reasons'] == [] and board.outcome(claim_draw=True) is None, label + ': invalid acceptance')
            score, packet = query['score'], query['raw_score_info_packet']
            white, stm = score['white_cp'], score['stm_cp']
            require(score['kind'] == 'cp' and type(white) is int and type(stm) is int
                    and abs(white) <= 1500 and stm == (white if board.turn else -white)
                    and score['pov'] == ('white' if board.turn else 'black')
                    and score['mate_stm'] is score['mate_white'] is None,
                    label + ': CP/POV or finite-range policy differs')
            require(not packet.get('lowerbound', False) and not packet.get('upperbound', False)
                    and packet['score']['white_cp'] == white and packet['score']['white_mate'] is None
                    and packet['depth'] == query['depth'] >= 1
                    and packet['nodes'] == query['score_nodes'] >= 1
                    and query['nodes'] >= query['score_nodes'] and packet['pv'] == query['pv_uci'],
                    label + ': final exact score packet differs')
            pv = board.copy(stack=True)
            require(query['pv_uci'] and query['bestmove_uci'] == query['pv_uci'][0], label + ': missing/mismatched best move')
            for text in query['pv_uci']:
                move = chess.Move.from_uci(text)
                require(move in pv.legal_moves, label + ': illegal teacher PV')
                pv.push(move)
            accepted.append({**row, 'source_target_white_cp': row.get('target_white_cp'),
                             'target_white_cp': white, 'teacher_white_cp': white, 'teacher_stm_cp': stm,
                             'teacher_query_id': query['query_id'], 'teacher_source': 'Stockfish offline root evaluation'})
        else:
            require(query['status'] == 'excluded' and query['reasons'], label + ': invalid exclusion status')
            for reason in query['reasons']:
                if reason == 'terminal':
                    require(board.outcome(claim_draw=True) is not None, label + ': false terminal exclusion')
                elif reason == 'bound':
                    packet = query['raw_score_info_packet']
                    require(packet.get('lowerbound', False) or packet.get('upperbound', False),
                            label + ': false bound exclusion')
                elif reason == 'mate':
                    score, packet = query['score'], query['raw_score_info_packet']
                    require(score['kind'] == 'mate' and score['white_cp'] is score['stm_cp'] is None
                            and type(score['mate_white']) is int
                            and score['mate_white'] == packet['score']['white_mate'],
                            label + ': false mate exclusion')
                else:
                    raise ValueError(label + ': unexpected rejection needs independent review: ' + reason)
            reasons.update(query['reasons'])
    counts = dict(Counter(row['partition'] for row in accepted))
    require(accepted == labels and len(labels) == report['accepted_rows']
            and report['excluded_rows'] == 640 - len(labels)
            and dict(reasons) == report['exclusion_reasons']
            and counts == report['accepted_by_partition']
            and counts.get('train', 0) >= 256 and counts.get('validation', 0) >= 64,
            'Accepted labels, counts, exclusions or partitions differ')
    return {'requested_rows': 640, 'accepted_rows': len(labels), 'accepted_by_partition': counts,
            'excluded_rows': report['excluded_rows'], 'exclusion_reasons': dict(reasons),
            'teacher_binary_sha256': TEACHER_SHA, 'requested_nodes': 10000,
            'score_pov': 'white', 'max_abs_cp': 1500, 'selection_reproduced': True,
            'source_history_and_pvs_replayed': True}


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
                            'One fixed MSE fine-tune candidate with unchanged v3 architecture; offline Stockfish labels.',
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
