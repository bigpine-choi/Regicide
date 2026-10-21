"""Freeze untrained opening roots and run one sealed move-quality gate.

The opening sources appeared in past M2 matches. Only their exclusion from
NNUE gradients/checkpoint selection is claimed; this is no independent Elo test.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time

import chess
import chess.engine

import neural_training as base
import neural_stm as stm
from stockfish_teacher import label_rows, source_board, _json_info

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / 'build-m3-stm/regicide.exe'
BINARY_SHA = '97140c4ad1c40a9f2c67e2fb9a8c0718fe70722e83dcbe50a7011b71c97558d1'
WARM = ROOT / 'docs/checks/2026-10-22-nnue-ordering/attempt-001/model.nnue'
WARM_SHA = '4e54305bce4a495ff3bb0d961e397f78eac12d387ff30e7766202fcdf7d3bdee'
PROFILE = ROOT / 'docs/checks/2026-10-21-m2-foundation/opponents/stockfish18-profile.json'
OPENINGS = ROOT / 'tests/data/m2_formal_openings_2026-10-21.json'
INDICES = [0, 1, 2, 3, 4, 5, 8, 9, 12, 13, 14, 16, 19, 21, 29, 31]
NODES = 10000
TEACHER_NODES = 100000
CRITERIA = {
    'minimum_accepted_positions': 12,
    'minimum_bestmove_match_gain': 2,
    'minimum_mean_mover_quality_gain_cp': 20,
    'maximum_regressions_at_least_200cp': 0,
    'native_mismatches': 0,
    'games_if_pass': 16, 'games_if_fail': 0,
    'retry_or_gate_checkpoint_selection': False,
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def lines(path):
    return [json.loads(x) for x in Path(path).read_text(encoding='utf-8').splitlines()]


def write(path, value):
    Path(path).write_text(json.dumps(value, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def root_features(board):
    """Reserve all legal one-ply siblings as well as their shared root."""
    result = {base.feature_identity(board)}
    for move in board.legal_moves:
        child = board.copy(stack=False)
        child.push(move)
        result.add(base.feature_identity(child))
    return result


def exposure_inventory():
    files = sorted({p.resolve() for name in ('train.jsonl', 'validation.jsonl', 'tuning.jsonl')
                    for p in (ROOT / 'docs/checks').rglob(name)
                    if 'source-snapshot' not in p.parts})
    exposed = set()
    for path in files:
        for row in lines(path):
            if isinstance(row.get('fen'), str):
                exposed.add(base.feature_identity(chess.Board(row['fen'])))
    return exposed, files


def prepare(directory):
    directory = Path(directory).resolve()
    require(not directory.exists(), 'Refuse to replace a frozen reserve')
    require(sha(WARM) == WARM_SHA and sha(BINARY) == BINARY_SHA, 'Warm/native bytes differ')
    exposed, files = exposure_inventory()
    openings = read(OPENINGS)['openings']
    roots, excluded, blocked = [], [], set()
    for index in INDICES:
        opening = openings[index]
        board = chess.Board(opening.get('initial_fen', chess.STARTING_FEN))
        history = opening['moves']
        for text in history:
            require(chess.Move.from_uci(text) in board.legal_moves, 'Illegal source opening')
            board.push_uci(text)
        require(board.fen(en_passant='fen') == chess.Board(opening['fen_after_opening']).fen(en_passant='fen'),
                'Opening FEN differs from complete history')
        features = root_features(board)
        if features & exposed:
            excluded.append({'index': index, 'reason': 'root_or_legal_child_exposed',
                             'overlap_features': sorted(features & exposed)})
            continue
        require(not features & blocked, 'Reserve roots/children collide')
        blocked.update(features)
        roots.append({'query_id': f'generalization-opening-{index:02d}', 'source_index': index,
                      'source_path': str(OPENINGS), 'family_id': opening['family_id'],
                      'cluster_id': opening['cluster_id'], 'kind': 'opening_gate',
                      'partition': 'gate', 'initial_fen': opening.get('initial_fen', chess.STARTING_FEN),
                      'history': history, 'fen': board.fen(en_passant='fen'),
                      'feature_hash': base.feature_identity(board),
                      'blocked_feature_hashes': sorted(features),
                      'target_white_cp': 0})
    require(len(roots) >= CRITERIA['minimum_accepted_positions'], 'Too few untouched reserved roots')
    directory.mkdir(parents=True)
    write(directory / 'frozen-roots.json', roots)
    write(directory / 'blocked-features.json', sorted(blocked))
    bindings = [OPENINGS, WARM, BINARY, PROFILE, ROOT / 'config/engine-profiles/classical.json',
                ROOT / 'scripts/stockfish_teacher.py', ROOT / 'scripts/neural_training.py', *files]
    plan = {'schema_version': 1, 'created_utc': datetime.now(timezone.utc).isoformat(),
            'selection': 'Fixed source indices; no model search/teacher score inspected; exclusion without refill',
            'indices': INDICES, 'reserved_roots': len(roots), 'excluded': excluded,
            'exposure_files': len(files), 'exposure_unique_features': len(exposed),
            'limitations': 'Project-authored openings appeared in past M2 matches; root and every legal child exclude all recorded NNUE train/validation/tuning inputs including color/rank mirrors. This gate is excluded from the current gradients and checkpoint selection.',
            'criteria': deepcopy(CRITERIA), 'native_nodes': NODES, 'teacher_nodes': TEACHER_NODES,
            'max_root_teacher_queries': len(roots), 'max_child_teacher_queries': 2 * len(roots),
            'warm_model': str(WARM), 'warm_sha256': WARM_SHA, 'binary_sha256': BINARY_SHA,
            'frozen_roots_sha256': sha(directory / 'frozen-roots.json'),
            'blocked_features_sha256': sha(directory / 'blocked-features.json'),
            'input_sha256': {str(path): sha(path) for path in bindings},
            'gate_source_path': str(Path(__file__).resolve()),
            'no_test_driven_retraining_or_retries': True}
    write(directory / 'plan.json', plan)
    return plan


def authenticate(directory):
    directory = Path(directory).resolve()
    plan = read(directory / 'plan.json')
    require(plan['criteria'] == CRITERIA, 'Frozen criteria differ from implementation')
    require(plan['native_nodes'] == NODES and plan['teacher_nodes'] == TEACHER_NODES,
            'Frozen search budgets differ')
    require(sha(directory / 'frozen-roots.json') == plan['frozen_roots_sha256'], 'Reserved roots changed')
    require(sha(directory / 'blocked-features.json') == plan['blocked_features_sha256'], 'Blocked features changed')
    for path, digest in plan['input_sha256'].items():
        require(sha(path) == digest, 'Frozen input changed: ' + path)
    return directory, plan, read(directory / 'frozen-roots.json')


def label(directory):
    directory, plan, roots = authenticate(directory)
    output = directory / 'root-teacher'
    require(not output.exists() and not (directory / 'label-started.json').exists(),
            'Root teacher was already attempted; no retry')
    profile = read(PROFILE)
    engine = Path(profile['binary']['path'])
    require(sha(engine) == profile['binary']['sha256'], 'Teacher binary changed')
    write(directory / 'label-started.json', {
        'started_utc': datetime.now(timezone.utc).isoformat(),
        'plan_sha256': sha(directory / 'plan.json'),
        'source_sha256': sha(Path(__file__)), 'teacher_sha256': sha(engine),
        'maximum_queries': len(roots), 'nodes': TEACHER_NODES,
        'purpose': 'Reserved gate root answers only; forbidden in learning or model selection'})
    labels, report = label_rows(engine, roots, output, nodes=TEACHER_NODES, max_abs_cp=1500)
    write(directory / 'root-label-seal.json', {
        'plan_sha256': sha(directory / 'plan.json'),
        'started_sha256': sha(directory / 'label-started.json'),
        'files': {str(output / name): sha(output / name)
                  for name in ('labels.jsonl', 'teacher-queries.jsonl', 'teacher-report.json')},
        'accepted': len(labels), 'minimum_for_later_gate': CRITERIA['minimum_accepted_positions'],
        'no_refill': True})
    return report


def quality_delta_cp(root_turn, warm_child_white_cp, candidate_child_white_cp):
    """Both scores remain white POV even though the CHILD mover has flipped."""
    require(type(root_turn) is bool, 'Root turn must be a color bool')
    require(type(warm_child_white_cp) is int and type(candidate_child_white_cp) is int,
            'Compare exact CP only')
    return (1 if root_turn else -1) * (candidate_child_white_cp - warm_child_white_cp)


def evaluate_gate(cases, *, native_mismatches=0):
    ids = [case['source']['query_id'] for case in cases]
    require(len(ids) == len(set(ids)), 'Duplicate gate cases')
    warm = sum(case['searches']['warm']['move_uci'] == case['teacher_bestmove_uci'] for case in cases)
    candidate = sum(case['searches']['candidate']['move_uci'] == case['teacher_bestmove_uci'] for case in cases)
    deltas = [quality_delta_cp(source_board(case['source']).turn,
                              case['child_white_cp']['warm'], case['child_white_cp']['candidate'])
              for case in cases]
    mean = sum(deltas) / len(deltas) if deltas else None
    regressions = sum(value <= -200 for value in deltas)
    conditions = {
        'at_least_twelve_exact_paired_positions': len(cases) >= CRITERIA['minimum_accepted_positions'],
        'at_least_two_more_teacher_bestmoves': candidate - warm >= CRITERIA['minimum_bestmove_match_gain'],
        'mean_root_mover_quality_gain_at_least_twenty_cp': mean is not None and mean >= CRITERIA['minimum_mean_mover_quality_gain_cp'],
        'no_regression_of_two_hundred_cp_or_more': regressions <= CRITERIA['maximum_regressions_at_least_200cp'],
        'native_validation_has_zero_mismatches': native_mismatches == CRITERIA['native_mismatches'],
    }
    passed = all(conditions.values())
    return {'passed': passed, 'conditions': conditions, 'accepted_positions': len(cases),
            'warm_matches': warm, 'candidate_matches': candidate,
            'mean_mover_quality_gain_cp': mean, 'quality_deltas_cp': deltas,
            'regressions_at_least_200cp': regressions,
            'match_authorized_by_gate': passed,
            'authorized_games': CRITERIA['games_if_pass'] if passed else CRITERIA['games_if_fail'],
            'retry_authorized': False}


def assert_reserved_unexposed(roots, exposed):
    blocked = set().union(*(root_features(source_board(row)) for row in roots))
    require(not blocked & exposed, 'Reserved root or legal child entered train/checkpoint selection')


def _search(engine, board, options, model_hash):
    engine.configure(options)
    engine.configure({'Clear Hash': None})
    tick, last_score = time.perf_counter(), {}
    with engine.analysis(board, chess.engine.Limit(nodes=NODES), game=object(),
                         info=chess.engine.INFO_ALL) as analysis:
        for packet in analysis:
            if 'score' in packet:
                last_score = packet.copy()
        best = analysis.wait().move
        aggregate = analysis.info
    require(best in board.legal_moves, 'Native bestmove missing or illegal')
    require(type(aggregate.get('nodes')) is int and aggregate['nodes'] >= NODES,
            'Native fixed-node search did not complete')
    score = last_score.get('score')
    require(isinstance(score, chess.engine.PovScore) and score.turn == board.turn,
            'Native search score POV missing or wrong')
    require(last_score.get('pv') and last_score['pv'][0] == best, 'Native score/PV differs from bestmove')
    replay = board.copy(stack=True)
    for move in last_score['pv']:
        require(move in replay.legal_moves, 'Native PV illegal')
        replay.push(move)
    return {'model_sha256': model_hash, 'options': options, 'requested_nodes': NODES,
            'nodes': aggregate['nodes'], 'depth': last_score.get('depth'),
            'move_uci': best.uci(), 'move_san': board.san(best),
            'white_cp': score.white().score(), 'white_mate': score.white().mate(),
            'pv_uci': [move.uci() for move in last_score['pv']],
            'raw_info': _json_info(aggregate), 'raw_score_info_packet': _json_info(last_score),
            'elapsed_seconds': time.perf_counter() - tick}


def run(directory, candidate_path, output_path):
    directory, plan, roots = authenticate(directory)
    candidate, output = Path(candidate_path).resolve(), Path(output_path).resolve()
    require(not output.exists(), 'Refuse to repeat or overwrite a gate result')
    require(candidate != WARM.resolve(), 'Candidate must be separate from warm')
    model = stm.load_model(candidate)
    require(model.get('format_version') == 3 and model.get('input_scale') == 32,
            'Require unchanged v3 S32 NNUE')
    candidate_hash = sha(candidate)
    validations = candidate.parent / 'validation.json'
    validation = read(validations)
    require(validation['native_model_returncode'] == 0 and
            validation['python_native_full_incremental_mismatches'] == 0,
            'Candidate native correctness validation failed')
    require(validation['input_sha256'].get(str(candidate)) == candidate_hash,
            'Native validation does not bind this candidate')
    training_paths = [candidate.parent / name for name in ('protocol.json', 'training.json', 'selection.json')]
    for path in training_paths:
        require(path.is_file(), 'Finish and seal training/selection before the gate: ' + str(path))
    seal = read(directory / 'root-label-seal.json')
    require(seal['plan_sha256'] == sha(directory / 'plan.json'), 'Root teacher belongs to another plan')
    require(seal['started_sha256'] == sha(directory / 'label-started.json'), 'Root label start changed')
    for path, digest in seal['files'].items():
        require(sha(path) == digest, 'Root teacher receipt changed: ' + path)
    accepted = lines(directory / 'root-teacher/labels.jsonl')
    receipts = {row['source_query_id']: row for row in lines(directory / 'root-teacher/teacher-queries.jsonl')}
    root_by_id = {row['query_id']: row for row in roots}
    for row in accepted:
        source = root_by_id[row['query_id']]
        require(all(row[key] == source[key] for key in source if key != 'target_white_cp'),
                'Accepted root differs from frozen source')
        receipt = receipts[row['query_id']]
        require(receipt['status'] == 'accepted' and receipt['reasons'] == [] and
                receipt['requested_nodes'] == TEACHER_NODES, 'Invalid root teacher receipt')
        require(row['teacher_query_id'] == receipt['query_id'] and
                row['target_white_cp'] == receipt['score']['white_cp'], 'Root CP differs from receipt')
    exposed, exposure_files = exposure_inventory()
    assert_reserved_unexposed(roots, exposed)
    common = dict(read(ROOT / 'config/engine-profiles/classical.json')['options'], UseNNUEEvaluation=True)
    options = {'warm': dict(common, NeuralModelFile=str(WARM)),
               'candidate': dict(common, NeuralModelFile=str(candidate))}
    teacher = Path(read(PROFILE)['binary']['path'])
    require(sha(teacher) == read(PROFILE)['binary']['sha256'], 'Teacher changed')
    binding_paths = [directory / 'plan.json', directory / 'frozen-roots.json',
                     directory / 'root-label-seal.json', directory / 'label-started.json',
                     Path(__file__), BINARY, WARM, candidate, validations, teacher,
                     *training_paths, *exposure_files,
                     *map(Path, seal['files']), ROOT / 'scripts/stockfish_teacher.py']
    result = {'schema_version': 1, 'started_utc': datetime.now(timezone.utc).isoformat(),
              'completed': False, 'plan_sha256': sha(directory / 'plan.json'),
              'criteria_fixed_before_search': plan['criteria'], 'candidate_sha256': candidate_hash,
              'warm_sha256': WARM_SHA, 'binary_sha256': BINARY_SHA,
              'input_sha256': {str(path): sha(path) for path in binding_paths},
              'limitations': plan['limitations'], 'candidate_options': options['candidate'],
              'warm_options': options['warm'], 'native_nodes': NODES, 'teacher_nodes': TEACHER_NODES,
              'native_validation_mismatches': 0, 'training_runs': 0,
              'maximum_native_searches': 2 * len(roots), 'maximum_child_teacher_queries': 2 * len(roots),
              'cases': [], 'excluded_cases': [], 'actual_native_searches': 0}
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf-8') as stream:
        stream.write(json.dumps(result, indent=2) + '\n')
    tick = time.perf_counter()
    if len(accepted) < CRITERIA['minimum_accepted_positions']:
        result.update(completed=True, gate=evaluate_gate([]), reason='Too few exact root labels; no refill')
        write(output, result)
        return result
    pending, children = [], {}
    with chess.engine.SimpleEngine.popen_uci([str(BINARY), 'uci'], timeout=30) as engine:
        for source in accepted:
            board = source_board(source)
            case = {'source': source, 'teacher_bestmove_uci': receipts[source['query_id']]['bestmove_uci'],
                    'teacher_receipt': receipts[source['query_id']], 'searches': {}, 'child_ids': {}}
            for role, digest in (('warm', WARM_SHA), ('candidate', candidate_hash)):
                require(sha(candidate) == candidate_hash, 'Candidate changed during gate')
                search = _search(engine, board, options[role], digest)
                result['actual_native_searches'] += 1
                case['searches'][role] = search
                move = search['move_uci']
                key = source['query_id'] + '-child-' + move
                case['child_ids'][role] = key
                if key not in children:
                    child_board = board.copy(stack=True)
                    child_board.push_uci(move)
                    children[key] = {**source, 'query_id': key, 'root_id': source['query_id'],
                                     'root_move': move, 'history': source['history'] + [move],
                                     'fen': child_board.fen(en_passant='fen'),
                                     'feature_hash': base.feature_identity(child_board),
                                     'target_white_cp': 0}
            pending.append(case)
            result['pending_cases'] = pending
            write(output, result)
    child_output = output.parent / (output.stem + '-child-teacher')
    child_labels, child_report = label_rows(teacher, list(children.values()), child_output,
                                            nodes=TEACHER_NODES, max_abs_cp=1500)
    child_by_id = {row['query_id']: row for row in child_labels}
    child_receipts = {row['source_query_id']: row for row in lines(child_output / 'teacher-queries.jsonl')}
    for case in pending:
        missing = [role for role, key in case['child_ids'].items() if key not in child_by_id]
        if missing:
            result['excluded_cases'].append({'root_id': case['source']['query_id'],
                                             'reason': 'missing_exact_selected_child', 'roles': missing,
                                             'receipts': {role: child_receipts[case['child_ids'][role]] for role in missing}})
            continue
        case['child_white_cp'] = {role: child_by_id[key]['target_white_cp'] for role, key in case['child_ids'].items()}
        case['child_teacher_receipts'] = {role: child_receipts[key] for role, key in case['child_ids'].items()}
        case['mover_quality_gain_cp'] = quality_delta_cp(source_board(case['source']).turn,
                                                        case['child_white_cp']['warm'],
                                                        case['child_white_cp']['candidate'])
        result['cases'].append(case)
    result.pop('pending_cases', None)
    for path, digest in result['input_sha256'].items():
        require(sha(path) == digest, 'Input changed during sealed gate: ' + path)
    result.update(completed=True, finished_utc=datetime.now(timezone.utc).isoformat(),
                  elapsed_seconds=time.perf_counter() - tick,
                  child_teacher_report=child_report, actual_child_teacher_queries=len(children),
                  child_teacher_files={str(child_output / name): sha(child_output / name)
                                       for name in ('labels.jsonl', 'teacher-queries.jsonl', 'teacher-report.json')},
                  gate=evaluate_gate(result['cases']))
    write(output, result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('prepare')
    p.add_argument('directory', type=Path)
    p = sub.add_parser('label')
    p.add_argument('directory', type=Path)
    p = sub.add_parser('run')
    p.add_argument('directory', type=Path)
    p.add_argument('candidate', type=Path)
    p.add_argument('output', type=Path)
    args = parser.parse_args()
    if args.command == 'prepare':
        print(json.dumps(prepare(args.directory), indent=2))
    elif args.command == 'label':
        print(json.dumps(label(args.directory), indent=2))
    else:
        result = run(args.directory, args.candidate, args.output)
        print(json.dumps({'completed': result['completed'], 'gate': result['gate'],
                          'elapsed_seconds': result.get('elapsed_seconds')}, indent=2))


if __name__ == '__main__':
    main()
