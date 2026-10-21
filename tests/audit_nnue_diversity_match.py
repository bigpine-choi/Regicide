"""Read-only binding and direct replay of the diverse-data guarded NNUE candidate screen.

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
SOURCE_BOARDS = {}
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
    payload = values[6:-1]
    require(all(-1048576 <= value <= 1048576 for value in payload[:16])
            and all(-8192 <= value <= 8192 for value in payload[16:32])
            and all(-32767 <= value <= 32767 for value in payload[32:]),
            'Candidate payload exceeds the v3 integer bounds')
    turn = values[-17:-1]
    require(any(turn) and all(-32767 <= value <= 32767 for value in turn),
            'Invalid or zero relative-turn weights')
    return {'model_checksum': checksum, 'turn_weights_integer': turn}


def source_board(row):
    key = row['initial_fen'], tuple(row['history'])
    if key in SOURCE_BOARDS:
        board = SOURCE_BOARDS[key]
        identity = min(board.board_fen(), board.mirror().board_fen())
        require(board.fen(en_passant='fen') == row['fen']
                and hashlib.sha256(identity.encode('ascii')).hexdigest() == row['feature_hash'],
                'Cached source state identity differs')
        return board
    board = chess.Board(row['initial_fen'])
    require(board.is_valid(), 'Invalid initial source position')
    for text in row['history']:
        move = chess.Move.from_uci(text)
        require(board.outcome(claim_draw=True) is None and move in board.legal_moves,
                'Illegal or postterminal source history')
        board.push(move)
    require(board.fen(en_passant='fen') == row['fen'], 'Source history/FEN mismatch')
    identity = min(board.board_fen(), board.mirror().board_fen())
    require(hashlib.sha256(identity.encode('ascii')).hexdigest() == row['feature_hash'],
            'Source canonical mirror-feature identity mismatch')
    SOURCE_BOARDS[key] = board
    return board


def audit_teacher(directory, protocol, selected):
    report = read(directory / 'teacher/teacher-report.json')
    queries = read_lines(directory / 'teacher/teacher-queries.jsonl')
    labels = read_lines(directory / 'teacher/labels.jsonl')
    teacher_sha = '9bde420202717ce083412027fbfb8c5c935b537591d712be8a8a8bae92f6e8d6'
    options = {'Threads': 1, 'Hash': 16, 'Skill Level': 20, 'UCI_LimitStrength': False,
               'SyzygyPath': '', 'MultiPV': 1, 'UCI_AnalyseMode': True}
    require(report['binary_sha256'] == sha(report['engine_path']) == teacher_sha,
            'Stockfish 18 teacher binary differs')
    require(report['requested_nodes'] == 10000 and report['max_abs_cp'] == 1500
            and report['options'] == options, 'Teacher search/label policy differs')
    for name, expected in report['files'].items():
        require(sha(directory / 'teacher' / name) == expected, 'Teacher output changed: ' + name)
    require(len(selected) == len(queries) == report['requested_rows'] <= 2000,
            'Teacher query count differs or exceeds the bounded data budget')
    accepted, exclusions, total_nodes = [], Counter(), 0
    for index, (row, query) in enumerate(zip(selected, queries, strict=True)):
        prefix = f'Teacher query {index + 1}: '
        board = source_board(row)
        require(query['index'] == index and query['query_id'] == f'sf-{index + 1:06d}'
                and query['source_query_id'] == row['query_id'] and query['fen'] == row['fen']
                and query['family_id'] == row['family_id'] and query['partition'] == row['partition']
                and query['history_plies'] == len(row['history']), prefix + 'Source metadata differs')
        require(query['binary_sha256'] == teacher_sha and query['options'] == options
                and query['requested_nodes'] == 10000 and query['engine_id'] == report['engine_id'],
                prefix + 'Teacher identity/options differ')
        reasons = []
        if board.outcome(claim_draw=True) is not None:
            reasons = ['terminal']
        else:
            score, packet = query['score'], query['raw_score_info_packet']
            require(score['pov'] == ('white' if board.turn else 'black'), prefix + 'Score POV differs')
            if score['kind'] == 'mate':
                require(score['white_cp'] is score['stm_cp'] is None
                        and type(score['mate_white']) is int
                        and score['mate_white'] == packet['score']['white_mate'], prefix + 'Mate score differs')
                reasons.append('mate')
            else:
                white, stm = score['white_cp'], score['stm_cp']
                require(score['kind'] == 'cp' and type(white) is int and type(stm) is int
                        and stm == (white if board.turn else -white)
                        and score['mate_white'] is score['mate_stm'] is None
                        and packet['score']['white_cp'] == white
                        and packet['score']['white_mate'] is None, prefix + 'Exact white/STM score differs')
                if abs(white) > 1500:
                    reasons.append('score_range')
            if packet.get('lowerbound', False) or packet.get('upperbound', False):
                reasons.append('bound')
            require(type(query['depth']) is int and query['depth'] >= 1
                    and packet['depth'] == query['depth']
                    and type(query['score_nodes']) is int and query['score_nodes'] >= 1
                    and packet['nodes'] == query['score_nodes']
                    and query['nodes'] >= query['score_nodes'], prefix + 'Node/depth receipt differs')
            total_nodes += query['nodes']
            require(query['pv_uci'] == packet['pv'] and query['pv_uci']
                    and query['bestmove_uci'] == query['pv_uci'][0], prefix + 'PV/best move differs')
            replay = board.copy(stack=True)
            for text in query['pv_uci']:
                move = chess.Move.from_uci(text)
                require(move in replay.legal_moves, prefix + 'Illegal teacher PV')
                replay.push(move)
        require(set(query['reasons']) == set(reasons)
                and query['status'] == ('excluded' if reasons else 'accepted'), prefix + 'Acceptance differs')
        if reasons:
            exclusions.update(reasons)
        else:
            accepted.append({**row, 'source_target_white_cp': row.get('target_white_cp'),
                             'target_white_cp': score['white_cp'], 'teacher_white_cp': score['white_cp'],
                             'teacher_stm_cp': score['stm_cp'], 'teacher_query_id': query['query_id'],
                             'teacher_source': 'Stockfish offline root evaluation'})
    counts = dict(Counter(row['partition'] for row in accepted))
    require(accepted == labels and len(labels) == report['accepted_rows']
            and len(queries) - len(labels) == report['excluded_rows']
            and counts == report['accepted_by_partition']
            and dict(exclusions) == report['exclusion_reasons'], 'Teacher labels/counts/exclusions differ')
    return labels, {'requested_rows': len(queries), 'accepted_rows': len(labels),
                    'accepted_by_partition': counts, 'excluded_rows': report['excluded_rows'],
                    'exclusion_reasons': dict(exclusions), 'requested_nodes': 10000,
                    'observed_completed_nodes': total_nodes, 'teacher_binary_sha256': teacher_sha,
                    'score_pov': 'white', 'source_history_and_pvs_replayed': True}


def distribution(rows):
    phases, movers, kinds, families = Counter(), Counter(), Counter(), set()
    checks = captures = 0
    groups = {}
    for row in rows:
        board = chess.Board(row['fen'])
        pieces = len(board.piece_map())
        phases['early' if pieces >= 28 else 'middle' if pieces >= 16 else 'late'] += 1
        movers['white' if board.turn else 'black'] += 1
        kinds[row['kind']] += 1
        families.add(row['family_id'])
        checks += int(board.is_check())
        captures += int(any(board.is_capture(move) for move in board.legal_moves))
        groups.setdefault(row['partition'], []).append(row)
    result = {'rows': len(rows), 'phases': dict(phases), 'movers': dict(movers), 'kinds': dict(kinds),
              'families': len(families), 'in_check': checks, 'legal_capture_available': captures}
    if len(groups) > 1:
        result['by_partition'] = {key: distribution(value) for key, value in groups.items()}
    return result


def audit_expanded_data(dataset, partitions, plan):
    manifest = read(dataset / 'manifest.json')
    require(str(dataset / 'manifest.json') in plan['inputs']
            and sha(dataset / 'manifest.json') == plan['inputs'][str(dataset / 'manifest.json')],
            'Expanded data manifest is not bound to the match')
    for field in ('reused_labels', 'new_labels', 'selection', 'bank_manifest',
                  'generation_protocol', 'teacher_report'):
        binding = manifest[field]
        require(sha(binding['path']) == binding['sha256'], 'Expanded-data binding differs: ' + field)
    protocol = read(manifest['generation_protocol']['path'])
    selected = read(manifest['selection']['path'])
    bank = read(manifest['bank_manifest']['path'])
    reused = read_lines(manifest['reused_labels']['path'])
    require(protocol['seed'] == 20261022 and protocol['maximum_teacher_queries'] == len(selected) == 2000
            and protocol['requested_new_rows'] == bank['requested_new_rows']
            == {'train': 1600, 'validation': 300, 'test': 100}
            and protocol['teacher_nodes_per_query'] == 10000 and protocol['teacher_max_abs_cp'] == 1500
            and protocol['no_new_games'] is True, 'Bounded source/teacher plan changed')
    profile = protocol['teacher_profile']
    require(profile['source']['version'] == '18' and profile['source']['license'] == 'GPL-3.0-or-later'
            and profile['source']['source_url'] == 'https://github.com/official-stockfish/Stockfish',
            'Stockfish teacher provenance differs')
    require(sha(manifest['reused_labels']['path']) == protocol['reused_labels']['sha256']
            == '2117ece8b2671906d4f754f9d907751df59b8404904571db9df791e1841d0fc3'
            and len(reused) == manifest['reused_labels']['rows'] == 431
            and Counter(row['partition'] for row in reused) == {'train': 350, 'validation': 81},
            'The original 431 labels were not reused exactly')
    for name, expected in protocol['input_sha256'].items():
        source = Path(name)
        snapshot = dataset / 'source-snapshot' / source.relative_to(ROOT) if source.suffix == '.py' else source
        require(sha(snapshot) == expected, 'Executed data source/input differs: ' + name)
    for binding in bank['source_artifacts']:
        require(sha(binding['path']) == binding['sha256'], 'Original source bank changed')
    digest = hashlib.sha256(json.dumps(selected, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    require(digest == bank['selected_query_sha256'] and bank['seed'] == 20261022,
            'Frozen selected query ordering differs')
    game_files, query_files = {}, {}
    for row in selected:
        source = row['source_game_file']
        if source not in game_files:
            game_files[source] = {game['game_id']: game for game in read_lines(source)}
        source = row['source_query_file']
        if source not in query_files:
            query_files[source] = {query['query_id']: query for query in read_lines(source)}
        game = game_files[row['source_game_file']][row['source_game_id']]
        query = query_files[row['source_query_file']][row['source_query_id']]
        length = row['source_history_plies']
        require(type(length) is int and length == len(row['history'])
                and row['initial_fen'] == game['initial_fen'] == query['initial_fen']
                and row['history'] == game['moves_uci'][:length] == query['history']
                and row['family_id'] == game['family_id'] == query['family_id']
                and row['partition'] == game['partition'] == query['partition']
                == bank['family_assignments'][row['family_id']]
                and row['source_game_id'] == query['game_id']
                and row['fen'] == query['fen'] and row['feature_hash'] == query['feature_hash'],
                'New selected row was not an unmodified source query/game position')
        board = source_board(row)
        count = len(board.piece_map())
        require(row['piece_count'] == count
                and row['phase'] == ('early' if count >= 28 else 'middle' if count >= 16 else 'late')
                and row['turn'] == ('white' if board.turn else 'black')
                and row['in_check'] == board.is_check()
                and row['legal_capture_count'] == sum(board.is_capture(move) for move in board.legal_moves),
                'Selected row coverage metadata differs')
    require(len({row['feature_hash'] for row in selected}) == 2000
            and not ({row['feature_hash'] for row in reused} & {row['feature_hash'] for row in selected}),
            'New teacher rows repeat old or mirrored piece inputs')
    require(distribution(selected) == bank['selected'] and distribution(reused) == bank['reused']
            and distribution(reused + selected) == bank['combined_before_new_teacher_exclusions'],
            'Pre-label source distributions differ')
    new, teacher = audit_teacher(dataset, protocol, selected)
    require(sha(dataset / 'teacher/labels.jsonl') == manifest['new_labels']['sha256']
            and len(new) == manifest['new_labels']['rows'], 'New accepted teacher labels differ')
    expected_rows = []
    for batch, rows in (('reused-small', reused), ('expanded-001', new)):
        for row in rows:
            original = row['partition']
            expected_rows.append({**row, 'source_partition': original,
                                  'partition': 'tuning' if original == 'validation' else original,
                                  'teacher_batch': batch})
    for name, rows in partitions.items():
        expected = [row for row in expected_rows if row['partition'] == name]
        require(rows == expected and distribution(rows) == manifest['distribution'][name],
                'Accepted/reused merge or actual post-exclusion distribution differs: ' + name)
    require({name: len(rows) for name, rows in partitions.items()} == manifest['rows']
            and Counter(row['partition'] for row in new) == manifest['new_teacher_rows_by_partition'],
            'Accepted split sizes differ')
    source_root = Path(protocol['source_root'])
    warm_rows = read_lines(source_root / 'trainval/train.jsonl') + read_lines(source_root / 'trainval/validation.jsonl')
    test_features = {row['feature_hash'] for row in partitions['test']}
    test_families = {row['family_id'] for row in partitions['test']}
    require(not (test_features & {row['feature_hash'] for row in warm_rows})
            and not (test_families & {row['family_id'] for row in warm_rows}),
            'This fine-tune test overlaps the warm model trainval family or input set')
    opening = bank['match_openings']
    require(opening is not None and sha(opening['path']) == opening['sha256'],
            'Declared comparison opening exclusions changed')
    opening_features = set()
    for entry in read(opening['path'])['openings']:
        board = chess.Board(entry.get('initial_fen', chess.STARTING_FEN))
        for text in [None] + entry['moves']:
            if text is not None:
                board.push_uci(text)
            identity = min(board.board_fen(), board.mirror().board_fen())
            opening_features.add(hashlib.sha256(identity.encode('ascii')).hexdigest())
    require(not (opening_features & {row['feature_hash'] for row in selected}),
            'New teacher selection overlaps a declared match-opening prefix')
    return {'reused_rows': len(reused), 'new_requested_rows': len(selected),
            'new_accepted_rows': len(new), 'teacher': teacher,
            'source_histories_authenticated': True, 'source_families_preserved': True,
            'all_split_mirror_features_disjoint': True, 'test_warm_trainval_overlap': 0,
            'selected_distribution': bank['selected'],
            'post_exclusion_distribution': manifest['distribution'],
            'test_limit': manifest['test_limit']}


def training_binding(directory, plan, candidate_path):
    protocol, trained, selection, validation = (read(directory / name) for name in
        ('protocol.json', 'training.json', 'selection.json', 'validation.json'))
    for name in ('protocol.json', 'training.json', 'selection.json', 'validation.json', 'model.nnue'):
        path = directory / name
        require(str(path) in plan['inputs'] and sha(path) == plan['inputs'][str(path)],
                'Training receipt is not bound to the match: ' + name)
    require(Path(candidate_path) == directory / 'model.nnue', 'Candidate model path differs')
    config = {'batch_size': 128, 'epochs': 500, 'input_bias_lr': .0002, 'input_scale': 32,
              'loss': 'mse', 'output_lr': .005, 'seed': 20261020}
    require(protocol['config'] == trained['config']
            and {key: value for key, value in protocol['config'].items() if key != 'candidate_id'} == config
            and protocol['config']['candidate_id'] == 'sf18-mse-stm-diverse-20261020',
            'MSE or optimizer conditions changed')
    require(protocol['guards'] == trained['guards']
            == {'patience': 5, 'eval_interval': 5, 'min_delta_cp2': 1.0}, 'Overfit guards changed')
    require(protocol['architecture'] == {'format': 3, 'piece_features': 768, 'relative_turn_features': 1,
                                         'hidden': 16, 'added_parameters': 0, 'changed': False}
            and trained['architecture_unchanged'] is True and trained['candidate_runs'] == 1
            and trained['loss'] == 'mse' and protocol['default_changed'] is False,
            'Architecture, candidate budget or stable default changed')
    warm_sha = '1b431332c5983d3a12e3fc3bdef9d85a55946e01806323d97b30c1cc6666e058'
    initial_path = Path(protocol['initial']['path'])
    warm_path = initial_path.parent / 'model.nnue'
    require(protocol['initial']['model_sha256'] == sha(warm_path) == warm_sha
            and protocol['initial']['selected_epoch'] == 15
            and protocol['initial']['sha256'] == sha(initial_path) == sha(directory / 'initial-floats.npz'),
            'The current guarded epoch-15 float checkpoint was not reused exactly')
    previous = read(initial_path.parent / 'training.json')
    require({key: value for key, value in previous['config'].items() if key != 'candidate_id'} == config
            and previous['guards'] == protocol['guards'], 'Warm and new optimizer/guard settings differ')
    for name, expected in protocol['input_sha256'].items():
        source = Path(name)
        snapshot = directory / 'source-snapshot' / source.relative_to(ROOT) if source.suffix == '.py' else source
        require(sha(snapshot) == expected, 'Executed source or frozen input changed: ' + name)
    dataset = Path(protocol['data']['directory'])
    require(sha(dataset / 'manifest.json') == protocol['data']['manifest_sha256'],
            'Dataset manifest differs from the training protocol')
    manifest = read(dataset / 'manifest.json')
    partitions, identities, families, coverage = {}, {}, {}, {}
    for name in ('train', 'tuning', 'test'):
        path = dataset / (name + '.jsonl')
        require(sha(path) == protocol['data']['file_sha256'][path.name], 'Dataset bytes changed: ' + name)
        expected = manifest['files'][path.name]
        require(sha(path) == (expected['sha256'] if isinstance(expected, dict) else expected),
                'Dataset manifest SHA differs: ' + name)
        rows = read_lines(path)
        require(len(rows) == manifest['rows'][name] == protocol['data']['rows'][name] > 0,
                'Partition size differs: ' + name)
        pieces, movers, buckets, sources = Counter(), Counter(), Counter(), Counter()
        identities[name], families[name] = set(), set()
        for row in rows:
            board = source_board(row)
            require(board.outcome(claim_draw=True) is None and type(row['target_white_cp']) is int
                    and abs(row['target_white_cp']) <= 1500
                    and row['teacher_white_cp'] == row['target_white_cp'], 'Invalid bounded Stockfish label')
            identities[name].add(row['feature_hash'])
            families[name].add(row['family_id'])
            count = len(board.piece_map())
            pieces[str(count)] += 1
            movers['white' if board.turn else 'black'] += 1
            buckets['25plus_pieces' if count >= 25 else '13to24_pieces' if count >= 13 else '2to12_pieces'] += 1
            sources[str(row.get('source_kind', row.get('source', 'unspecified')))] += 1
        require(len(identities[name]) == len(rows), 'Repeated/mirrored inputs within ' + name)
        partitions[name] = rows
        coverage[name] = {'rows': len(rows), 'families': len(families[name]), 'side_to_move': dict(movers),
                          'piece_count': dict(pieces), 'piece_count_buckets': dict(buckets), 'source_kinds': dict(sources)}
    for index, left in enumerate(partitions):
        for right in list(partitions)[index + 1:]:
            require(not (identities[left] & identities[right]) and not (families[left] & families[right]),
                    'Source family or mirrored feature crosses partitions: ' + left + '/' + right)
    require(coverage == protocol['data']['coverage'], 'Reported training coverage differs')
    data_audit = audit_expanded_data(dataset, partitions, plan)
    require(trained['selection'] == selection and selection['loss'] == 'mse'
            and selection['objective'] == 'held_out_integer_white_cp_mse'
            and selection['mode'] == 'validation_mse_early_stopping'
            and selection['validation_used_in_gradients'] is False, 'Tuning selection policy changed')
    require(trained['test_evaluations'] == 1 and trained['test_used_in_selection'] is False
            and trained['test_scored_after_saved_selection'] is True, 'Test use policy changed')
    evaluations = selection['validation_evaluations']
    require(evaluations and evaluations[0]['epoch'] == 0
            and selection['initial_validation'] == evaluations[0]
            and selection['last_validation'] == evaluations[-1], 'Epoch-zero/last tuning receipt differs')
    stopped = selection['stopped_epoch']
    require(selection['max_epochs'] == 500 and selection['eval_interval_epochs'] == 5
            and selection['patience_evaluations'] == 5 and selection['min_delta_cp2'] == 1.0
            and 1 <= stopped <= 500, 'Guard stopping conditions differ')
    require([row['epoch'] for row in evaluations] == [0] + [epoch for epoch in range(1, stopped + 1)
            if epoch % 5 == 0 or epoch == 500] and evaluations[-1]['epoch'] == stopped,
            'Tuning evaluation schedule differs')
    best = min(evaluations, key=lambda row: row['mse_cp2'])
    require(selection['selected_validation'] == best and selection['selected_epoch'] == best['epoch'],
            'Selected checkpoint is not the earliest minimum tuning integer MSE')
    reference, stale = evaluations[0]['mse_cp2'], 0
    for index, evaluation in enumerate(evaluations[1:], 1):
        require(all(math.isfinite(evaluation[key]) and evaluation[key] >= 0
                    for key in ('mse_cp2', 'rmse_cp', 'mae_cp')), 'Nonfinite tuning metric')
        if evaluation['mse_cp2'] < reference - 1.0:
            reference, stale = evaluation['mse_cp2'], 0
        else:
            stale += 1
        require(stale < 5 or index == len(evaluations) - 1, 'Training continued past patience exhaustion')
    require(selection['stop_reason'] == ('patience_exhausted' if stale >= 5 else 'max_epochs')
            and (stale >= 5 or stopped == 500) and selection['stopped_early'] == (stopped < 500),
            'Stopping reason differs')
    steps = math.ceil(len(partitions['train']) / 128)
    require([entry['epoch'] for entry in trained['history']] == list(range(1, stopped + 1))
            and all(entry['optimizer_steps'] == entry['epoch'] * steps for entry in trained['history'])
            and selection['optimizer_steps'] == stopped * steps, 'Training epoch/minibatch counts differ')
    sys.path.insert(0, str(ROOT / 'scripts'))
    import neural_stm as stm
    import neural_training as base
    model, warm = stm.load_model(candidate_path), stm.load_model(warm_path)
    with np.load(directory / 'float_weights.npz', allow_pickle=False) as archive:
        floats = {key: archive[key].copy() for key in stm.PARAMETER_KEYS}
    require(stm.model_checksum(stm.quantize(floats, 32)) == stm.model_checksum(model),
            'Saved restored float checkpoint differs from the actual integer model')
    require(trained['model_sha256'] == sha(candidate_path)
            and trained['model_changed_from_warm'] == (stm.model_checksum(model) != stm.model_checksum(warm)),
            'Candidate model identity differs')
    metrics = {}
    for name, rows in partitions.items():
        indices, targets, turns = stm.pack_records(rows)
        predictions = {label: stm.predict(net, indices, turns) for label, net in (('warm', warm), ('diverse', model))}
        actual = {'count': len(rows), **{label: base.error_metrics(values, targets)
                                       for label, values in predictions.items()}}
        counts = np.array([len(chess.Board(row['fen']).piece_map()) for row in rows])
        groups = {'white_to_move': turns == 0, 'black_to_move': turns == 1,
                  '25plus_pieces': counts >= 25, '13to24_pieces': (counts >= 13) & (counts <= 24),
                  '2to12_pieces': counts <= 12}
        actual['by_group'] = {group: {'count': int(mask.sum()), **{
            label: base.error_metrics(values[mask], targets[mask]) for label, values in predictions.items()}}
            for group, mask in groups.items() if np.any(mask)}
        metrics[name] = actual
    require(metrics == trained['metrics'], 'Recomputed full/phase/mover metrics differ')
    for receipt, label in (('initial_validation', 'warm'), ('selected_validation', 'diverse')):
        require({key: selection[receipt][key] for key in ('mse_cp2', 'rmse_cp', 'mae_cp')}
                == metrics['tuning'][label], 'Tuning selection metric differs from the actual model')
    require(validation['corpus_positions'] == sum(map(len, partitions.values()))
            and validation['special_positions'] == 46
            and validation['positions'] == validation['corpus_positions'] + 46
            and validation['python_native_full_incremental_mismatches'] == 0
            and validation['native_model_returncode'] == 0, 'Actual trained-model native validation failed')
    for name, expected in validation['input_sha256'].items():
        require(sha(name) == expected, 'Native validation input changed: ' + name)
    cached, chain = validation, []
    while 'reused_legacy_validation' in cached:
        require(cached['python_native_full_incremental_mismatches'] == 0
                and cached['native_model_returncode'] == 0, 'Cached native check failed')
        prior = cached['reused_legacy_validation']
        require(prior['path'] not in chain and sha(prior['path']) == prior['sha256'],
                'Cached validation chain is cyclic or changed')
        chain.append(prior['path'])
        cached = read(prior['path'])
    require(cached['legacy_old_new_python_mismatches'] == cached['python_native_full_incremental_mismatches'] == 0,
            'Original legacy/full/incremental compatibility check failed')
    return {'config': protocol['config'], 'guards': protocol['guards'], 'candidate_runs': 1,
            'architecture_unchanged': True, 'warm_model_sha256': warm_sha, 'data': data_audit,
            'selected_epoch': best['epoch'], 'stopped_epoch': stopped, 'stop_reason': selection['stop_reason'],
            'test_unused_by_this_fine_tune_and_selection': True,
            'test_is_new_independent_corpus': False, 'metrics_recomputed': metrics,
            'python_native_full_incremental_mismatches': 0, 'legacy_validation_chain': chain,
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
                            'One guarded MSE fine-tune on expanded data; unchanged v3 architecture and Stockfish teacher.',
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
