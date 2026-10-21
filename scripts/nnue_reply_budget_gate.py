"""Adopt the still-unused sixteen roots for one bounded tactical-cost screen.

The original reservation and both unused qsearch-cost reservations remain
immutable. Historical input hashes and their explicit source archive are
authenticated; only later diagnostics are replayed. No gate answers, native
searches or fitting are performed by reserve or its position-only tests.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import nnue_qsearch_cost_gate as cost
from check_nnue_tactical_search import fingerprint, known_cases

legacy = cost.legacy
ROOT, MODEL, MODEL_SHA = cost.ROOT, cost.MODEL, cost.MODEL_SHA
CRITERIA, TEACHER_DEPTH = deepcopy(cost.CRITERIA), cost.TEACHER_DEPTH
SOURCE = cost.SOURCE
PREVIOUS = cost.EXPERIMENT / 'gate-002'
EXPERIMENT = ROOT / 'docs/checks/2026-10-22-nnue-qsearch-reply-budget'
CANDIDATE_PROFILE = ROOT / 'config/engine-profiles/nnue-qsearch-reply-budget.json'
SOURCE_FILES = cost.SOURCE_FILES
CACHE_OPTION, ORDER_OPTION = cost.CACHE_OPTION, cost.ORDER_OPTION
REPLY_OPTION = 'UseQuiescenceReplyBudget'
BF5_FIRST_NODE_LIMIT = 2577
sha, read, write, require = cost.sha, cost.read, cost.write, cost.require


def search_options():
    common = dict(read(ROOT / 'config/engine-profiles/classical.json')['options'],
                  UseNNUEEvaluation=True, NeuralModelFile=str(MODEL),
                  UseQuiescenceThreats=True,
                  **{CACHE_OPTION: True, ORDER_OPTION: True})
    # The previous binary predates the candidate-only option. Its missing option
    # means the disabled path; do not send an unsupported option to that binary.
    return {'warm': common, 'candidate': dict(common, **{ORDER_OPTION: False, REPLY_OPTION: True})}


def authenticate_source(previous=None):
    source, original, roots, blocked = cost.authenticate_source()
    previous = Path(previous or PREVIOUS).resolve()
    require({path.name for path in previous.iterdir()} == SOURCE_FILES,
            'Previous reservation was queried, sealed, consumed, or changed')
    previous, reservation, adopted = cost.authenticate_reservation(previous)
    audit = read(previous / 'position-only-verification.json')
    require(audit.get('completed') is True and audit.get('passed') is True and
            audit.get('native_queries') == 0 and audit.get('teacher_queries') == 0 and
            audit.get('training_runs') == 0 and audit.get('overlap_features') == 0 and
            audit.get('reservation_sha256') == sha(previous / 'reservation.json'),
            'Previous zero-query position-only audit is invalid')
    require(len(adopted) == len(roots) == 16 and len(blocked) == 594,
            'Exactly sixteen untouched roots and all 594 child features are required')
    for name in ('frozen-roots.json', 'blocked-features.json'):
        require((source / name).read_bytes() == (previous / name).read_bytes(),
                'Previous reservation changed original position bytes: ' + name)
    return source, original, previous, reservation, roots, blocked


def delta_inventory(*, directory=None, experiment=None):
    """Replay only diagnostics newer than the authenticated previous exclusion."""
    source, original, previous, reservation, roots, blocked = authenticate_source()
    experiment = Path(experiment or EXPERIMENT).resolve()
    omitted = Path(directory).resolve() if directory is not None else None
    historical = {Path(path).resolve() for path in
                  (*original['input_sha256'], *reservation['input_sha256'])}
    omitted_folders = (source, cost.EXPERIMENT / 'gate-001', previous,
                       experiment / 'gate-001')
    features, paths = set(), []
    for folder in (source.parent, cost.EXPERIMENT, experiment):
        if not folder.exists():
            continue
        for path in sorted(folder.rglob('*')):
            if path.suffix not in ('.json', '.jsonl') or any(
                    'source-snapshot' in part for part in path.parts):
                continue
            resolved = path.resolve()
            if (resolved in historical or
                    any(resolved.is_relative_to(part) for part in omitted_folders) or
                    (omitted and resolved.is_relative_to(omitted))):
                continue
            content = path.read_text(encoding='utf-8')
            if not content.strip():
                continue
            if not content.lstrip().startswith(('{', '[')):
                require('"fen"' not in content and '"root_fen"' not in content,
                        'Position-bearing non-JSON diagnostic: ' + str(path))
                continue
            values = ([json.loads(line) for line in content.splitlines() if line.strip()]
                      if path.suffix == '.jsonl' else [json.loads(content)])
            found = set().union(*(legacy.exposure.position_features(value) for value in values))
            if path.name == 'blocked-features.json':
                require(len(values) == 1 and isinstance(values[0], list), 'Malformed feature block')
                found.update(values[0])
            features.update(found)
            paths.append(resolved)
    require(not blocked & features, 'Reserved root or legal child entered later diagnostics')
    return features, paths


def reserve(directory):
    directory = Path(directory).resolve()
    require(not directory.exists(), 'Refuse to replace a reservation')
    source, original, previous, previous_reservation, roots, blocked = authenticate_source()
    require(sha(MODEL) == MODEL_SHA, 'Frozen output-only NNUE changed')
    features, paths = delta_inventory(directory=directory)
    directory.mkdir(parents=True)
    for name in ('frozen-roots.json', 'blocked-features.json'):
        (directory / name).write_bytes((source / name).read_bytes())
    bindings = [Path(__file__).resolve(), ROOT / 'tests/nnue_reply_budget_gate_tests.py',
                Path(cost.__file__).resolve(), Path(legacy.__file__).resolve(),
                *[previous / name for name in SOURCE_FILES],
                *map(Path, previous_reservation['input_sha256']), *paths]
    result = {'schema_version': 1, 'created_utc': datetime.now(timezone.utc).isoformat(),
              'source_reservation': str(source),
              'source_reservation_sha256': sha(source / 'reservation.json'),
              'previous_unused_reservation': str(previous),
              'previous_reservation_sha256': sha(previous / 'reservation.json'),
              'selection': 'Byte-identical adoption of the same still-unqueried sixteen roots and all legal children. No new opening-family claim, no outcome-based selection or refill.',
              'reserved_roots': len(roots), 'blocked_root_and_legal_child_features': len(blocked),
              'historical_exposure_files': original['exposure_files'],
              'historical_exposure_unique_features': original['exposure_unique_features'],
              'previous_later_diagnostic_files': previous_reservation['later_diagnostic_files'],
              'previous_later_diagnostic_features': previous_reservation['later_diagnostic_features'],
              'new_later_diagnostic_files': len(paths), 'new_later_diagnostic_features': len(features),
              'later_overlap_features': 0,
              'exposure_policy': 'Authenticate original historical hashes and unused gate002 including its pre-ordering source archive. Replay only later tactical, qsearch-cost and reply-budget JSON diagnostics, including wrapped roots, complete histories and legal PV prefixes. Reserved root bytes are not observed outcomes.',
              'limitations': original['limitations'], 'criteria': deepcopy(CRITERIA),
              'native_nodes': legacy.previous.NODES,
              'teacher_nodes': legacy.previous.TEACHER_NODES, 'teacher_depth': TEACHER_DEPTH,
              'max_root_teacher_queries': 16, 'max_child_teacher_queries': 32,
              'model_path': str(MODEL), 'model_sha256': MODEL_SHA,
              'search_options': search_options(),
              'bf5_first_iteration_node_limit': BF5_FIRST_NODE_LIMIT,
              'known_position_policy': 'All three fixed Ne4/exf6/Ng5 choices in completed iterations; first iterations complete within 10000 nodes, Bf5 first iteration no worse than original 2577 before the regression; disabled-path and full-refresh oracle equality.',
              'frozen_roots_sha256': sha(directory / 'frozen-roots.json'),
              'blocked_features_sha256': sha(directory / 'blocked-features.json'),
              'input_sha256': {str(path): sha(path) for path in bindings},
              'native_build_binding': 'Pending immutable binary pair, correctness and strict known-position pass',
              'no_test_driven_retraining_or_retries': True}
    write(directory / 'reservation.json', result)
    return result


def authenticate_reservation(directory):
    directory = Path(directory).resolve()
    reservation = read(directory / 'reservation.json')
    source, original, previous, previous_reservation, roots, blocked = authenticate_source(
        reservation['previous_unused_reservation'])
    require(reservation['source_reservation_sha256'] == sha(source / 'reservation.json') and
            reservation['previous_reservation_sha256'] == sha(previous / 'reservation.json'),
            'Historical reservation changed')
    require(reservation['criteria'] == CRITERIA and
            reservation['native_nodes'] == legacy.previous.NODES and
            reservation['teacher_nodes'] == legacy.previous.TEACHER_NODES and
            reservation['teacher_depth'] == TEACHER_DEPTH,
            'Frozen criteria or budgets differ')
    require(reservation['model_path'] == str(MODEL) and reservation['model_sha256'] == MODEL_SHA,
            'Frozen NNUE differs')
    require(reservation['search_options'] == search_options() and
            reservation['bf5_first_iteration_node_limit'] == BF5_FIRST_NODE_LIMIT,
            'Frozen search options or Bf5 cost criterion differs')
    for name, key in (('frozen-roots.json', 'frozen_roots_sha256'),
                      ('blocked-features.json', 'blocked_features_sha256')):
        require(sha(directory / name) == reservation[key] == sha(source / name),
                'Adopted position bytes differ: ' + name)
    for path, digest in reservation['input_sha256'].items():
        require(sha(path) == digest, 'Frozen reservation input changed: ' + path)
    return directory, reservation, roots


def validate_known_receipt(known):
    require(known.get('completed') is True and known.get('known_position_passed') is True and
            known.get('cost_gate_passed') is True,
            'Strict problem-position pass required before fresh answers')
    require(known.get('native_mismatches') == 0 and known.get('training_runs') == 0 and
            known.get('teacher_queries') == 0 and known.get('native_searches') == 12 and
            known.get('nodes_per_search') == legacy.previous.NODES,
            'Known-position correctness, query count or node budget differs')
    cases, expected = known.get('cases', []), known_cases()
    require(len(cases) == len(expected) == 3, 'Exactly the fixed three problem positions are required')
    options = search_options()
    for row, source in zip(cases, expected):
        require(row.get('source') == source and row.get('passed') is True and
                row.get('disabled_path_equal') is True and row.get('full_incremental_equal') is True,
                'Fixed known position or oracle equality differs')
        searches = row['searches']
        for role in ('baseline', 'candidate_off', 'candidate', 'candidate_full'):
            actual_options = dict(searches[role]['options'])
            expected_options = dict(options['candidate' if role in ('candidate', 'candidate_full') else 'warm'])
            if role == 'candidate_off':
                # New option exists in the candidate binary and is disabled for
                # behavior-equivalence checks against the previous binary.
                expected_options[REPLY_OPTION] = False
            expected_options['UseIncrementalNeuralEvaluation'] = role != 'candidate_full'
            require(actual_options == expected_options, 'Known search did not retain its fixed tactical policy')
            require(1 <= searches[role]['nodes'] <= legacy.previous.NODES, 'Known search node budget differs')
        require(fingerprint(searches['baseline']) == fingerprint(searches['candidate_off']) and
                fingerprint(searches['candidate']) == fingerprint(searches['candidate_full']),
                'Known oracle flags do not match the actual move/score/depth/node/PV evidence')
        candidate = searches['candidate']
        require(candidate.get('completed_iteration') is True and candidate['depth'] >= 1 and
                candidate['move_uci'] == source['expected_move'],
                'Known position did not choose its fixed move in a completed iteration')
        first = candidate.get('first_iteration_nodes')
        require(type(first) is int and 1 <= first <= legacy.previous.NODES,
                'Every known position must finish its first iteration inside the fixed budget')
        if source['name'] == 'white_reply_after_Bf5':
            require(first <= BF5_FIRST_NODE_LIMIT, 'Bf5 first iteration exceeds its fixed prior cost')
    critical = cases[-1]['searches']['candidate']
    require(known.get('critical_first_iteration_completed') is True and
            known.get('critical_first_iteration_nodes') == critical['first_iteration_nodes'],
            'Ng5 first iteration receipt differs')
    for path, digest in known.get('input_sha256', {}).items():
        require(sha(path) == digest, 'Known-position input changed: ' + path)


def seal(directory, old_binary, new_binary, correctness, known_positions, protocol):
    directory, reservation, roots = authenticate_reservation(directory)
    require(not (directory / 'plan.json').exists(), 'Build seal already exists; no reselection')
    old_binary, new_binary, correctness, known_positions, protocol = map(
        lambda path: Path(path).resolve(), (old_binary, new_binary, correctness, known_positions, protocol))
    require(old_binary != new_binary and sha(old_binary) != sha(new_binary), 'Search binaries must differ')
    correct, known = read(correctness), read(known_positions)
    require(correct.get('completed') is True and correct.get('passed') is True and
            correct.get('native_mismatches') == 0, 'Completed correctness pass required')
    validate_known_receipt(known)
    for receipt in (correct, known):
        require(receipt.get('model_sha256') == MODEL_SHA, 'Prerequisite uses another model')
        require(receipt.get('old_binary_sha256') == sha(old_binary) and
                receipt.get('new_binary_sha256') == sha(new_binary), 'Prerequisite binary binding differs')
    profile = read(CANDIDATE_PROFILE)
    require(profile['options'] == dict(search_options()['candidate'],
            NeuralModelFile=profile['options']['NeuralModelFile']), 'Candidate profile differs')
    require((CANDIDATE_PROFILE.parent / profile['options']['NeuralModelFile']).resolve() == MODEL.resolve()
            and profile['models']['NeuralModelFile']['sha256'] == MODEL_SHA,
            'Candidate profile NNUE binding differs')
    require((CANDIDATE_PROFILE.parent / profile['binary']['path']).resolve() == new_binary and
            profile['binary']['sha256'] == sha(new_binary), 'Candidate profile binary binding differs')
    features, paths = delta_inventory(directory=directory)
    legacy.previous.assert_reserved_unexposed(roots, features)
    plan = deepcopy(reservation)
    plan.update(sealed_utc=datetime.now(timezone.utc).isoformat(),
                reservation_sha256=sha(directory / 'reservation.json'),
                old_binary_path=str(old_binary), old_binary_sha256=sha(old_binary),
                new_binary_path=str(new_binary), new_binary_sha256=sha(new_binary),
                correctness_path=str(correctness), known_positions_path=str(known_positions),
                protocol_path=str(protocol), native_build_binding='Immutable searches, correctness and strict known-position pass sealed before fresh answers')
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


@contextmanager
def configured():
    names = ('authenticate', 'search_options', 'exposure')
    saved = {name: getattr(legacy, name) for name in names}
    legacy.authenticate, legacy.search_options = authenticate, search_options
    legacy.exposure = SimpleNamespace(exposure_inventory=delta_inventory,
                                      position_features=saved['exposure'].position_features)
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(legacy, name, value)


def label(directory):
    with configured():
        return legacy.label(directory)


def run(directory, output):
    with configured():
        return legacy.run(directory, output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('reserve'); p.add_argument('directory', type=Path)
    p = sub.add_parser('seal')
    for name in ('directory', 'old_binary', 'new_binary', 'correctness', 'known_positions', 'protocol'):
        p.add_argument(name, type=Path)
    p = sub.add_parser('label'); p.add_argument('directory', type=Path)
    p = sub.add_parser('run'); p.add_argument('directory', type=Path); p.add_argument('output', type=Path)
    args = parser.parse_args()
    if args.command == 'reserve': value = reserve(args.directory)
    elif args.command == 'seal':
        value = seal(args.directory, args.old_binary, args.new_binary,
                     args.correctness, args.known_positions, args.protocol)
    elif args.command == 'label': value = label(args.directory)
    else:
        result = run(args.directory, args.output)
        value = {'completed': result['completed'], 'gate': result['gate']}
    print(json.dumps(value, indent=2))


if __name__ == '__main__':
    main()
