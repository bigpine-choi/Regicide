"""Read-only audit of one loss-position NNUE repair and a sixteen-game screen.

The unchanged independent match replay and v3 checksum parser are reused from
the diverse-data audit.  Training/data bindings are specific to this experiment
and are filled from its frozen receipts, never by invoking the engine or trainer.
"""
from datetime import datetime, timezone
from collections import Counter
import hashlib
import math
import os

for _thread_variable in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS'):
    os.environ[_thread_variable] = '1'

import chess
import chess.pgn
import numpy as np
import json
from pathlib import Path
import sys
import time

import audit_nnue_diversity_match as replay

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
read = replay.read
read_lines = replay.read_lines
sha = replay.sha
require = replay.require
model_binding = replay.model_binding


def partition_metrics(rows, warm, model):
    """Recompute unique and bounded-repeat MSE without importing the trainer."""
    import neural_stm as stm
    import neural_training as base
    indices, targets, turns = stm.pack_records(rows)
    weights = np.array([row['replay_weight'] for row in rows], dtype=np.int64)
    predictions = {name: stm.predict(net, indices, turns)
                   for name, net in (('warm', warm), ('repair', model))}
    metrics = {'count': len(rows), **{name: base.error_metrics(values, targets)
                                    for name, values in predictions.items()}}
    metrics['weighted'] = {'count': int(weights.sum()), **{
        name: base.error_metrics(np.repeat(values, weights), np.repeat(targets, weights))
        for name, values in predictions.items()}}
    counts = np.array([len(chess.Board(row['fen']).piece_map()) for row in rows])
    groups = {'white_to_move': turns == 0, 'black_to_move': turns == 1,
              'repair_replay': weights > 1, 'background': weights == 1,
              '25plus_pieces': counts >= 25,
              '13to24_pieces': (counts >= 13) & (counts <= 24), '2to12_pieces': counts <= 12}
    metrics['by_group'] = {name: {'count': int(mask.sum()), **{
        label: base.error_metrics(values[mask], targets[mask])
        for label, values in predictions.items()}} for name, mask in groups.items() if np.any(mask)}
    return metrics


def loss_case_row(game, case, history, role):
    board = chess.Board(game['initial_fen'])
    for text in history:
        move = chess.Move.from_uci(text)
        require(board.outcome(claim_draw=True) is None and move in board.legal_moves,
                'Illegal or postterminal loss-example history')
        board.push(move)
    identity = min(board.board_fen(), board.mirror().board_fen())
    pair = game['pair']
    return {'query_id': f"loss-{game['game']:02d}-{case['ply']:03d}-{role}",
            'fen': board.fen(en_passant='fen'), 'initial_fen': game['initial_fen'],
            'history': history, 'feature_hash': hashlib.sha256(identity.encode('ascii')).hexdigest(),
            'family_id': f'loss-pair-{pair:02d}', 'partition': 'train' if pair <= 6 else 'tuning',
            'kind': 'loss_replay', 'source_game': game['game'], 'source_ply': case['ply'],
            'source_pair': pair, 'source_role': role, 'played_move': case['move'],
            'candidate_sign': case['candidate_sign'],
            'source_search_cp': case['recorded_search_candidate_cp'],
            'target_white_cp': 0, 'replay_weight': 4}


def audit_loss_screen(screen, previous, warm):
    """Replay the old games and bind all thirty frozen error examples to searches."""
    import neural_stm as stm
    match = read(previous / 'games.json')
    require(screen['source_games_sha256'] == sha(previous / 'games.json')
            and screen['source_model_sha256'] == sha(previous / 'model.nnue'),
            'Failure diagnosis source identity differs')
    with (previous / 'games.pgn').open(encoding='utf-8') as stream:
        pgns = []
        while (game := chess.pgn.read_game(stream)) is not None:
            require(not game.errors, 'Old loss PGN parser error')
            pgns.append(game)
    require(len(pgns) == len(match['games']) == screen['games_replayed']
            == screen['pgn_games_replayed'] == 16, 'Old game replay count differs')
    games, states, legal = {}, {}, 0
    for game, pgn in zip(match['games'], pgns, strict=True):
        board = chess.Board(game['initial_fen'])
        boards = [board.copy(stack=True)]
        searches = {search['ply']: search for search in game['search_moves']}
        for ply, text in enumerate(game['uci_moves'], 1):
            move = chess.Move.from_uci(text)
            require(board.outcome(claim_draw=True) is None and move in board.legal_moves,
                    'Illegal or postterminal old game mainline')
            if ply in searches:
                search = searches[ply]
                require(search['fen_before'] == board.fen(en_passant='fen') and search['move'] == text,
                        'Old search FEN/move differs')
            board.push(move)
            boards.append(board.copy(stack=True))
            legal += 1
        outcome = board.outcome(claim_draw=True)
        require(outcome is not None and outcome.result() == game['result']
                and board.fen(en_passant='fen') == game['final_fen']
                and [move.uci() for move in pgn.mainline_moves()] == game['uci_moves'],
                'Old PGN/outcome differs')
        games[game['game']] = game
        states[game['game']] = boards, searches
    require(legal == screen['legal_moves_replayed'] and screen['pv_illegal'] == [],
            'Old replay move count/PV status differs')
    losses = [game['game'] for game in match['games'] if game['candidate_points'] == 0]
    require([loss['game'] for loss in screen['losses']] == losses
            and len(losses) == screen['candidate_losses'] == 10, 'Diagnosed losses differ')
    values = {chess.PAWN: 100, chess.KNIGHT: 320, chess.BISHOP: 330,
              chess.ROOK: 500, chess.QUEEN: 900, chess.KING: 0}

    def board_receipt(board, sign):
        material = sum(values[piece.piece_type] * (1 if piece.color else -1)
                       for piece in board.piece_map().values())
        cp = stm.quantized_white(board, warm)
        return {'fen': board.fen(en_passant='fen'), 'nnue_white_cp': cp,
                'nnue_candidate_cp': sign * cp, 'material_white_cp': material,
                'material_candidate_cp': sign * material, 'piece_count': len(board.piece_map()),
                'in_check': board.is_check()}

    cases = []
    for loss in screen['losses']:
        game = games[loss['game']]
        boards, searches = states[game['game']]
        sign = 1 if game['white'] == 'candidate' else -1
        require(len(loss['candidates']) == 3 and loss['candidate_color'] == ('white' if sign == 1 else 'black')
                and loss['result'] == game['result'] and loss['plies'] == game['plies'],
                'Loss metadata differs')
        all_priorities = []
        for ply, search in searches.items():
            cp = search['score'].get('cp')
            if search['role'] != 'candidate' or cp is None or cp < -800 or ply > 120:
                continue
            before, future = boards[ply - 1], boards[min(ply + 5, len(boards) - 1)]
            reply = searches.get(ply + 1)
            other = -reply['score']['cp'] if reply and reply['score'].get('cp') is not None else None
            drift = board_receipt(future, sign)['material_candidate_cp'] - board_receipt(before, sign)['material_candidate_cp']
            disagreement = cp - other if other is not None else 0
            leaf = before.copy(stack=True)
            for text in search['pv']:
                move = chess.Move.from_uci(text)
                require(move in leaf.legal_moves, 'Old candidate PV is illegal')
                leaf.push(move)
            leaf_score = board_receipt(leaf, sign)
            priority = (max(0, disagreement) + max(0, -drift)
                        + .5 * max(0, leaf_score['nnue_candidate_cp'] - leaf_score['material_candidate_cp'])
                        - 2 * max(0, ply - 60) - max(0, -cp - 200))
            all_priorities.append((priority, ply))
        selected = []
        for priority, ply in sorted(all_priorities, key=lambda entry: (-entry[0], entry[1])):
            if all(abs(ply - other) >= 6 for other in selected):
                selected.append(ply)
            if len(selected) == 3:
                break
        require([case['ply'] for case in loss['candidates']] == sorted(selected),
                'Top-three spaced error selection differs')
        for case in loss['candidates']:
            ply, search = case['ply'], searches[case['ply']]
            require(case['candidate_sign'] == sign and case['move'] == search['move']
                    and case['pv'] == search['pv'] and case['depth'] == search['depth']
                    and case['recorded_search_candidate_cp'] == search['score']['cp']
                    and case['priority'] == dict((p, value) for value, p in all_priorities)[ply],
                    'Loss case source search differs')
            expected = {'before': boards[ply - 1], 'after_played': boards[ply],
                        'after_reply': boards[min(ply + 1, len(boards) - 1)],
                        'six_plies_later': boards[min(ply + 5, len(boards) - 1)]}
            leaf = boards[ply - 1].copy(stack=True)
            for text in case['pv']:
                leaf.push(chess.Move.from_uci(text))
            expected['pv_leaf'] = leaf
            require(all(case[name] == board_receipt(board, sign) for name, board in expected.items()),
                    'Loss case static/material state differs')
            cases.append((game, case, game['uci_moves'][:ply - 1]))
    return cases, {'old_games_replayed': 16, 'old_mainline_plies': legal,
                   'loss_games': 10, 'frozen_cases': len(cases), 'selection_ranking_recomputed': True}


def audit_repair_data(dataset, partitions, plan):
    import neural_stm as stm
    manifest = read(dataset / 'manifest.json')
    require(str(dataset / 'manifest.json') in plan['inputs']
            and sha(dataset / 'manifest.json') == plan['inputs'][str(dataset / 'manifest.json')],
            'Loss repair data is not bound to the match')
    for field in ('background', 'generation_protocol'):
        require(sha(manifest[field]['path']) == manifest[field]['sha256'],
                'Loss repair data binding differs: ' + field)
    protocol = read(manifest['generation_protocol']['path'])
    require(protocol['maximum_queries'] == 150 and protocol['root_cases'] == 30
            and protocol['nodes_per_query'] == 50000 and protocol['max_abs_cp'] == 1500
            and protocol['roles'] == ['root', 'played', 'teacher_best', 'pv_leaf', 'reply']
            and protocol['replay_weight'] == 4 and protocol['loss'] == 'mse'
            and protocol['no_refill'] is True, 'Bounded loss repair generation policy differs')
    for name, expected in protocol['input_sha256'].items():
        source = Path(name)
        snapshot = dataset / 'source-snapshot' / source.relative_to(ROOT) if source.suffix == '.py' else source
        require(sha(snapshot) == expected, 'Loss repair source/input changed: ' + name)
    background_dir = Path(protocol['background'])
    background_manifest = read(background_dir / 'manifest.json')
    background = {name: read_lines(background_dir / (name + '.jsonl')) for name in partitions}
    require([len(background[name]) for name in partitions] == [1294, 263, 69],
            'Original background count differs')
    for name in partitions:
        require(sha(background_dir / (name + '.jsonl')) == background_manifest['files'][name + '.jsonl'],
                'Original background bytes changed')
    previous = background_dir.parent / 'attempt-001'
    warm = stm.load_model(previous / 'model.nnue')
    screen_paths = [Path(name) for name in protocol['input_sha256'] if Path(name).name == 'loss-screen.json']
    require(len(screen_paths) == 1, 'Loss screen binding is missing or ambiguous')
    cases, diagnosis = audit_loss_screen(read(screen_paths[0]), previous, warm)
    roots = [loss_case_row(game, case, history, 'root') for game, case, history in cases]
    require(roots == read(dataset / 'root-selected.json') and len(roots) == 30,
            'Frozen root rows differ from the actual lost games')
    root_labels, root_receipt = audit_teacher(dataset / 'roots-teacher', roots)
    root_queries = {query['source_query_id']: query
                    for query in read_lines(dataset / 'roots-teacher/teacher-queries.jsonl')}
    children = []
    for row, (game, case, history) in zip(roots, cases, strict=True):
        query = root_queries[row['query_id']]
        alternatives = {'played': history + [case['move']], 'pv_leaf': history + case['pv'],
                        'reply': game['uci_moves'][:case['ply'] + 1]}
        if query.get('bestmove_uci'):
            alternatives['teacher_best'] = history + [query['bestmove_uci']]
        children.extend(loss_case_row(game, case, values, role) for role, values in alternatives.items())
    background_features = {row['feature_hash'] for rows in background.values() for row in rows}
    feature_partitions = {}
    for row in roots + children:
        feature_partitions.setdefault(row['feature_hash'], set()).add(row['partition'])
    cross = {identity for identity, names in feature_partitions.items() if len(names) > 1}
    seen = {row['feature_hash'] for row in roots}
    selected, exclusions = [], Counter()
    for row in children:
        identity = row['feature_hash']
        if identity in background_features or identity in cross or identity in seen:
            exclusions['background' if identity in background_features else 'cross_split' if identity in cross else 'duplicate'] += 1
            continue
        selected.append(row)
        seen.add(identity)
    require(selected == read(dataset / 'child-selected.json')
            and dict(exclusions) == manifest['selection_exclusions']
            and len(roots) + len(selected) <= 150, 'Child selection or query budget differs')
    child_labels, child_receipt = audit_teacher(dataset / 'children-teacher', selected)
    accepted, identities, exclusions = [], set(), Counter()
    for batch, labels in (('roots', root_labels), ('children', child_labels)):
        for row in labels:
            identity = row['feature_hash']
            if identity in background_features or identity in cross or identity in identities:
                exclusions['background' if identity in background_features else 'cross_split' if identity in cross else 'duplicate'] += 1
                continue
            identities.add(identity)
            accepted.append({**row, 'teacher_batch': 'loss-' + batch})
    counts = dict(Counter(row['partition'] for row in accepted))
    require(dict(exclusions) == manifest['merge_exclusions'] and counts == manifest['new_repair_rows']
            and counts.get('train', 0) >= 12 and counts.get('tuning', 0) >= 4,
            'Accepted loss repair counts or exclusions differ')
    for name in partitions:
        require(partitions[name] == ([{**row, 'replay_weight': 1} for row in background[name]]
                + [row for row in accepted if row['partition'] == name]),
                'Background preservation, loss split or replay weights differ: ' + name)
    require({name: sum(row['replay_weight'] for row in rows) for name, rows in partitions.items()}
            == manifest['effective_weighted_rows'], 'Effective weighted sizes differ')
    require(manifest['selected_roots'] == len(roots) and manifest['selected_children'] == len(selected)
            and manifest['actual_teacher_queries'] == len(roots) + len(selected), 'Teacher query counts differ')
    evidence = []
    for query in [*root_queries.values(), *read_lines(dataset / 'children-teacher/teacher-queries.jsonl')]:
        cp = stm.quantized_white(chess.Board(query['fen']), warm)
        score = query.get('score', {})
        evidence.append({'query_id': query['source_query_id'], 'fen': query['fen'], 'nnue_white_cp': cp,
                         'stockfish_white_cp': score.get('white_cp'),
                         'stockfish_white_mate': score.get('mate_white'), 'status': query['status'],
                         'reasons': query['reasons'],
                         'abs_error_cp': abs(cp - score['white_cp']) if score.get('white_cp') is not None else None})
    require(evidence == read(dataset / 'teacher-diagnosis.json'), 'NNUE/teacher error evidence differs')
    return {'loss_diagnosis': diagnosis, 'roots_teacher': root_receipt, 'children_teacher': child_receipt,
            'background_rows': {name: len(rows) for name, rows in background.items()},
            'new_repair_rows': counts, 'effective_weighted_rows': manifest['effective_weighted_rows'],
            'test69_background_preserved_and_unweighted': True, 'teacher_error_evidence_recomputed': True}


def audit_teacher(directory, selected):
    report = read(directory / 'teacher-report.json')
    queries = read_lines(directory / 'teacher-queries.jsonl')
    labels = read_lines(directory / 'labels.jsonl')
    teacher_sha = '9bde420202717ce083412027fbfb8c5c935b537591d712be8a8a8bae92f6e8d6'
    options = {'Threads': 1, 'Hash': 16, 'Skill Level': 20, 'UCI_LimitStrength': False,
               'SyzygyPath': '', 'MultiPV': 1, 'UCI_AnalyseMode': True}
    require(report['binary_sha256'] == sha(report['engine_path']) == teacher_sha,
            'Stockfish 18 teacher binary differs')
    require(report['requested_nodes'] == 50000 and report['max_abs_cp'] == 1500
            and report['options'] == options, 'Teacher search/label policy differs')
    for name, expected in report['files'].items():
        require(sha(directory / name) == expected, 'Teacher output changed: ' + name)
    require(len(selected) == len(queries) == report['requested_rows'] <= 150,
            'Teacher query count differs or exceeds the bounded data budget')
    accepted, exclusions, total_nodes = [], Counter(), 0
    for index, (row, query) in enumerate(zip(selected, queries, strict=True)):
        prefix = f'Teacher query {index + 1}: '
        board = replay.source_board(row)
        require(query['index'] == index and query['query_id'] == f'sf-{index + 1:06d}'
                and query['source_query_id'] == row['query_id'] and query['fen'] == row['fen']
                and query['family_id'] == row['family_id'] and query['partition'] == row['partition']
                and query['history_plies'] == len(row['history']), prefix + 'Source metadata differs')
        require(query['binary_sha256'] == teacher_sha and query['options'] == options
                and query['requested_nodes'] == 50000 and query['engine_id'] == report['engine_id'],
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
            pv_board = board.copy(stack=True)
            for text in query['pv_uci']:
                move = chess.Move.from_uci(text)
                require(move in pv_board.legal_moves, prefix + 'Illegal teacher PV')
                pv_board.push(move)
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
                    'exclusion_reasons': dict(exclusions), 'requested_nodes': 50000,
                    'observed_completed_nodes': total_nodes, 'teacher_binary_sha256': teacher_sha,
                    'score_pov': 'white', 'source_history_and_pvs_replayed': True}



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
            and protocol['config']['candidate_id'] == 'sf18-mse-stm-loss-repair-20261020',
            'MSE or optimizer conditions changed')
    require(protocol['guards'] == trained['guards']
            == {'patience': 5, 'eval_interval': 5, 'min_delta_cp2': 1.0}, 'Overfit guards changed')
    require(protocol['architecture'] == {'format': 3, 'piece_features': 768, 'relative_turn_features': 1,
                                         'hidden': 16, 'added_parameters': 0, 'changed': False}
            and trained['architecture_unchanged'] is True and trained['candidate_runs'] == 1
            and trained['loss'] == 'mse' and protocol['default_changed'] is False,
            'Architecture, candidate budget or stable default changed')
    warm_sha = 'c73b510cd83ca221829eb9e1bf781b86d28c5b9c47ed922d8f9fa30e08385c86'
    initial_path = Path(protocol['initial']['path'])
    warm_path = initial_path.parent / 'model.nnue'
    require(protocol['initial']['model_sha256'] == sha(warm_path) == warm_sha
            and protocol['initial']['selected_epoch'] == 20
            and protocol['initial']['sha256'] == sha(initial_path) == sha(directory / 'initial-floats.npz'),
            'The current guarded epoch-20 float checkpoint was not reused exactly')
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
            board = replay.source_board(row)
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
    data_audit = audit_repair_data(dataset, partitions, plan)
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
    steps = math.ceil(sum(row['replay_weight'] for row in partitions['train']) / 128)
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
    require(protocol['replay_policy'] == trained['replay_policy']
            and protocol['replay_policy']['maximum_integer_weight'] == 4
            and protocol['replay_policy']['background_weight'] == 1
            and protocol['replay_policy']['repair_weight'] == 4
            and protocol['replay_policy']['weighted_rows'] == manifest['effective_weighted_rows'],
            'Weighted MSE replay policy differs')
    metrics = {name: partition_metrics(rows, warm, model) for name, rows in partitions.items()}
    require(metrics == trained['metrics'], 'Recomputed unique/weighted/group metrics differ')
    for receipt, label in (('initial_validation', 'warm'), ('selected_validation', 'repair')):
        require({key: selection[receipt][key] for key in ('mse_cp2', 'rmse_cp', 'mae_cp')}
                == metrics['tuning']['weighted'][label],
                'Weighted tuning metric differs from the actual restored model')
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
    """Reuse the prior direct, runner-independent sixteen-game replay."""
    previous = replay.training_binding
    replay.training_binding = training_binding
    try:
        output = replay.audit(directory)
    finally:
        replay.training_binding = previous
    output['opening_separation'] = audit_openings(directory)
    output['limitations'] = [
        'Recorded option acceptance is not independent UCI state readback.',
        'Binary/source identity is bound by hashes and prior build receipts; this audit does not rebuild.',
        'One guarded MSE fine-tune; unchanged v3 architecture and Stockfish teacher.',
        'Sixteen paired games are a descriptive screen, not Elo or automatic promotion.',
        'No process CPU accounting, system-load isolation, extra training or match retry is performed.',
    ]
    return output


def audit_openings(directory):
    """Check the declared first-eight starts against every repair/background input."""
    plan, protocol = read(directory / 'plan.json'), read(directory / 'protocol.json')
    command = plan['command']
    opening_path = Path(command[command.index('--openings') + 1])
    opening_file = read(opening_path)
    source = read(opening_file['source'])
    require(sha(opening_file['source']) == opening_file['source_sha256'],
            'New opening source SHA differs')
    dataset = Path(protocol['data']['directory'])
    identities = {row['feature_hash'] for name in ('train', 'tuning', 'test')
                  for row in read_lines(dataset / (name + '.jsonl'))}
    previous = ROOT / 'docs/checks/2026-10-22-nnue-diversity/attempt-001'
    old_match = read(previous / 'games.json')
    prefixes = [opening['moves'] for opening in old_match['selected_openings']
                if len(opening['moves']) >= 4]
    for game in old_match['games']:
        board = chess.Board(game['initial_fen'])
        for text in [None, *game['uci_moves']]:
            if text is not None:
                board.push(chess.Move.from_uci(text))
            identity = min(board.board_fen(), board.mirror().board_fen())
            identities.add(hashlib.sha256(identity.encode('ascii')).hexdigest())
    expected, skipped = [], []
    seen = set()
    for opening in source['openings']:
        board = chess.Board()
        for text in opening['moves']:
            move = chess.Move.from_uci(text)
            require(board.outcome(claim_draw=True) is None and move in board.legal_moves,
                    'New opening source has illegal or terminal moves')
            board.push(move)
        identity = min(board.board_fen(), board.mirror().board_fen())
        feature = hashlib.sha256(identity.encode('ascii')).hexdigest()
        overlap = feature in identities or any(opening['moves'][:len(prefix)] == prefix for prefix in prefixes)
        if overlap:
            skipped.append(opening['name'])
            continue
        require(feature not in seen, 'New comparison starts repeat mirrored features')
        seen.add(feature)
        expected.append({'name': opening['name'], 'moves': opening['moves'],
                         'source_family_id': opening['family_id']})
        if len(expected) == 8:
            break
    require(expected == opening_file['openings'] and len(expected) == 8,
            'Declared comparison starts are not the first eight separated source positions')
    return {'starts': 8, 'source_sha256': opening_file['source_sha256'],
            'source_order_selection_recomputed': True,
            'mirrored_inputs_disjoint_from_current_data_and_diagnosed_game_states': True,
            'no_diagnosed_opening_prefix_of_at_least_four_plies': True,
            'excluded_source_names': skipped,
            'independent_unseen_opening_distribution_claim': False}


def main():
    directory = Path(sys.argv[1]).resolve()
    started = time.perf_counter()
    output = {
        'schema_version': 1,
        'auditor': 'Independent direct python-chess replay; no runner imports or engine invocation',
        'created_utc': datetime.now(timezone.utc).isoformat(),
        'auditor_source_sha256': sha(__file__),
        'reused_match_auditor_sha256': sha(Path(replay.__file__)),
    }
    try:
        output['raw_sha256'] = {
            name: sha(directory / name) for name in ('plan.json', 'games.json', 'games.pgn')
        }
        output.update(audit(directory))
    except Exception as exc:
        output.update(status='FAIL', error=f'{type(exc).__name__}: {exc}')
    output['audit_elapsed_seconds'] = time.perf_counter() - started
    with (directory / 'peer-audit.json').open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(output, stream, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        stream.write('\n')
    print(json.dumps({key: output[key] for key in ('status', 'audit_elapsed_seconds')}
                     | {key: output[key] for key in
                        ('games', 'totals', 'candidate_wdl', 'verified_input_count', 'error')
                        if key in output}))
    return 0 if output['status'] == 'PASS' else 1


if __name__ == '__main__':
    sys.exit(main())
