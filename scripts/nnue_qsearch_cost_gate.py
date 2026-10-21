"""Adopt an unused position-only screen for bounded tactical search costs.

The old reservation is immutable and has never been searched or labelled. Both
searches enable the same tactical extension and use the same frozen NNUE. Only
the candidate enables the new cache and root quiet-fork priority. No move is
removed. Existing label/scoring code is reused with
explicit, temporary dependency bindings; historical scripts remain unchanged.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import nnue_tactical_search_gate as legacy
from check_nnue_tactical_search import known_cases

ROOT, MODEL, MODEL_SHA = legacy.ROOT, legacy.MODEL, legacy.MODEL_SHA
CRITERIA = deepcopy(legacy.CRITERIA)
TEACHER_DEPTH = legacy.TEACHER_DEPTH
SOURCE = ROOT / 'docs/checks/2026-10-22-nnue-tactical-search/gate-001'
EXPERIMENT = ROOT / 'docs/checks/2026-10-22-nnue-qsearch-cost'
CANDIDATE_PROFILE = ROOT / 'config/engine-profiles/nnue-qsearch-cost.json'
CACHE_OPTION = 'UseQuiescenceThreatCache'
ORDER_OPTION = 'UseTacticalThreatOrdering'
SOURCE_FILES = frozenset(('reservation.json', 'frozen-roots.json',
                          'blocked-features.json', 'position-only-verification.json'))
sha, read, write, require = legacy.sha, legacy.read, legacy.write, legacy.require


def search_options():
    common = dict(read(ROOT / 'config/engine-profiles/classical.json')['options'],
                  UseNNUEEvaluation=True, NeuralModelFile=str(MODEL),
                  UseQuiescenceThreats=True)
    # The historical binary predates this option; absence is its disabled path.
    return {'warm': common, 'candidate': dict(common, **{CACHE_OPTION: True, ORDER_OPTION: True})}


def authenticate_source(source=None):
    source = Path(source or SOURCE).resolve()
    require({p.name for p in source.iterdir()} == SOURCE_FILES,
            'Original reservation was queried, sealed, consumed, or changed')
    directory, reservation, roots = legacy.authenticate_reservation(source)
    audit = read(directory / 'position-only-verification.json')
    require(audit.get('completed') is True and audit.get('passed') is True and
            audit.get('native_queries') == 0 and audit.get('teacher_queries') == 0 and
            audit.get('training_runs') == 0 and audit.get('overlap_features') == 0 and
            audit.get('reservation_sha256') == sha(directory / 'reservation.json'),
            'Original zero-query position-only audit is invalid')
    blocked = set(read(directory / 'blocked-features.json'))
    recomputed = set().union(*(legacy.root_features(legacy.source_board(row)) for row in roots))
    require(len(roots) == 16 and len(blocked) == 594 and blocked == recomputed,
            'Original sixteen roots or legal children differ')
    return directory, reservation, roots, blocked


def delta_inventory(*, directory=None, source=None, experiment=None):
    """Reuse authenticated historical exclusion; replay only later diagnostics.

    Every historical position-bearing input hash is checked by the original
    reservation authentication. The only later development is in the previous
    tactical experiment and this experiment. Their new receipts are replayed,
    including wrapped FENs, histories and PVs. Reserved bytes are excluded.
    """
    source, reservation, roots, blocked = authenticate_source(source)
    omitted = Path(directory).resolve() if directory is not None else None
    experiment = Path(experiment or EXPERIMENT).resolve()
    original = {Path(path).resolve() for path in reservation['input_sha256']}
    features, paths = set(), []
    for folder in (source.parent, experiment):
        if not folder.exists():
            continue
        for path in sorted(folder.rglob('*')):
            if path.suffix not in ('.json', '.jsonl') or 'source-snapshot' in path.parts:
                continue
            resolved = path.resolve()
            if (resolved.is_relative_to(source) or
                    resolved.is_relative_to(experiment / 'gate-001') or
                    (omitted and resolved.is_relative_to(omitted))):
                continue
            if resolved in original:
                continue
            text = path.read_text(encoding='utf-8')
            if not text.strip():
                continue
            if not text.lstrip().startswith(('{', '[')):
                require('"fen"' not in text and '"root_fen"' not in text,
                        'Position-bearing non-JSON diagnostic: ' + str(path))
                continue
            values = ([json.loads(line) for line in text.splitlines() if line.strip()]
                      if path.suffix == '.jsonl' else [json.loads(text)])
            found = set().union(*(legacy.exposure.position_features(value) for value in values))
            if path.name == 'blocked-features.json':
                require(len(values) == 1 and isinstance(values[0], list), 'Malformed feature block')
                found.update(values[0])
            # All later JSON inputs, not just nonempty position inputs, are bound.
            paths.append(resolved)
            features.update(found)
    require(not blocked & features, 'Reserved root or legal child entered later diagnostics')
    return features, paths


def authenticate_superseded(source):
    superseded = EXPERIMENT / 'gate-001'
    snapshot = EXPERIMENT / 'pre-ordering-source-snapshot/manifest.json'
    require(superseded.exists() and snapshot.exists(),
            'Ordering amendment requires the preserved unused cache-only reservation and source archive')
    previous_reservation = read(superseded / 'reservation.json')
    require(not (superseded / 'plan.json').exists() and not (superseded / 'label-started.json').exists() and
            not (superseded / 'consumed.json').exists() and not (superseded / 'root-teacher').exists(),
            'Superseded cache-only reservation has already produced gate outcomes')
    archived = {row['original_path']: row for row in read(snapshot)['files']}
    for path, digest in previous_reservation['input_sha256'].items():
        resolved = Path(path)
        if path in archived:
            row = archived[path]
            require(row['sha256'] == digest and sha(row['archived_path']) == digest,
                    'Superseded gate source archive differs: ' + path)
        else:
            require(sha(resolved) == digest, 'Superseded gate non-source input changed: ' + path)
    require((superseded / 'frozen-roots.json').read_bytes() == (source / 'frozen-roots.json').read_bytes() and
            (superseded / 'blocked-features.json').read_bytes() == (source / 'blocked-features.json').read_bytes(),
            'Superseded and new adopted gate position bytes differ')
    return superseded, snapshot, archived


def reserve(directory):
    directory = Path(directory).resolve()
    require(not directory.exists(), 'Refuse to replace a reservation')
    source, original, roots, blocked = authenticate_source()
    require(sha(MODEL) == MODEL_SHA, 'Frozen output-only NNUE changed')
    superseded, snapshot, archived = authenticate_superseded(source)
    require(directory != superseded, 'Refuse to replace the superseded reservation')
    features, paths = delta_inventory(directory=directory)
    directory.mkdir(parents=True)
    for name in ('frozen-roots.json', 'blocked-features.json'):
        (directory / name).write_bytes((source / name).read_bytes())
    bindings = [Path(__file__).resolve(), ROOT / 'tests/nnue_qsearch_cost_gate_tests.py',
                Path(legacy.__file__).resolve(), *[source / name for name in SOURCE_FILES],
                *map(Path, original['input_sha256']), snapshot,
                *[Path(row['archived_path']) for row in archived.values()],
                *[superseded / name for name in ('reservation.json', 'frozen-roots.json',
                                                  'blocked-features.json', 'position-only-verification.json')], *paths]
    value = {'schema_version': 1, 'created_utc': datetime.now(timezone.utc).isoformat(),
             'source_reservation': str(source), 'source_reservation_sha256': sha(source / 'reservation.json'),
             'superseded_unused_cache_only_reservation': str(superseded),
             'superseded_reservation_sha256': sha(superseded / 'reservation.json'),
             'pre_ordering_source_archive_manifest': str(snapshot),
             'selection': 'Byte-identical adoption of the unused tactical-search sixteen-root reservation; zero prior teacher/native queries. No new selection or outcome-driven refill.',
             'reserved_roots': len(roots), 'blocked_root_and_legal_child_features': len(blocked),
             'historical_exposure_files': original['exposure_files'],
             'historical_exposure_unique_features': original['exposure_unique_features'],
             'later_diagnostic_features': len(features), 'later_overlap_features': 0,
             'later_diagnostic_files': len(paths),
             'exposure_policy': 'Authenticate every original input hash; replay only later tactical and qsearch-cost experiment JSON diagnostics. All FENs, wrapped roots, complete histories and legal PV prefixes count. No model or training changes.',
             'limitations': original['limitations'], 'criteria': deepcopy(CRITERIA),
             'native_nodes': legacy.previous.NODES, 'teacher_nodes': legacy.previous.TEACHER_NODES,
             'teacher_depth': TEACHER_DEPTH, 'max_root_teacher_queries': 16,
             'max_child_teacher_queries': 32, 'model_path': str(MODEL), 'model_sha256': MODEL_SHA,
             'comparison': 'Both old guarded and new searches enable UseQuiescenceThreats=true. Same NNUE/common options/node budget; candidate adds UseQuiescenceThreatCache=true and UseTacticalThreatOrdering=true. Ordering changes priority only, without pruning.',
             'frozen_roots_sha256': sha(directory / 'frozen-roots.json'),
             'blocked_features_sha256': sha(directory / 'blocked-features.json'),
             'input_sha256': {str(path): sha(path) for path in bindings},
             'native_build_binding': 'Pending both immutable binaries, correctness and strict three-position pass',
             'no_test_driven_retraining_or_retries': True}
    write(directory / 'reservation.json', value)
    return value


def authenticate_reservation(directory):
    directory = Path(directory).resolve()
    reservation = read(directory / 'reservation.json')
    source, original, roots, blocked = authenticate_source(reservation['source_reservation'])
    superseded, snapshot, archived = authenticate_superseded(source)
    require(reservation['superseded_reservation_sha256'] == sha(superseded / 'reservation.json'),
            'Superseded reservation changed')
    require(reservation['source_reservation_sha256'] == sha(source / 'reservation.json'),
            'Source reservation changed')
    require(reservation['criteria'] == CRITERIA and
            reservation['native_nodes'] == legacy.previous.NODES and
            reservation['teacher_nodes'] == legacy.previous.TEACHER_NODES and
            reservation['teacher_depth'] == TEACHER_DEPTH, 'Frozen criteria or budgets differ')
    require(reservation['model_path'] == str(MODEL) and reservation['model_sha256'] == MODEL_SHA,
            'Frozen model differs')
    for name, key in (('frozen-roots.json', 'frozen_roots_sha256'),
                      ('blocked-features.json', 'blocked_features_sha256')):
        require(sha(directory / name) == reservation[key] == sha(source / name),
                'Adopted gate bytes differ: ' + name)
    for path, digest in reservation['input_sha256'].items():
        require(sha(path) == digest, 'Frozen reservation input changed: ' + path)
    return directory, reservation, roots


def validate_known_receipt(known):
    require(known.get('completed') is True and known.get('known_position_passed') is True,
            'Strict problem-position pass required before fresh labels')
    require(known.get('native_mismatches') == 0 and known.get('training_runs') == 0 and
            known.get('teacher_queries') == 0 and known.get('native_searches') == 12 and
            known.get('nodes_per_search') == legacy.previous.NODES,
            'Known-position correctness, query count or node budget differs')
    cases = known.get('cases', [])
    expected = known_cases()
    require(len(cases) == len(expected) == 3, 'Exactly the three fixed known positions are required')
    for row, source in zip(cases, expected):
        require(row.get('source') == source and row.get('passed') is True and
                row.get('disabled_path_equal') is True and row.get('full_incremental_equal') is True,
                'Fixed known position or oracle equality differs')
        searches = row['searches']
        for role in ('baseline', 'candidate_off', 'candidate', 'candidate_full'):
            result = searches[role]
            options = result['options']
            require(options.get('UseQuiescenceThreats') is True and
                    options.get(CACHE_OPTION, False) is (role in ('candidate', 'candidate_full')) and
                    options.get(ORDER_OPTION, False) is (role in ('candidate', 'candidate_full')),
                    'Known search did not retain the same tactical policy')
            require(1 <= result['nodes'] <= legacy.previous.NODES, 'Known search node budget differs')
        candidate = searches['candidate']
        require(candidate.get('completed_iteration') is True and candidate['depth'] >= 1 and
                candidate['move_uci'] == source['expected_move'],
                'Known position did not choose its fixed move in a completed iteration')
    critical = cases[-1]['searches']['candidate']
    first_nodes = known.get('critical_first_iteration_nodes')
    require(known.get('critical_first_iteration_completed') is True and
            type(first_nodes) is int and 1 <= first_nodes <= legacy.previous.NODES and
            first_nodes == critical.get('first_iteration_nodes'),
            'Ng5 position must finish its first iteration inside the fixed node budget')
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
                receipt.get('new_binary_sha256') == sha(new_binary), 'Prerequisite binary bindings differ')
    profile = read(CANDIDATE_PROFILE)
    require(profile['options'].get('UseQuiescenceThreats') is True and
            profile['options'].get(CACHE_OPTION) is True and
            profile['options'].get(ORDER_OPTION) is True,
            'Candidate profile must enable threats, cache and root ordering')
    features, paths = delta_inventory(directory=directory)
    legacy.previous.assert_reserved_unexposed(roots, features)
    plan = deepcopy(reservation)
    plan.update(sealed_utc=datetime.now(timezone.utc).isoformat(),
                reservation_sha256=sha(directory / 'reservation.json'),
                old_binary_path=str(old_binary), old_binary_sha256=sha(old_binary),
                new_binary_path=str(new_binary), new_binary_sha256=sha(new_binary),
                correctness_path=str(correctness), known_positions_path=str(known_positions),
                protocol_path=str(protocol), native_build_binding='Both immutable searches, correctness and strict known-position pass sealed before fresh labels')
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
    """Bind shared one-shot execution code; restore globals even on failure."""
    names = ('authenticate', 'search_options', 'exposure')
    saved = {name: getattr(legacy, name) for name in names}
    legacy.authenticate, legacy.search_options = authenticate, search_options
    # Keep the real extractor in delta_inventory; only its inventory dependency
    # is replaced during shared execution, never the historical source file.
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
