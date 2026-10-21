"""Authenticate default-search support pairs while preserving older data receipts.

The new teacher root-child gaps are frozen-search surrogates. They are not
literal Stockfish evaluations of the recorded native support leaves.
"""
from __future__ import annotations

import json
from pathlib import Path

import chess

import neural_stm as stm
import neural_training as base
import nnue_choice_pairs as previous
from prepare_neural_huber import read, require, sha

PARTITIONS = previous.PARTITIONS
TARGET_SEMANTICS = previous.TARGET_SEMANTICS
DEFAULT_FLAGS = ('nnue', 'nnue_incremental', 'quiescence', 'transposition', 'pvs', 'lmr',
                 'fast_moves', 'quiet_ordering', 'quiescence_checks', 'see_ordering',
                 'positional', 'king_pawn_shield', 'pawn_structure')


def validate_pair(pair, warm_model, *, model_sha256=None, teacher_sha256=None):
    """Return root/child/native/teacher path identities after independent replay."""
    require(isinstance(pair, dict) and pair.get('kind') == 'live_search_backed_gap', 'Unknown live pair kind')
    partition, family = pair.get('partition'), pair.get('family_id')
    require(partition in ('train', 'tuning') and isinstance(family, str) and family, 'Live source family/partition missing')
    require(not pair.get('critical_refutation') or partition == 'train', 'Critical refutation roots must stay TRAIN only')
    require(isinstance(pair.get('root_id'), str) and pair['root_id'], 'Live root id missing')
    depth = previous._integer(pair.get('branch_depth'), 'Live branch depth invalid', minimum=1, maximum=4)
    decision = pair.get('depth_decision', {})
    require(decision.get('requested_nodes') == 10000 and decision.get('branch_depth') == depth,
            'Live root decision budget/depth differs')
    if decision.get('policy') == 'default_last_completed_depth':
        require(decision.get('default_completed_depth') == depth, 'Live default horizon differs')
    else:
        require(pair.get('critical_refutation') is True
                and decision.get('policy') == 'critical_parent_completed_depth_minus_one'
                and decision.get('parent_completed_depth') == depth + 1,
                'Unknown live depth policy')
    root = chess.Board(pair['initial_fen'])
    require(root.is_valid(), 'Invalid live initial position')
    history = pair.get('root_history')
    previous._play(root, history, 'Illegal live root history')
    previous._fen_equal(root, pair.get('root_fen'), 'Live root replay differs')
    require(root.outcome(claim_draw=True) is None, 'Live root is terminal or claimable')
    sign = previous._integer(pair.get('root_sign'), 'Live root sign invalid')
    require(sign == (1 if root.turn else -1) and pair.get('move_a') != pair.get('move_b'), 'Live move/sign differs')
    identities, leaves = {base.feature_identity(root)}, []
    for side in ('a', 'b'):
        row = pair[side]
        require(row.get('target_semantics') == TARGET_SEMANTICS and 'teacher_white_cp' not in row,
                'Live target must explicitly remain a root-child surrogate')
        target = previous._integer(row.get('target_white_cp'), 'Live target invalid', minimum=-1500, maximum=1500)
        require(row.get('teacher_root_child_white_cp') == target and row.get('partition') == partition
                and row.get('family_id') == family, 'Live target source differs')
        forced = pair['move_' + side]
        child = root.copy(stack=True)
        previous._play(child, [forced], 'Illegal live root move', identities=identities, ongoing=True)
        child_record = row.get('root_child', {})
        child_history = history + [forced]
        require(child_record.get('history') == child_history, 'Live teacher child history differs')
        previous._fen_equal(child, child_record.get('fen'), 'Live teacher child FEN differs')
        teacher = child_record.get('teacher_receipt')
        previous._teacher(teacher, child, child_history, partition, family, target, identities)
        require(teacher_sha256 is None or teacher.get('binary_sha256') == teacher_sha256, 'Live teacher binary differs')
        support = row.get('support_receipt', {})
        require(support.get('accepted') is True and support.get('reason') == 'observed_completed_pv_support',
                'Live support not accepted')
        require(support.get('initial_fen') == pair['initial_fen'] and support.get('root_history') == history,
                'Live native root history differs')
        previous._fen_equal(root, support.get('root_fen'), 'Live native root FEN differs')
        require(support.get('forced_root_move') == forced and support.get('depth_limit') == depth
                and support.get('nodes_limit') == 10000, 'Live native forced move/budget differs')
        require(isinstance(support.get('model_sha256'), str) and len(support['model_sha256']) == 64
                and (model_sha256 is None or support['model_sha256'] == model_sha256), 'Live support model SHA differs')
        options = support.get('options', {})
        require(all(options.get(key) is True for key in DEFAULT_FLAGS)
                and options.get('full_window_forced_root') is True and options.get('hash_mb') == 16,
                'Live native default search options differ')
        search = support.get('last_completed_iteration', {})
        require(search.get('depth') == depth and search.get('stopped') is False,
                'Live requested iteration did not complete')
        complete_nodes = previous._integer(search.get('nodes'), 'Live completed nodes invalid', minimum=1, maximum=10000)
        final = support.get('search', {})
        previous._integer(final.get('nodes'), 'Live total nodes invalid', minimum=complete_nodes, maximum=10000)
        require(final.get('depth') == depth and type(final.get('stopped')) is bool, 'Live published iteration differs')
        for field in ('score_cp', 'white_score_cp', 'bestmove', 'pv'):
            require(final.get(field) == search.get(field), 'Live incomplete tail changed completed result')
        pv = search.get('pv')
        require(isinstance(pv, list) and len(pv) >= depth and pv[0] == forced
                and search.get('bestmove') == forced, 'Live forced PV differs')
        leaf = root.copy(stack=True)
        previous._play(leaf, pv, 'Illegal live native PV', identities=identities, ongoing=True)
        require(not leaf.is_check(), 'Live support endpoint is checked')
        witness = support.get('support_leaf', {})
        require(witness.get('history') == history + pv and witness.get('path_from_root') == pv
                and row.get('history') == history + pv, 'Live support full path differs')
        previous._fen_equal(leaf, witness.get('fen'), 'Live witness FEN differs')
        previous._fen_equal(leaf, row.get('fen'), 'Live leaf FEN differs')
        require(witness.get('iteration_depth') == depth and witness.get('ply') == len(pv)
                and witness.get('qply') == len(pv) - depth
                and witness.get('phase') == 'quiescence_stand_pat', 'Live witness phase/depth differs')
        require(witness.get('turn') == ('white' if leaf.turn else 'black')
                and witness.get('history_size') == len(history + pv), 'Live witness turn/history differs')
        repetitions = 1
        while leaf.is_repetition(repetitions + 1):
            repetitions += 1
        require(witness.get('repetition_count') == repetitions
                and all(witness.get(key) is False for key in ('in_check', 'terminal', 'current_draw_claim', 'draw_claim_adjusted')),
                'Live witness check/draw/repetition differs')
        total = previous._integer(support.get('observations_count'), 'Live observation count invalid', minimum=1, maximum=10000)
        completed = previous._integer(support.get('completed_observations_count'), 'Live completed observations invalid', minimum=1, maximum=total)
        index = previous._integer(support.get('support_observation_index'), 'Live witness index invalid', minimum=0, maximum=completed - 1)
        require(support.get('support_observation_index_base') == 0 and witness.get('evaluation_index') == index + 1,
                'Live witness comes from incomplete tail or wrong invocation')
        white = stm.quantized_white(leaf, warm_model)
        raw = white if leaf.turn else -white
        require(type(witness.get('raw_score_cp')) is int and witness.get('raw_score_cp') == raw
                and witness.get('score_cp') == raw and witness.get('white_score_cp') == white,
                'Live support full-refresh value differs or clamped')
        require(type(search.get('score_cp')) is int and search['score_cp'] == sign * white
                and search.get('white_score_cp') == white, 'Live leaf does not support completed root score')
        identity = base.feature_identity(leaf)
        require(row.get('feature_hash') == identity, 'Live leaf feature hash differs')
        leaves.append(identity)
    require(leaves[0] != leaves[1], 'Live pair has identical canonical inputs')
    require(type(pair.get('target_gap_white_cp')) is int and pair['target_gap_white_cp'] ==
            pair['a']['target_white_cp'] - pair['b']['target_white_cp'], 'Live target gap differs')
    return identities


def existing_features(pairs, rows, manifest):
    """Include all historical teacher/native PVs, not just their endpoints."""
    identities = {part: {base.feature_identity(chess.Board(row['fen'])) for row in rows[part]} for part in PARTITIONS}
    families = {part: {row['family_id'] for row in rows[part]} for part in PARTITIONS}
    warm = stm.load_model(manifest['warm_model']['path'])
    for part, group in pairs.items():
        for pair in group:
            identities[part].update(previous.validate_pair(pair, warm, model_sha256=manifest['warm_model']['sha256'])
                                    if pair.get('kind') == 'search_backed_gap' else previous._old_features(pair))
            families[part].add(pair['family_id'])
    return identities, families


def validate_decision(pair, decision, model_sha256):
    """Bind the training horizon/moves to the separately persisted root search."""
    root = decision.get('root', {})
    require(root.get('query_id') == pair['root_id'] and root.get('initial_fen') == pair['initial_fen']
            and root.get('history') == pair['root_history'] and root.get('fen') == pair['root_fen']
            and root.get('partition') == pair['partition'] and root.get('family_id') == pair['family_id'],
            'Live default decision source differs')
    require(decision.get('depth_decision') == pair['depth_decision']
            and decision.get('teacher_move') == pair['move_a'] and decision.get('student_move') == pair['move_b'],
            'Live depth/move decision differs')
    receipt = decision.get('teacher_recommendation_receipt', {})
    require(receipt.get('status') == 'accepted' and receipt.get('reasons') == []
            and receipt.get('bestmove_uci') == pair['move_a'], 'Live root recommendation receipt differs')
    previous._fen_equal(chess.Board(pair['root_fen']), receipt.get('fen'), 'Live recommendation FEN differs')
    native = decision.get('native_receipt', {})
    require(native.get('initial_fen') == pair['initial_fen'] and native.get('root_history') == pair['root_history']
            and native.get('root_fen') == pair['root_fen'] and native.get('forced_root_move') is None
            and native.get('nodes_limit') == 10000 and native.get('model_sha256') == model_sha256,
            'Live unrestricted decision source/model/budget differs')
    options = native.get('options', {})
    require(all(options.get(k) is True for k in DEFAULT_FLAGS) and options.get('hash_mb') == 16
            and options.get('full_window_forced_root') is False, 'Live unrestricted default profile differs')
    expected_limit = pair['branch_depth'] if pair['depth_decision']['policy'] == 'critical_parent_completed_depth_minus_one' else 64
    complete = native.get('last_completed_iteration', {})
    require(native.get('depth_limit') == expected_limit and complete.get('depth') == pair['depth_decision']['default_completed_depth']
            and complete.get('stopped') is False and complete.get('bestmove') == pair['move_b'],
            'Live unrestricted completed horizon/choice differs')
    require(isinstance(complete.get('pv'), list) and complete['pv'] and complete['pv'][0] == pair['move_b'],
            'Live unrestricted completed PV differs')
    board = chess.Board(pair['initial_fen'])
    previous._play(board, pair['root_history'] + complete['pv'], 'Illegal live unrestricted decision PV')
    previous._integer(complete.get('nodes'), 'Live unrestricted completed nodes invalid', minimum=1, maximum=10000)


def load_pairs(dataset, rows, manifest):
    """Keep all old pairs and authenticate new-model support separately."""
    dataset = Path(dataset)
    pairs, hashes = previous.load_pairs(dataset, rows, manifest)
    identities, families = existing_features(pairs, rows, manifest)
    ref = manifest.get('live_warm_model', {})
    path = Path(ref.get('path', ''))
    require(path.is_file() and sha(path) == ref.get('sha256'), 'Live warm model bytes differ')
    warm = stm.load_model(path)
    gate = manifest.get('live_gate', {})
    block = Path(gate.get('blocked_features', ''))
    require(block.is_file() and sha(block) == gate.get('blocked_features_sha256'), 'Live reserved gate bytes differ')
    blocked_rows = read(block)
    require(isinstance(blocked_rows, list) and all(isinstance(h, str) and len(h) == 64 for h in blocked_rows), 'Live gate features invalid')
    blocked, keys = set(blocked_rows), set()
    for part, group in pairs.items():
        keys.update((part, p['root_id'], tuple(sorted((p['move_a'], p['move_b'])))) for p in group)
    teacher_sha = manifest.get('live_teacher_sha256')
    require(isinstance(teacher_sha, str) and len(teacher_sha) == 64, 'Live teacher binding missing')
    decision_path = dataset / 'student-choices.json'
    require(sha(decision_path) == manifest.get('live_decisions_sha256'), 'Live root decision bytes differ')
    hashes['student-choices.json'] = manifest['live_decisions_sha256']
    decision_rows = read(decision_path)
    decisions = {row['root']['query_id']: row for row in decision_rows}
    require(len(decisions) == len(decision_rows), 'Duplicate live root decisions')
    for part in ('train', 'tuning'):
        name = f'pairs-live-{part}.jsonl'
        expected = manifest.get('live_pair_files', {}).get(name)
        require(isinstance(expected, str) and sha(dataset / name) == expected, 'Live pair bytes differ')
        hashes[name] = expected
        records = [json.loads(line) for line in (dataset / name).read_text(encoding='utf-8').splitlines()]
        require(manifest.get('live_pair_rows', {}).get(part) == len(records), 'Live pair count differs')
        for pair in records:
            require(pair.get('partition') == part, 'Live file partition differs')
            key = (part, pair['root_id'], tuple(sorted((pair['move_a'], pair['move_b']))))
            require(key not in keys, 'Duplicate historical/live comparison')
            keys.add(key)
            require(pair['root_id'] in decisions, 'Missing live root decision')
            validate_decision(pair, decisions[pair['root_id']], ref['sha256'])
            identities[part].update(validate_pair(pair, warm, model_sha256=ref['sha256'], teacher_sha256=teacher_sha))
            families[part].add(pair['family_id'])
        pairs[part] = [*pairs[part], *records]
        require(manifest.get('live_combined_pair_rows', {}).get(part) == len(pairs[part]), 'Live combined pair count differs')
    require(not (dataset / 'pairs-live-test.jsonl').exists() and 'pairs-live-test.jsonl' not in manifest.get('live_pair_files', {}),
            'Test cannot have live optimization pairs')
    for index, left in enumerate(PARTITIONS):
        require(not identities[left] & blocked, 'Historical/live features overlap fresh reserved gate')
        for right in PARTITIONS[index + 1:]:
            require(not identities[left] & identities[right], 'Historical/live/PV features cross partitions')
            require(not families[left] & families[right], 'Historical/live families cross partitions')
    return pairs, hashes
