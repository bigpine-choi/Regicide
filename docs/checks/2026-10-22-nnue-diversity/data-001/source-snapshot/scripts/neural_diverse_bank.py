"""Reuse legal source-game positions for one bounded, diverse teacher batch.

No games, moves, labels, or new partition assignments are generated here.
Original family splits are retained; piece-only color/rank mirror identities
are deliberately stricter than the current NNUE's piece-plus-turn identity.
"""
from __future__ import annotations

from collections import Counter, defaultdict, deque
import hashlib
import json
from pathlib import Path

import chess

from neural_search_data.schema import feature_hash, sha256, state_hash


PARTITIONS = ('train', 'validation', 'test')
PHASE_WEIGHTS = {'early': 50, 'middle': 35, 'late': 15}
DEFAULT_COUNTS = {'train': 1600, 'validation': 300, 'test': 100}


def _rows(path):
    return [json.loads(line) for line in Path(path).read_text(encoding='utf-8').splitlines()]


def _rank(seed: int, value: str) -> str:
    return hashlib.sha256(f'{seed}:{value}'.encode('utf-8')).hexdigest()


def _phase(board: chess.Board) -> tuple[str, int]:
    pieces = len(board.piece_map())
    return ('early' if pieces >= 28 else 'middle' if pieces >= 16 else 'late'), pieces


def distribution(rows: list[dict]) -> dict:
    """Describe material, mover, checks, and available captures without scores."""
    phases, movers, kinds, families = Counter(), Counter(), Counter(), set()
    checks = capture_positions = 0
    by_partition = {}
    for row in rows:
        board = chess.Board(row['fen'])
        phase, _ = _phase(board)
        phases[phase] += 1
        movers['white' if board.turn else 'black'] += 1
        kinds[row['kind']] += 1
        families.add(row['family_id'])
        checks += int(board.is_check())
        capture_positions += int(any(board.is_capture(move) for move in board.legal_moves))
        by_partition.setdefault(row['partition'], []).append(row)
    result = {'rows': len(rows), 'phases': dict(phases), 'movers': dict(movers),
              'kinds': dict(kinds), 'families': len(families),
              'in_check': checks, 'legal_capture_available': capture_positions}
    if len(by_partition) > 1:
        result['by_partition'] = {key: distribution(value) for key, value in by_partition.items()}
    return result


def _match_features(path) -> set[str]:
    """Exclude every declared match-opening prefix, including mirrored pieces."""
    if path is None:
        return set()
    value = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    openings = value if isinstance(value, list) else value.get('openings')
    if not isinstance(openings, list):
        raise ValueError('Match openings require an openings list')
    identities = set()
    for opening in openings:
        if not isinstance(opening, dict):
            raise ValueError('Each match opening must be an object')
        board = chess.Board(opening.get('initial_fen', chess.STARTING_FEN))
        moves = opening.get('moves', opening.get('moves_uci', opening.get('history', [])))
        if not isinstance(moves, list):
            raise ValueError('Match opening moves must be a list')
        identities.add(feature_hash(board))
        for text in moves:
            move = chess.Move.from_uci(text)
            if move not in board.legal_moves:
                raise ValueError('Illegal declared match-opening move')
            board.push(move)
            identities.add(feature_hash(board))
    return identities


def _source_pool(source_root: Path) -> tuple[list[dict], dict, list[dict]]:
    """Replay each source game once and check every query's declared history."""
    pool, assignments, artifacts = [], {}, []
    for source_name in ('trainval', 'heldout'):
        directory = source_root / source_name
        paths = {name: directory / name for name in ('bank.json', 'games.jsonl', 'queries.jsonl')}
        bank = json.loads(paths['bank.json'].read_text(encoding='utf-8'))
        current_assignments = bank['master_partition']
        if any(key in assignments and assignments[key] != value
               for key, value in current_assignments.items()):
            raise ValueError('Source banks disagree on original family partitions')
        assignments.update(current_assignments)
        queries = _rows(paths['queries.jsonl'])
        games = _rows(paths['games.jsonl'])
        query_map, seen = {}, set()
        for query in queries:
            key = (query['game_id'], len(query['history']))
            if key in query_map:
                raise ValueError('Multiple queries at one source-game history')
            query_map[key] = query
        for game in games:
            board = chess.Board(game['initial_fen'])
            if not board.is_valid():
                raise ValueError('Invalid source-game initial position')
            history = []
            for ply in range(len(game['moves_uci']) + 1):
                key = (game['game_id'], ply)
                query = query_map.get(key)
                if query is not None:
                    seen.add(key)
                    family, partition = query['family_id'], query['partition']
                    if (family != game['family_id'] or partition != game['partition']
                            or assignments.get(family) != partition or partition not in PARTITIONS
                            or query['initial_fen'] != game['initial_fen']
                            or query['history'] != history
                            or chess.Board(query['fen']).fen(en_passant='fen') != board.fen(en_passant='fen')):
                        raise ValueError('Source query does not match its original game and family split')
                    identity = feature_hash(board)
                    if query.get('feature_hash') != identity:
                        raise ValueError('Source query piece-feature identity mismatch')
                    if board.outcome(claim_draw=True) is None:
                        phase, pieces = _phase(board)
                        pool.append({
                            'query_id': f'diverse-{source_name}-{query["query_id"]}',
                            'source_query_id': query['query_id'],
                            'source_query_file': str(paths['queries.jsonl'].resolve()),
                            'source_game_id': game['game_id'],
                            'source_game_file': str(paths['games.jsonl'].resolve()),
                            'source_history_plies': ply,
                            'initial_fen': game['initial_fen'], 'history': history.copy(),
                            'fen': board.fen(en_passant='fen'), 'partition': partition,
                            'family_id': family, 'kind': query['kind'], 'subtype': query['subtype'],
                            'feature_hash': identity,
                            'state_hash': state_hash(game['initial_fen'], history),
                            'phase': phase, 'piece_count': pieces,
                            'turn': 'white' if board.turn else 'black',
                            'in_check': board.is_check(),
                            'legal_capture_count': sum(board.is_capture(move) for move in board.legal_moves),
                            'source': 'Unmodified replay of existing project-authored Regicide source game',
                        })
                if ply < len(game['moves_uci']):
                    move = chess.Move.from_uci(game['moves_uci'][ply])
                    if move not in board.legal_moves:
                        raise ValueError('Illegal source-game move')
                    board.push(move)
                    history.append(move.uci())
        if seen != set(query_map):
            raise ValueError('Source query lacks an original game state')
        artifacts.extend({'path': str(path.resolve()), 'sha256': sha256(path)} for path in paths.values())
    return pool, assignments, artifacts


def _select(rows: list[dict], count: int, seed: int) -> list[dict]:
    """Round-robin source families and movers within fixed material quotas."""
    selected = []
    quotas = {phase: count * weight // 100 for phase, weight in PHASE_WEIGHTS.items()}
    quotas['early'] += count - sum(quotas.values())
    used = set()
    for phase in PHASE_WEIGHTS:
        buckets = defaultdict(list)
        for row in rows:
            if row['phase'] == phase:
                buckets[(row['family_id'], row['turn'])].append(row)
        buckets = {key: deque(sorted(value, key=lambda row: _rank(seed, row['query_id'])))
                   for key, value in buckets.items()}
        keys = sorted(buckets, key=lambda key: _rank(seed, ':'.join(key)))
        phase_selected = []
        while len(phase_selected) < quotas[phase] and any(buckets.values()):
            for key in keys:
                if buckets[key] and len(phase_selected) < quotas[phase]:
                    row = buckets[key].popleft()
                    phase_selected.append(row)
                    used.add(row['feature_hash'])
        selected.extend(phase_selected)
    # Keep the bound even for a smaller fixture or exhausted phase, and report
    # the actual distribution instead of inventing extra positions.
    remainder = sorted((row for row in rows if row['feature_hash'] not in used),
                       key=lambda row: _rank(seed, row['query_id']))
    selected.extend(remainder[:count - len(selected)])
    if len(selected) != count:
        raise ValueError(f'Insufficient nonoverlapping source rows: need {count}, have {len(rows)}')
    return sorted(selected, key=lambda row: _rank(seed, row['query_id']))


def build_bank(source_root, reused_rows: list[dict], *, counts=None, seed: int = 20261022,
               match_openings_path=None) -> tuple[list[dict], dict]:
    """Return new teacher-query rows and their frozen source/selection manifest.

    The caller writes artifacts and obtains new teacher labels. Existing labels
    stay untouched. Selection never reads previous engine targets or outcomes.
    The test split is unused by this fine-tune/selection, not newly unseen data:
    the source bank has appeared in earlier development and analysis.
    """
    counts = dict(DEFAULT_COUNTS if counts is None else counts)
    if (set(counts) != set(PARTITIONS) or any(type(value) is not int or value < 0 for value in counts.values())
            or not sum(counts.values()) or type(seed) is not int):
        raise ValueError('Require nonnegative train/validation/test row budgets and an integer seed')
    source_root = Path(source_root).resolve()
    pool, assignments, artifacts = _source_pool(source_root)
    old_features, old_families = set(), {}
    for row in reused_rows:
        family, partition = row['family_id'], row['partition']
        if assignments.get(family) != partition:
            raise ValueError('Reused label changed its original source-family partition')
        if family in old_families and old_families[family] != partition:
            raise ValueError('Reused family crosses partitions')
        old_families[family] = partition
        identity = feature_hash(chess.Board(row['fen']))
        if row.get('feature_hash') != identity:
            raise ValueError('Reused label piece-feature identity mismatch')
        old_features.add(identity)
    match_features = _match_features(match_openings_path)
    split_features = defaultdict(set)
    for row in pool:
        split_features[row['feature_hash']].add(row['partition'])
    colliding = {key for key, partitions in split_features.items() if len(partitions) > 1}
    exclusions = Counter()
    unique = {}
    for row in sorted(pool, key=lambda entry: _rank(seed, entry['query_id'])):
        identity = row['feature_hash']
        if identity in old_features:
            exclusions['reused_piece_or_mirror'] += 1
        elif identity in colliding:
            exclusions['cross_partition_piece_or_mirror'] += 1
        elif identity in match_features:
            exclusions['match_opening_prefix_piece_or_mirror'] += 1
        elif identity in unique:
            exclusions['within_partition_duplicate_piece_or_mirror'] += 1
        else:
            unique[identity] = row
    selected = []
    for partition in PARTITIONS:
        selected.extend(_select([row for row in unique.values() if row['partition'] == partition],
                                counts[partition], seed))
    manifest = {
        'schema_version': 1, 'seed': seed, 'requested_new_rows': counts,
        'phase_target_percent': PHASE_WEIGHTS,
        'selection': 'Fixed material quotas; deterministic hash order; round-robin original family and mover; no labels or outcomes used',
        'source_policy': 'Existing legal source-game replay only; no new games, random moves, external PGN, or opening prefixes added',
        'split_policy': 'Original master family assignment retained; all new cross-split piece/mirror collisions removed; every reused-label piece/mirror excluded from new rows',
        'test_limit': 'Unused by this fine-tune and checkpoint selection; previously available development source positions, not a newly independent corpus',
        'match_limit': 'Declared exact opening-prefix piece/mirror inputs excluded; opening families and match bank are known from earlier development',
        'source_artifacts': artifacts,
        'match_openings': ({'path': str(Path(match_openings_path).resolve()), 'sha256': sha256(match_openings_path)}
                           if match_openings_path is not None else None),
        'family_assignments': dict(sorted(assignments.items())),
        'exclusions': dict(exclusions),
        'source_nonterminal_rows': len(pool),
        'available_unique_rows': len(unique),
        'selected': distribution(selected),
        'reused': distribution(reused_rows),
        'combined_before_new_teacher_exclusions': distribution(reused_rows + selected),
        'selected_query_sha256': hashlib.sha256(json.dumps(selected, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
    }
    return selected, manifest
