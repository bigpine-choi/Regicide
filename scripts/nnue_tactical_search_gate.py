"""One untouched move-choice screen for two searches using one frozen NNUE.

Reservation is position-only. Sealing requires two immutable binaries, completed
correctness and known-position checks, and an experiment protocol. No fitting,
refill, binary reselection, gate retry, or match after a failed screen is allowed.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import time

import chess
import chess.engine

import neural_stm as stm
import neural_training as base
import nnue_generalization_gate as previous
import nnue_head_repair_gate as exposure
from stockfish_teacher import label_rows, source_board

ROOT, OPENINGS = previous.ROOT, previous.OPENINGS
MODEL = ROOT / 'docs/checks/2026-10-22-nnue-head-repair/attempt-001/model.nnue'
MODEL_SHA = '43958337348ce3d16397ed63ffadcea9bbe9b97473e61489653ffdede02eb5ac'
CANDIDATE_PROFILE = ROOT / 'config/engine-profiles/nnue-tactical-search.json'
INDICES = list(range(0, 32, 2))
EXTENSION_PLIES = (4, 6, 8, 10)
TEACHER_DEPTH = 10
CRITERIA = deepcopy(previous.CRITERIA)
sha, read, write, require = previous.sha, previous.read, previous.write, previous.require
root_features = previous.root_features


def search_options():
    common = dict(read(ROOT / 'config/engine-profiles/classical.json')['options'],
                  UseNNUEEvaluation=True, NeuralModelFile=str(MODEL))
    return {'warm': common, 'candidate': dict(common, UseQuiescenceThreats=True)}


def select_roots(exposed, *, openings=None, indices=None):
    """Choose the second quiet UCI move, avoiding repetitions, before outcomes."""
    openings = read(OPENINGS)['openings'] if openings is None else openings
    indices = INDICES if indices is None else indices
    roots, excluded, blocked = [], [], set()
    for index in indices:
        opening = openings[index]
        initial = opening.get('initial_fen', chess.STARTING_FEN)
        board, original = chess.Board(initial), list(opening['moves'])
        visited = {base.feature_identity(board)}
        for text in original:
            require(chess.Move.from_uci(text) in board.legal_moves, 'Illegal source opening')
            board.push_uci(text)
            visited.add(base.feature_identity(board))
        require(board.fen(en_passant='fen') == chess.Board(opening['fen_after_opening']).fen(en_passant='fen'),
                'Opening FEN differs from complete history')
        extension, overlap, exhausted = [], set(), False
        for length in EXTENSION_PLIES:
            while len(extension) < length:
                choices = []
                for move in sorted(board.legal_moves, key=lambda value: value.uci()):
                    if board.is_capture(move) or board.gives_check(move):
                        continue
                    child = board.copy(stack=False)
                    child.push(move)
                    if base.feature_identity(child) not in visited:
                        choices.append(move)
                if not choices:
                    exhausted = True
                    break
                move = choices[min(1, len(choices) - 1)]
                extension.append(move.uci())
                board.push(move)
                visited.add(base.feature_identity(board))
            if exhausted:
                excluded.append({'index': index, 'extension_plies': length,
                                 'reason': 'no_nonrepeating_quiet_continuation'})
                break
            features = root_features(board)
            overlap = features & (exposed | blocked)
            if not overlap:
                break
            excluded.append({'index': index, 'extension_plies': length,
                             'reason': 'root_or_legal_child_exposed_or_reserved',
                             'overlap_features': sorted(overlap)})
        if exhausted or overlap:
            continue
        roots.append({'query_id': f'tactical-search-gate-opening-{index:02d}',
                      'source_index': index, 'source_path': str(OPENINGS),
                      'family_id': opening['family_id'], 'cluster_id': opening['cluster_id'],
                      'kind': 'quiet_extended_tactical_search_gate', 'partition': 'gate',
                      'initial_fen': initial, 'history': original + extension,
                      'source_opening_history': original,
                      'deterministic_extension_uci': extension,
                      'fen': board.fen(en_passant='fen'),
                      'feature_hash': base.feature_identity(board),
                      'blocked_feature_hashes': sorted(features), 'target_white_cp': 0})
        blocked.update(features)
    require(len({row['family_id'] for row in roots}) == len(roots), 'Gate families repeat')
    return roots, excluded, blocked


def reserve(directory):
    directory = Path(directory).resolve()
    require(not directory.exists(), 'Refuse to replace a reservation')
    require(sha(MODEL) == MODEL_SHA, 'Frozen NNUE differs')
    exposed, paths = exposure.exposure_inventory(directory=directory)
    roots, excluded, blocked = select_roots(exposed)
    require(len(roots) == 16, 'Require sixteen untouched fixed families; no outcome-driven refill')
    directory.mkdir(parents=True)
    write(directory / 'frozen-roots.json', roots)
    write(directory / 'blocked-features.json', sorted(blocked))
    bindings = [Path(__file__).resolve(), Path(previous.__file__).resolve(),
                Path(exposure.__file__).resolve(), ROOT / 'tests/nnue_tactical_search_gate_tests.py',
                OPENINGS, MODEL, previous.PROFILE, ROOT / 'config/engine-profiles/classical.json',
                ROOT / 'scripts/stockfish_teacher.py', ROOT / 'scripts/neural_training.py',
                ROOT / 'scripts/neural_stm.py', *paths]
    result = {'schema_version': 1, 'created_utc': datetime.now(timezone.utc).isoformat(),
              'selection': 'Fixed even M2 source indices0..30. At each ply choose second UCI-sorted legal quiet move whose canonical feature has not appeared in its history (first if only one). Exactly4plies, extend same deterministic path to6/8/10 only for recorded exposure overlap. No model, teacher or outcome input and no other refill.',
              'indices': INDICES, 'extension_plies': list(EXTENSION_PLIES),
              'reserved_roots': len(roots), 'excluded': excluded,
              'exposure_files': len(paths), 'exposure_unique_features': len(exposed),
              'exposure_policy': 'All recorded neural root/FEN/wrapped contexts, complete history prefixes, teacher/native/development PV prefixes, and every prior gate root/legal child, including consumed head-repair16; canonical color/rank-mirror feature identity.',
              'limitations': 'Source opening families were previously observed. These16 root positions and every legal child are absent from all recorded learning/selection/teacher/probe positions. This is a small move-choice screen, not independent Elo evidence.',
              'criteria': deepcopy(CRITERIA), 'native_nodes': previous.NODES,
              'teacher_nodes': previous.TEACHER_NODES, 'teacher_depth': TEACHER_DEPTH,
              'max_root_teacher_queries': len(roots), 'max_child_teacher_queries': 2 * len(roots),
              'model_path': str(MODEL), 'model_sha256': MODEL_SHA,
              'comparison': 'Different immutable search binaries; one identical frozen NNUE; candidate adds only UseQuiescenceThreats=true to common options; equal10000nodes',
              'frozen_roots_sha256': sha(directory / 'frozen-roots.json'),
              'blocked_features_sha256': sha(directory / 'blocked-features.json'),
              'input_sha256': {str(path): sha(path) for path in bindings},
              'gate_source_path': str(Path(__file__).resolve()),
              'no_test_driven_retraining_or_retries': True,
              'native_build_binding': 'Pending old/new binary, known-position pass and correctness seal'}
    write(directory / 'reservation.json', result)
    return result


def authenticate_reservation(directory):
    directory = Path(directory).resolve()
    reservation = read(directory / 'reservation.json')
    require(reservation['criteria'] == CRITERIA, 'Frozen criteria differ')
    require(reservation['native_nodes'] == previous.NODES and
            reservation['teacher_nodes'] == previous.TEACHER_NODES and
            reservation['teacher_depth'] == TEACHER_DEPTH, 'Frozen search budgets differ')
    require(reservation['model_path'] == str(MODEL) and reservation['model_sha256'] == MODEL_SHA,
            'Frozen NNUE differs')
    require(sha(directory / 'frozen-roots.json') == reservation['frozen_roots_sha256'], 'Reserved roots changed')
    require(sha(directory / 'blocked-features.json') == reservation['blocked_features_sha256'], 'Blocked features changed')
    for path, digest in reservation['input_sha256'].items():
        require(sha(path) == digest, 'Frozen reservation input changed: ' + path)
    return directory, reservation, read(directory / 'frozen-roots.json')


def seal(directory, old_binary, new_binary, correctness, known_positions, protocol):
    directory, reservation, roots = authenticate_reservation(directory)
    require(not (directory / 'plan.json').exists(), 'Build seal already exists; no reselection')
    old_binary, new_binary, correctness, known_positions, protocol = map(
        lambda path: Path(path).resolve(), (old_binary, new_binary, correctness, known_positions, protocol))
    require(old_binary != new_binary and sha(old_binary) != sha(new_binary), 'Search binaries must differ')
    correct, known = read(correctness), read(known_positions)
    require(correct.get('completed') is True and correct.get('passed') is True and
            correct.get('native_mismatches') == 0, 'Completed correctness pass required')
    require(known.get('completed') is True and known.get('known_position_passed') is True,
            'Problem-position move-choice pass required before fresh labels')
    for receipt in (correct, known):
        require(receipt.get('model_sha256') == MODEL_SHA, 'Prerequisite receipt uses another model')
        require(receipt.get('old_binary_sha256') == sha(old_binary) and
                receipt.get('new_binary_sha256') == sha(new_binary), 'Prerequisite binary bindings differ')
    exposed, paths = exposure.exposure_inventory(directory=directory)
    previous.assert_reserved_unexposed(roots, exposed)
    plan = deepcopy(reservation)
    plan.update(sealed_utc=datetime.now(timezone.utc).isoformat(),
                reservation_sha256=sha(directory / 'reservation.json'),
                old_binary_path=str(old_binary), old_binary_sha256=sha(old_binary),
                new_binary_path=str(new_binary), new_binary_sha256=sha(new_binary),
                correctness_path=str(correctness), known_positions_path=str(known_positions),
                protocol_path=str(protocol), native_build_binding='Both searches and prerequisite passes sealed before fresh labels')
    profile = read(CANDIDATE_PROFILE)
    require(profile['options'].get('UseQuiescenceThreats') is True, 'Candidate profile must enable the sealed search feature')
    plan['input_sha256'].update({str(path): sha(path) for path in
                                (old_binary, new_binary, correctness, known_positions, protocol, CANDIDATE_PROFILE, *paths)})
    write(directory / 'plan.json', plan)
    return plan


def authenticate(directory):
    directory, reservation, roots = authenticate_reservation(directory)
    plan = read(directory / 'plan.json')
    require(plan['reservation_sha256'] == sha(directory / 'reservation.json'), 'Reservation changed after seal')
    for key, value in reservation.items():
        if key not in ('input_sha256', 'native_build_binding'):
            require(plan.get(key) == value, 'Sealed reservation field changed: ' + key)
    for path, digest in plan['input_sha256'].items():
        require(sha(path) == digest, 'Sealed input changed: ' + path)
    return directory, plan, roots


def label(directory):
    directory, plan, roots = authenticate(directory)
    output = directory / 'root-teacher'
    require(not output.exists() and not (directory / 'label-started.json').exists(),
            'Root teacher already attempted; no retry')
    profile = read(previous.PROFILE)
    teacher = Path(profile['binary']['path'])
    require(sha(teacher) == profile['binary']['sha256'], 'Teacher binary changed')
    write(directory / 'label-started.json', {
        'started_utc': datetime.now(timezone.utc).isoformat(), 'plan_sha256': sha(directory / 'plan.json'),
        'source_sha256': sha(Path(__file__)), 'teacher_sha256': sha(teacher),
        'maximum_queries': len(roots), 'nodes': previous.TEACHER_NODES, 'teacher_depth': TEACHER_DEPTH,
        'purpose': 'Reserved gate answers only; forbidden for training or search/binary selection'})
    labels, report = label_rows(teacher, roots, output, nodes=previous.TEACHER_NODES,
                                max_abs_cp=1500, max_depth=TEACHER_DEPTH)
    write(directory / 'root-label-seal.json', {
        'plan_sha256': sha(directory / 'plan.json'),
        'started_sha256': sha(directory / 'label-started.json'),
        'files': {str(output / name): sha(output / name)
                  for name in ('labels.jsonl', 'teacher-queries.jsonl', 'teacher-report.json')},
        'accepted': len(labels), 'minimum_for_later_gate': CRITERIA['minimum_accepted_positions'], 'no_refill': True})
    return report


def run(directory, output_path):
    directory, plan, roots = authenticate(directory)
    output = Path(output_path).resolve()
    require(not output.exists() and not (directory / 'consumed.json').exists(), 'Gate already attempted; no retry')
    model = stm.load_model(MODEL)
    require(model.get('format_version') == 3 and model.get('input_scale') == 32, 'Require unchanged v3 S32 NNUE')
    label_seal = read(directory / 'root-label-seal.json')
    require(label_seal['plan_sha256'] == sha(directory / 'plan.json') and
            label_seal['started_sha256'] == sha(directory / 'label-started.json'), 'Root label seal changed')
    for path, digest in label_seal['files'].items():
        require(sha(path) == digest, 'Root teacher receipt changed: ' + path)
    accepted = previous.lines(directory / 'root-teacher/labels.jsonl')
    receipts = {row['source_query_id']: row for row in previous.lines(directory / 'root-teacher/teacher-queries.jsonl')}
    require(len(accepted) == len({row['query_id'] for row in accepted}), 'Duplicate root labels')
    roots_by_id = {row['query_id']: row for row in roots}
    for row in accepted:
        source = roots_by_id[row['query_id']]
        require(all(row[key] == source[key] for key in source if key != 'target_white_cp'), 'Accepted source changed')
        receipt = receipts[row['query_id']]
        require(receipt['status'] == 'accepted' and receipt['reasons'] == [] and
                receipt['requested_nodes'] == previous.TEACHER_NODES and
                receipt['requested_depth'] == TEACHER_DEPTH, 'Invalid root teacher receipt')
        require(row['teacher_query_id'] == receipt['query_id'] and
                row['target_white_cp'] == receipt['score']['white_cp'], 'Root CP differs from receipt')
    exposed, paths = exposure.exposure_inventory(directory=directory)
    previous.assert_reserved_unexposed(roots, exposed)
    options = search_options()
    binaries = {'warm': Path(plan['old_binary_path']), 'candidate': Path(plan['new_binary_path'])}
    teacher_profile = read(previous.PROFILE)
    teacher = Path(teacher_profile['binary']['path'])
    require(sha(teacher) == teacher_profile['binary']['sha256'], 'Teacher binary changed')
    bindings = [directory / 'plan.json', directory / 'root-label-seal.json', directory / 'label-started.json',
                *map(Path, label_seal['files']), *map(Path, plan['input_sha256']), teacher, *paths]
    result = {'schema_version': 1, 'completed': False, 'started_utc': datetime.now(timezone.utc).isoformat(),
              'plan_sha256': sha(directory / 'plan.json'), 'model_sha256': MODEL_SHA,
              'old_binary_sha256': plan['old_binary_sha256'], 'new_binary_sha256': plan['new_binary_sha256'],
              'input_sha256': {str(path): sha(path) for path in bindings},
              'criteria_fixed_before_search': CRITERIA, 'limitations': plan['limitations'],
              'warm_options': options['warm'], 'candidate_options': options['candidate'], 'native_nodes': previous.NODES,
              'teacher_nodes': previous.TEACHER_NODES, 'teacher_depth': TEACHER_DEPTH,
              'native_validation_mismatches': 0, 'training_runs': 0,
              'maximum_native_searches': 2 * len(roots), 'maximum_child_teacher_queries': 2 * len(roots),
              'actual_native_searches': 0, 'cases': [], 'excluded_cases': []}
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf-8') as stream:
        stream.write(json.dumps(result, indent=2) + '\n')
    write(directory / 'consumed.json', {'started_utc': result['started_utc'],
          'plan_sha256': result['plan_sha256'], 'result_path': str(output),
          'fresh_independent_reuse_allowed': False, 'completed': False, 'games': 0})
    tick, pending, children = time.perf_counter(), [], {}
    if len(accepted) >= CRITERIA['minimum_accepted_positions']:
        engines = {}
        try:
            for role, binary in binaries.items():
                engines[role] = chess.engine.SimpleEngine.popen_uci([str(binary), 'uci'], timeout=30)
            for source in accepted:
                board = source_board(source)
                case = {'source': source, 'teacher_bestmove_uci': receipts[source['query_id']]['bestmove_uci'],
                        'teacher_receipt': receipts[source['query_id']], 'searches': {}, 'child_ids': {}}
                for role, engine in engines.items():
                    require(sha(binaries[role]) == plan['old_binary_sha256' if role == 'warm' else 'new_binary_sha256'], 'Search binary changed')
                    search = previous._search(engine, board, options[role], MODEL_SHA)
                    search['binary_sha256'] = sha(binaries[role])
                    result['actual_native_searches'] += 1
                    case['searches'][role] = search
                    move = search['move_uci']
                    key = source['query_id'] + '-child-' + move
                    case['child_ids'][role] = key
                    if key not in children:
                        child = board.copy(stack=True)
                        child.push_uci(move)
                        children[key] = {**source, 'query_id': key, 'root_id': source['query_id'],
                                         'root_move': move, 'history': source['history'] + [move],
                                         'fen': child.fen(en_passant='fen'),
                                         'feature_hash': base.feature_identity(child), 'target_white_cp': 0}
                pending.append(case)
                result['pending_cases'] = pending
                write(output, result)
        finally:
            for engine in engines.values():
                engine.quit()
        child_output = output.parent / (output.stem + '-child-teacher')
        child_labels, child_report = label_rows(teacher, list(children.values()), child_output,
                                                nodes=previous.TEACHER_NODES, max_abs_cp=1500,
                                                max_depth=TEACHER_DEPTH)
        child_by_id = {row['query_id']: row for row in child_labels}
        child_receipts = {row['source_query_id']: row for row in previous.lines(child_output / 'teacher-queries.jsonl')}
        for case in pending:
            missing = [role for role, key in case['child_ids'].items() if key not in child_by_id]
            if missing:
                result['excluded_cases'].append({'root_id': case['source']['query_id'],
                    'reason': 'missing_exact_selected_child', 'roles': missing,
                    'receipts': {role: child_receipts[case['child_ids'][role]] for role in missing}})
                continue
            case['child_white_cp'] = {role: child_by_id[key]['target_white_cp'] for role, key in case['child_ids'].items()}
            case['child_teacher_receipts'] = {role: child_receipts[key] for role, key in case['child_ids'].items()}
            case['mover_quality_gain_cp'] = previous.quality_delta_cp(source_board(case['source']).turn,
                case['child_white_cp']['warm'], case['child_white_cp']['candidate'])
            result['cases'].append(case)
        result.update(child_teacher_report=child_report, actual_child_teacher_queries=len(children),
                      child_teacher_files={str(child_output / name): sha(child_output / name)
                          for name in ('labels.jsonl', 'teacher-queries.jsonl', 'teacher-report.json')})
    else:
        result.update(reason='Too few exact root labels; no refill', actual_child_teacher_queries=0)
    result.pop('pending_cases', None)
    for path, digest in result['input_sha256'].items():
        require(sha(path) == digest, 'Input changed during sealed gate: ' + path)
    result.update(completed=True, finished_utc=datetime.now(timezone.utc).isoformat(),
                  elapsed_seconds=time.perf_counter() - tick, gate=previous.evaluate_gate(result['cases']))
    write(output, result)
    write(directory / 'consumed.json', {'created_utc': datetime.now(timezone.utc).isoformat(),
          'plan_sha256': result['plan_sha256'], 'model_sha256': MODEL_SHA,
          'result_path': str(output), 'result_sha256': sha(output),
          'completed': True, 'gate_passed': result['gate']['passed'],
          'fresh_independent_reuse_allowed': False, 'games': 0})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('inventory')
    p = sub.add_parser('reserve'); p.add_argument('directory', type=Path)
    p = sub.add_parser('seal')
    for name in ('directory', 'old_binary', 'new_binary', 'correctness', 'known_positions', 'protocol'):
        p.add_argument(name, type=Path)
    p = sub.add_parser('label'); p.add_argument('directory', type=Path)
    p = sub.add_parser('run'); p.add_argument('directory', type=Path); p.add_argument('output', type=Path)
    args = parser.parse_args()
    if args.command == 'inventory':
        exposed, paths = exposure.exposure_inventory()
        roots, excluded, blocked = select_roots(exposed)
        value = {'exposure_files': len(paths), 'exposed_features': len(exposed),
                 'eligible_indices': [row['source_index'] for row in roots],
                 'extension_plies': [len(row['deterministic_extension_uci']) for row in roots],
                 'excluded': excluded, 'blocked_features': len(blocked)}
    elif args.command == 'reserve': value = reserve(args.directory)
    elif args.command == 'seal': value = seal(args.directory, args.old_binary, args.new_binary,
                                           args.correctness, args.known_positions, args.protocol)
    elif args.command == 'label': value = label(args.directory)
    else:
        result = run(args.directory, args.output)
        value = {'completed': result['completed'], 'gate': result['gate'],
                 'elapsed_seconds': result.get('elapsed_seconds')}
    print(json.dumps(value, indent=2))


if __name__ == '__main__':
    main()
