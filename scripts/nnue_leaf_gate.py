"""Reserve a fresh, recorded-input-disjoint move gate before leaf learning.

Reservation inspects positions only, never model moves or teacher scores.
The final native build is sealed separately before fitting. Scoring reuses the
previous gate's unchanged criteria and exact-CP receipt validation.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from functools import lru_cache
import json
from pathlib import Path

import chess

import neural_training as base
import nnue_generalization_gate as previous
from stockfish_teacher import source_board

ROOT = previous.ROOT
OPENINGS = previous.OPENINGS
WARM = previous.WARM
WARM_SHA = previous.WARM_SHA
CRITERIA = deepcopy(previous.CRITERIA)
INDICES = [6, 7, 10, 11, 15, 17, 18, 20, 22, 23, 24, 25, 26, 27, 28, 30]
TEACHER_DEPTH = 10
sha, read, write, require = previous.sha, previous.read, previous.write, previous.require
root_features = previous.root_features


@lru_cache(maxsize=262144)
def fen_feature(fen):
    return base.feature_identity(chess.Board(fen))


def position_features(value, board_context=None):
    """Extract observed FENs, recorded feature IDs and legal PV continuations."""
    result = set()
    if isinstance(value, dict):
        for key in ('feature_hash', 'root_feature_hash'):
            identity = value.get(key)
            if isinstance(identity, str) and len(identity) == 64:
                result.add(identity)
        source = value.get('source', {})
        fen = value.get('fen', source.get('fen') if isinstance(source, dict) else None)
        if isinstance(fen, str):
            board_context = fen
            result.add(fen_feature(fen))
        if board_context is not None:
            board = chess.Board(board_context)
            # Every recorded teacher/native continuation is development-exposed.
            for key in ('pv_uci', 'pv'):
                moves = value.get(key)
                if not isinstance(moves, list) or not all(isinstance(x, str) for x in moves):
                    continue
                replay = board.copy(stack=False)
                for text in moves:
                    move = chess.Move.from_uci(text)
                    if move not in replay.legal_moves:
                        break
                    replay.push(move)
                    result.add(fen_feature(replay.fen(en_passant='fen')))
        for child in value.values():
            result.update(position_features(child, board_context))
    elif isinstance(value, list):
        for child in value:
            result.update(position_features(child, board_context))
    return result


def exposure_inventory(*, directory=None, checks=None):
    """Include all recorded neural datasets, teacher queries and probe outputs.

    A gate's own reservation/answers are excluded when checking its later
    training leakage. Other gates, including all their legal children, count
    as prior development exposure. Source snapshots duplicate original data.
    """
    checks = Path(checks or ROOT / 'docs/checks').resolve()
    omitted = Path(directory).resolve() if directory is not None else None
    exposed, paths = set(), []
    for path in sorted(checks.rglob('*')):
        if path.suffix not in ('.json', '.jsonl') or 'source-snapshot' in path.parts:
            continue
        if omitted is not None and path.resolve().is_relative_to(omitted):
            continue
        if not any(token in path.relative_to(checks).as_posix().lower()
                   for token in ('neural', 'nnue', 'm3-stockfish')):
            continue
        text = path.read_text(encoding='utf-8')
        if not text.strip():
            continue  # Empty historical capture files contain no positions.
        if not text.lstrip().startswith(('{', '[')):
            # Some historical *.json files hold plain PASS/status transcripts,
            # not structured training/query/probe rows.
            require('"fen"' not in text, 'Position-bearing non-JSON capture: ' + str(path))
            continue
        try:
            values = ([json.loads(line) for line in text.splitlines() if line.strip()]
                      if path.suffix == '.jsonl' else [json.loads(text)])
        except json.JSONDecodeError as exc:
            raise ValueError('Malformed position inventory JSON: ' + str(path)) from exc
        features = set().union(*(position_features(value) for value in values)) if values else set()
        if path.name == 'blocked-features.json':
            require(len(values) == 1 and isinstance(values[0], list), 'Malformed previous gate block')
            features.update(values[0])
        # Only position-bearing files need sealing, avoiding unrelated outputs
        # being written by parallel work after reservation.
        if features:
            paths.append(path.resolve())
            exposed.update(features)
    return exposed, paths


def select_roots(exposed, *, openings=None, indices=None):
    openings = read(OPENINGS)['openings'] if openings is None else openings
    indices = INDICES if indices is None else indices
    roots, excluded, blocked = [], [], set()
    for index in indices:
        opening = openings[index]
        board = chess.Board(opening.get('initial_fen', chess.STARTING_FEN))
        for text in opening['moves']:
            require(chess.Move.from_uci(text) in board.legal_moves, 'Illegal source opening')
            board.push_uci(text)
        require(board.fen(en_passant='fen') == chess.Board(opening['fen_after_opening']).fen(en_passant='fen'),
                'Opening history differs from source FEN')
        original_history = list(opening['moves'])
        extension = []
        features = root_features(board)
        overlap = features & (exposed | blocked)
        if overlap:
            excluded.append({'index': index, 'extension_plies': 0,
                             'reason': 'root_or_legal_child_exposed_or_reserved',
                             'overlap_features': sorted(overlap)})
        # Only index 27 is eligible for the predeclared, purely legal extension.
        # This is fixed before teacher labels or candidate searches are observed.
        for extension_plies in (2, 4) if index == 27 and overlap else ():
            while len(extension) < extension_plies:
                choices = [move for move in board.legal_moves
                           if not board.is_capture(move) and not board.gives_check(move)]
                require(bool(choices), 'No deterministic quiet continuation')
                move = min(choices, key=lambda item: item.uci())
                extension.append(move.uci())
                board.push(move)
            features = root_features(board)
            overlap = features & (exposed | blocked)
            if not overlap:
                break
            excluded.append({'index': index, 'extension_plies': extension_plies,
                             'reason': 'root_or_legal_child_exposed_or_reserved',
                             'overlap_features': sorted(overlap)})
        if overlap:
            continue
        roots.append({'query_id': f'leaf-gate-opening-{index:02d}', 'source_index': index,
                      'source_path': str(OPENINGS), 'family_id': opening['family_id'],
                      'cluster_id': opening['cluster_id'], 'kind': 'leaf_opening_gate',
                      'partition': 'gate', 'initial_fen': opening.get('initial_fen', chess.STARTING_FEN),
                      'history': original_history + extension,
                      'source_opening_history': original_history,
                      'deterministic_extension_uci': extension,
                      'fen': board.fen(en_passant='fen'),
                      'feature_hash': base.feature_identity(board),
                      'blocked_feature_hashes': sorted(features), 'target_white_cp': 0})
        blocked.update(features)
    return roots, excluded, blocked


def reserve(directory):
    directory = Path(directory).resolve()
    require(not directory.exists(), 'Refuse to replace a reservation')
    require(sha(WARM) == WARM_SHA, 'Warm model differs')
    exposed, paths = exposure_inventory(directory=directory)
    roots, excluded, blocked = select_roots(exposed)
    require(len(roots) == 16, 'Require sixteen untouched fixed-index roots; no outcome-driven refill')
    directory.mkdir(parents=True)
    write(directory / 'frozen-roots.json', roots)
    write(directory / 'blocked-features.json', sorted(blocked))
    bindings = [Path(__file__).resolve(), Path(previous.__file__).resolve(), OPENINGS, WARM,
                previous.PROFILE, ROOT / 'config/engine-profiles/classical.json',
                ROOT / 'scripts/stockfish_teacher.py', ROOT / 'scripts/neural_training.py', *paths]
    result = {'schema_version': 1, 'created_utc': datetime.now(timezone.utc).isoformat(),
              'selection': 'Fixed remaining16 M2 source indices, positions/legality only; index27 may advance2 then4plies by UCI-lexicographic first legal noncapture/noncheck moves if prior exposure excludes its root; no other refill, no teacher/model outcomes',
              'indices': INDICES, 'reserved_roots': len(roots), 'excluded': excluded,
              'exposure_files': len(paths), 'exposure_unique_features': len(exposed),
              'exposure_policy': 'Every recorded neural FEN, teacher query, native/development probe and legal PV continuation; all previous gate roots and all legal children; color/rank-mirror canonical feature identity',
              'limitations': 'Project-authored opening sources appeared in past M2 gameplay. Untouched means excluded from all recorded neural learning, checkpoint selection and prior teacher/probe positions, not unseen chess theory or independent Elo evidence.',
              'criteria': deepcopy(CRITERIA), 'native_nodes': previous.NODES,
              'teacher_nodes': previous.TEACHER_NODES, 'teacher_depth': TEACHER_DEPTH,
              'max_root_teacher_queries': len(roots), 'max_child_teacher_queries': 2 * len(roots),
              'warm_model': str(WARM), 'warm_sha256': WARM_SHA,
              'frozen_roots_sha256': sha(directory / 'frozen-roots.json'),
              'blocked_features_sha256': sha(directory / 'blocked-features.json'),
              'input_sha256': {str(path): sha(path) for path in bindings},
              'gate_source_path': str(Path(__file__).resolve()),
              'no_test_driven_retraining_or_retries': True,
              'native_build_binding': 'Pending final same-behavior observer build; seal before collecting learning data or fitting'}
    write(directory / 'reservation.json', result)
    return result


def seal(directory, binary):
    directory, binary = Path(directory).resolve(), Path(binary).resolve()
    require(not (directory / 'plan.json').exists(), 'Native gate plan already sealed')
    reservation = read(directory / 'reservation.json')
    for path, digest in reservation['input_sha256'].items():
        require(sha(path) == digest, 'Reservation input changed: ' + path)
    for filename, field in [('frozen-roots.json', 'frozen_roots_sha256'),
                            ('blocked-features.json', 'blocked_features_sha256')]:
        require(sha(directory / filename) == reservation[field], 'Reservation changed')
    require(binary.is_file(), 'Native binary missing')
    plan = deepcopy(reservation)
    plan.update(sealed_utc=datetime.now(timezone.utc).isoformat(), binary_path=str(binary),
                binary_sha256=sha(binary), reservation_sha256=sha(directory / 'reservation.json'),
                native_build_binding='Sealed before current learning collection or fitting')
    plan['input_sha256'].update({str(binary): sha(binary),
                               str(directory / 'reservation.json'): sha(directory / 'reservation.json')})
    write(directory / 'plan.json', plan)
    return plan


@contextmanager
def configured(directory):
    """Reuse the frozen scorer; only explicit sealed build/exposure bindings vary."""
    plan = read(Path(directory) / 'plan.json')
    require(plan['criteria'] == CRITERIA and plan.get('teacher_depth') == TEACHER_DEPTH,
            'Leaf gate criteria/teacher depth changed')
    require(plan['warm_sha256'] == WARM_SHA and Path(plan['warm_model']) == WARM,
            'Leaf warm model differs')
    names = ('BINARY', 'BINARY_SHA', 'exposure_inventory')
    saved = {name: getattr(previous, name) for name in names}
    previous.BINARY, previous.BINARY_SHA = Path(plan['binary_path']), plan['binary_sha256']
    previous.exposure_inventory = lambda: exposure_inventory(directory=directory)
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(previous, name, value)


def label(directory):
    with configured(directory):
        return previous.label(directory)


def run(directory, candidate, output):
    with configured(directory):
        return previous.run(directory, candidate, output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('inventory')
    p = sub.add_parser('reserve'); p.add_argument('directory', type=Path)
    p = sub.add_parser('seal'); p.add_argument('directory', type=Path); p.add_argument('binary', type=Path)
    p = sub.add_parser('label'); p.add_argument('directory', type=Path)
    p = sub.add_parser('run'); p.add_argument('directory', type=Path); p.add_argument('candidate', type=Path); p.add_argument('output', type=Path)
    args = parser.parse_args()
    if args.command == 'inventory':
        exposed, paths = exposure_inventory()
        roots, excluded, blocked = select_roots(exposed)
        value = {'exposure_files': len(paths), 'exposed_features': len(exposed),
                 'eligible_indices': [row['source_index'] for row in roots], 'excluded': excluded,
                 'blocked_features': len(blocked)}
    elif args.command == 'reserve': value = reserve(args.directory)
    elif args.command == 'seal': value = seal(args.directory, args.binary)
    elif args.command == 'label': value = label(args.directory)
    else:
        result = run(args.directory, args.candidate, args.output)
        value = {'completed': result['completed'], 'gate': result['gate'],
                 'elapsed_seconds': result.get('elapsed_seconds')}
    print(json.dumps(value, indent=2))


if __name__ == '__main__':
    main()
