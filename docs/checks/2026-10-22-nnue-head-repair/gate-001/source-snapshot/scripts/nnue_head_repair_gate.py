"""Reserve a fresh output-head repair gate without teacher or model outcomes.

Recorded root wrappers, root_fen fields and legal PV paths are all exposure.
The old scorer is reused with explicitly sealed warm/build/exposure bindings.
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
import nnue_leaf_gate as leaf

ROOT, OPENINGS = previous.ROOT, previous.OPENINGS
WARM = ROOT / 'docs/checks/2026-10-22-nnue-output-only/attempt-001/model.nnue'
WARM_SHA = '4e100302474cc3861ee5ef2ec69a641dad0e08cc94c1b685f80c3bb2e8883700'
INDICES = list(range(1, 32, 2))
EXTENSION_PLIES = (4, 6, 8, 10)
TEACHER_DEPTH = 10
CRITERIA = deepcopy(previous.CRITERIA)
sha, read, write, require = previous.sha, previous.read, previous.write, previous.require
root_features = previous.root_features


@lru_cache(maxsize=262144)
def fen_feature(fen):
    return base.feature_identity(chess.Board(fen))


def replay_features(fen, moves):
    """Every legal prefix was inspected, including a truncated recorded PV."""
    if isinstance(moves, str):
        moves = moves.split()
    if not isinstance(moves, list) or not all(isinstance(x, str) for x in moves):
        return set()
    return _replay_features(fen, tuple(moves))


@lru_cache(maxsize=262144)
def _replay_features(fen, moves):
    board, result = chess.Board(fen), set()
    for text in moves:
        try:
            move = chess.Move.from_uci(text)
        except ValueError:
            break
        if move not in board.legal_moves:
            break
        board.push(move)
        result.add(fen_feature(board.fen(en_passant='fen')))
    return frozenset(result)


def position_features(value, board_context=None):
    """Extract direct FENs and inherit wrapped root context into engine PVs."""
    result = set()
    if isinstance(value, dict):
        for key in ('feature_hash', 'root_feature_hash'):
            identity = value.get(key)
            if isinstance(identity, str) and len(identity) == 64:
                result.add(identity)
        for key, fen in value.items():
            if isinstance(fen, str) and (key == 'fen' or key.endswith('_fen')):
                result.add(fen_feature(fen))
        local_fen = next((value.get(key) for key in ('fen', 'root_fen')
                          if isinstance(value.get(key), str)), None)
        if not isinstance(local_fen, str):
            for key in ('root', 'source'):
                wrapped = value.get(key)
                if isinstance(wrapped, dict) and isinstance(wrapped.get('fen'), str):
                    local_fen = wrapped['fen']
                    break
        if isinstance(local_fen, str):
            board_context = local_fen
        if board_context is not None:
            for key in ('pv_uci', 'pv', 'path_from_root'):
                result.update(replay_features(board_context, value.get(key)))
        initial = value.get('initial_fen', chess.STARTING_FEN)
        if isinstance(initial, str):
            for key in ('history', 'root_history', 'source_opening_history'):
                result.update(replay_features(initial, value.get(key)))
        for child in value.values():
            result.update(position_features(child, board_context))
    elif isinstance(value, list):
        for child in value:
            result.update(position_features(child, board_context))
    return result


def exposure_inventory(*, directory=None, checks=None):
    """Inventory all neural inputs, prior gates, probes and observed PV paths."""
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
            continue
        if not text.lstrip().startswith(('{', '[')):
            require('"fen"' not in text and '"root_fen"' not in text,
                    'Position-bearing non-JSON capture: ' + str(path))
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
        if features:
            paths.append(path.resolve())
            exposed.update(features)
    return exposed, paths


def select_roots(exposed, *, openings=None, indices=None):
    """Fixed families extend only for feature overlap, never outcome quality."""
    openings = read(OPENINGS)['openings'] if openings is None else openings
    indices = INDICES if indices is None else indices
    roots, excluded, blocked = [], [], set()
    for index in indices:
        opening = openings[index]
        initial = opening.get('initial_fen', chess.STARTING_FEN)
        board = chess.Board(initial)
        original = list(opening['moves'])
        for text in original:
            require(chess.Move.from_uci(text) in board.legal_moves, 'Illegal source opening')
            board.push_uci(text)
        require(board.fen(en_passant='fen') == chess.Board(opening['fen_after_opening']).fen(en_passant='fen'),
                'Opening FEN differs from complete history')
        extension, overlap = [], set()
        for length in EXTENSION_PLIES:
            while len(extension) < length:
                choices = [move for move in board.legal_moves
                           if not board.is_capture(move) and not board.gives_check(move)]
                require(bool(choices), 'Fixed family has no deterministic quiet continuation')
                move = min(choices, key=lambda value: value.uci())
                extension.append(move.uci())
                board.push(move)
            features = root_features(board)
            overlap = features & (exposed | blocked)
            if not overlap:
                break
            excluded.append({'index': index, 'extension_plies': length,
                             'reason': 'root_or_legal_child_exposed_or_reserved',
                             'overlap_features': sorted(overlap)})
        if overlap:
            continue
        roots.append({'query_id': f'head-repair-gate-opening-{index:02d}', 'source_index': index,
                      'source_path': str(OPENINGS), 'family_id': opening['family_id'],
                      'cluster_id': opening['cluster_id'], 'kind': 'quiet_extended_head_repair_gate',
                      'partition': 'gate', 'initial_fen': initial,
                      'history': original + extension, 'source_opening_history': original,
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
    require(sha(WARM) == WARM_SHA, 'Warm model differs')
    exposed, paths = exposure_inventory(directory=directory)
    roots, excluded, blocked = select_roots(exposed)
    require(len(roots) == 16, 'Require sixteen untouched fixed families; no outcome-driven refill')
    directory.mkdir(parents=True)
    write(directory / 'frozen-roots.json', roots)
    write(directory / 'blocked-features.json', sorted(blocked))
    bindings = [Path(__file__).resolve(), Path(leaf.__file__).resolve(), Path(previous.__file__).resolve(),
                ROOT / 'tests/nnue_head_repair_gate_tests.py',
                OPENINGS, WARM, previous.PROFILE, ROOT / 'config/engine-profiles/classical.json',
                ROOT / 'scripts/stockfish_teacher.py', ROOT / 'scripts/neural_training.py', *paths]
    result = {'schema_version': 1, 'created_utc': datetime.now(timezone.utc).isoformat(),
              'selection': 'Fixed odd source indices1..31; UCI-lexicographic first legal noncapture/noncheck moves for exactly4plies; only recorded feature overlap allows extending same path to6 then8 then10plies; no other refill, no teacher/model outputs',
              'indices': INDICES, 'extension_plies': list(EXTENSION_PLIES),
              'reserved_roots': len(roots), 'excluded': excluded,
              'exposure_files': len(paths), 'exposure_unique_features': len(exposed),
              'exposure_policy': 'Every recorded neural FEN/root_fen/source/root wrapper, complete history prefix, teacher/native/development PV continuation; all previous gate roots and legal children; color/rank-mirror canonical feature identity',
              'limitations': 'M2 source families were previously observed; only these extended roots and all legal children are absent from the recorded learning/selection/teacher/probe inventory. This is a small move-choice screen, not independent Elo evidence.',
              'criteria': deepcopy(CRITERIA), 'native_nodes': previous.NODES,
              'teacher_nodes': previous.TEACHER_NODES, 'teacher_depth': TEACHER_DEPTH,
              'max_root_teacher_queries': len(roots), 'max_child_teacher_queries': 2 * len(roots),
              'warm_model': str(WARM), 'warm_sha256': WARM_SHA,
              'frozen_roots_sha256': sha(directory / 'frozen-roots.json'),
              'blocked_features_sha256': sha(directory / 'blocked-features.json'),
              'input_sha256': {str(path): sha(path) for path in bindings},
              'gate_source_path': str(Path(__file__).resolve()),
              'no_test_driven_retraining_or_retries': True,
              'native_build_binding': 'Pending binary seal before current learning collection or fitting'}
    write(directory / 'reservation.json', result)
    return result


def seal(directory, binary):
    return leaf.seal(directory, binary)


@contextmanager
def configured(directory):
    plan = read(Path(directory) / 'plan.json')
    require(plan['criteria'] == CRITERIA and plan.get('teacher_depth') == TEACHER_DEPTH,
            'Head repair gate criteria/teacher depth changed')
    require(plan['warm_sha256'] == WARM_SHA and Path(plan['warm_model']) == WARM,
            'Head repair warm model differs')
    names = ('BINARY', 'BINARY_SHA', 'WARM', 'WARM_SHA', 'exposure_inventory')
    saved = {name: getattr(previous, name) for name in names}
    previous.BINARY, previous.BINARY_SHA = Path(plan['binary_path']), plan['binary_sha256']
    previous.WARM, previous.WARM_SHA = WARM, WARM_SHA
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
    sub.add_parser('inventory')
    p = sub.add_parser('reserve'); p.add_argument('directory', type=Path)
    p = sub.add_parser('seal'); p.add_argument('directory', type=Path); p.add_argument('binary', type=Path)
    p = sub.add_parser('label'); p.add_argument('directory', type=Path)
    p = sub.add_parser('run'); p.add_argument('directory', type=Path); p.add_argument('candidate', type=Path); p.add_argument('output', type=Path)
    args = parser.parse_args()
    if args.command == 'inventory':
        exposed, paths = exposure_inventory()
        roots, excluded, blocked = select_roots(exposed)
        value = {'exposure_files': len(paths), 'exposed_features': len(exposed),
                 'eligible_indices': [row['source_index'] for row in roots],
                 'extension_plies': [len(row['deterministic_extension_uci']) for row in roots],
                 'excluded': excluded, 'blocked_features': len(blocked)}
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
