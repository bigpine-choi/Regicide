"""Independently audit completed dynamic-play records without running engines.

This checks the frozen 32 paired choices (64 requests), own-engine admission
evidence, deterministic selection, and set-based style geometry. Style points
are not centipawns or playing-strength measurements.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path

import chess


ROOT = Path(__file__).resolve().parents[1]
POLICIES = {'legacy': 'regicide-opening-variety-v3', 'dynamic': 'regicide-dynamic-play-v1'}


def require(value, message):
    if not value:
        raise ValueError(message)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def integer(value):
    return type(value) is int


def replay_line(board, line, expected, stats, context):
    if line is None:
        return None
    require(isinstance(line, dict), context + ': score line must be an object')
    require(integer(line.get('depth')) and line['depth'] >= 1, context + ': invalid depth')
    require(integer(line.get('cp')) and integer(line.get('cp_white')), context + ': noninteger CP')
    require(line.get('pov') == ('white' if board.turn else 'black'), context + ': root POV mismatch')
    require(line['cp_white'] == (line['cp'] if board.turn else -line['cp']), context + ': white/root sign mismatch')
    pv = line.get('pv_uci')
    require(isinstance(pv, list) and 1 <= len(pv) <= 128, context + ': missing/bounded PV')
    require(pv[0] == expected == line.get('uci'), context + ': first move mismatch')
    replay = board.copy(stack=True)
    for uci in pv:
        move = chess.Move.from_uci(uci)
        require(move in replay.legal_moves, context + ': illegal PV move ' + uci)
        replay.push(move)
    stats['legal_score_pv_lines'] += 1
    return replay


def set_features(board, color):
    """Separate set-based implementation of the recorded v1 geometry."""
    own = {square for square, piece in board.piece_map().items() if piece.color == color}
    pawn_targets = set()
    for square in board.pieces(chess.PAWN, not color):
        pawn_targets.update(board.attacks(square))
    king = board.king(not color)
    king_zone = set(chess.SquareSet(chess.BB_KING_ATTACKS[king])) | {king} if king is not None else set()
    mobility, developed, files, attackers = 0, 0, 0, 0
    pressure_targets = set()
    for kind, cap, factor in ((chess.KNIGHT, 8, 2), (chess.BISHOP, 12, 2),
                              (chess.ROOK, 14, 1), (chess.QUEEN, 20, 1)):
        for square in board.pieces(kind, color):
            targets = set(board.attacks(square)) - own - pawn_targets
            mobility += min(cap, len(targets)) * factor
            rank = chess.square_rank(square) if color else 7 - chess.square_rank(square)
            developed += int(kind in (chess.KNIGHT, chess.BISHOP) and rank >= 2 and square not in pawn_targets)
            if kind == chess.ROOK:
                files += int(not any(chess.square_file(pawn) == chess.square_file(square)
                                     for pawn in board.pieces(chess.PAWN, color)))
            if targets & king_zone:
                attackers += 1
                pressure_targets.update(targets & king_zone)
    pressure = ((min(attackers, 4) - 1) * 3 + min(len(pressure_targets), 6)) if attackers >= 2 else 0
    return {'mobility': mobility, 'developed_minors': developed, 'rook_files': files,
            'king_attackers': attackers, 'coordinated_pressure': pressure}


def audit_style(board, line, evidence, stats, context):
    require(len(line['pv_uci']) >= 2, context + ': no actual opponent reply')
    first_move = chess.Move.from_uci(line['pv_uci'][0])
    first = board.copy(stack=True)
    first.push(first_move)
    after = first.copy(stack=True)
    after.push_uci(line['pv_uci'][1])
    require(not first.is_game_over(claim_draw=True) and not after.is_game_over(claim_draw=True),
            context + ': terminal continuation cannot establish style')
    require(evidence['pv_prefix_uci'] == line['pv_uci'][:2], context + ': style/PV mismatch')
    require(evidence['reply_fen'] == after.fen(en_passant='fen'), context + ': style reply board mismatch')
    before_features, after_features = set_features(board, board.turn), set_features(after, board.turn)
    require(evidence['before'] == before_features and evidence['after_opponent_reply'] == after_features,
            context + ': independent style geometry mismatch')
    opened = 0
    if (board.piece_at(first_move.from_square) == chess.Piece(chess.PAWN, board.turn)
            and chess.square_file(first_move.from_square) in (2, 3, 4, 5)):
        for kind in (chess.BISHOP, chess.ROOK, chess.QUEEN):
            for square in board.pieces(kind, board.turn):
                piece = chess.Piece(kind, board.turn)
                if first.piece_at(square) == piece and after.piece_at(square) == piece:
                    retained = (set(first.attacks(square)) - set(board.attacks(square))) & set(after.attacks(square))
                    opened += int(len(retained) >= 2)
    opened = min(opened, 3)
    terms = {'activity': after_features['mobility'] - before_features['mobility'],
             'development': 3 * (after_features['developed_minors'] - before_features['developed_minors']),
             'rook_files': 3 * (after_features['rook_files'] - before_features['rook_files']),
             'king_pressure': 4 * (after_features['coordinated_pressure'] - before_features['coordinated_pressure']),
             'central_break': 6 * opened}
    require(evidence['retained_open_lines'] == opened and evidence['components'] == terms,
            context + ': independent style components mismatch')
    require(integer(evidence['points']) and evidence['points'] == sum(terms.values()), context + ': style sum mismatch')
    require(evidence['units'] == 'style points; not centipawns', context + ': style unit mismatch')
    stats['independent_style_geometry_lines'] += 1
    return evidence['points']


def selected_from_weights(board, seed, policy, weights):
    history = [board.root().fen(en_passant='fen'), [move.uci() for move in board.move_stack], board.fen(en_passant='fen')]
    payload = [policy, seed, history, 'selection', sorted(weights.items())]
    value = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode('utf-8')
    draw = int(hashlib.sha256(value).hexdigest(), 16) % sum(weights.values())
    for uci, weight in sorted(weights.items()):
        if draw < weight:
            return uci
        draw -= weight
    raise ValueError('Invalid weighted selection')


def audit_decision(board, seed, label, request, budget, stats, context):
    selected = request['selected_uci']
    actual = chess.Move.from_uci(selected)
    require(actual in board.legal_moves, context + ': selected illegal move')
    require(request['selected_san'] == board.san(actual), context + ': SAN mismatch')
    require(request['engine'].get('evaluation') == 'classical_activity_v1', context + ': evaluator mismatch')
    require(request['engine']['movetime_ms'] == budget, context + ': AI setting changed')
    require(bool(request['engine'].get('dynamic_play_enabled', False)) == (label == 'dynamic'), context + ': policy mode mismatch')
    decision = request.get('decision')
    if decision is None:
        require(label == 'legacy' and len(board.move_stack) >= 16, context + ': unexpected missing decision')
        stats['legacy_nonopening_requests_without_policy_receipt'] += 1
        return
    stats['policy_receipts'] += 1
    require(decision['policy'] == POLICIES[label], context + ': policy version mismatch')
    require(decision['selected_uci'] == selected and not decision['reused'], context + ': stale/incorrect decision')
    require(decision['ply'] == len(board.move_stack) + 1 and decision['before_fen'] == board.fen(en_passant='fen'),
            context + ': decision root mismatch')
    require(decision['history_uci'] == [move.uci() for move in board.move_stack], context + ': decision history mismatch')
    require(decision['color'] == ('white' if board.turn else 'black'), context + ': color mismatch')
    require(decision['baseline_budget_ms'] == budget, context + ': baseline budget was reduced')
    base_uci, baseline = decision['baseline_uci'], decision['baseline']
    require(chess.Move.from_uci(base_uci) in board.legal_moves, context + ': illegal baseline')
    replay_line(board, baseline, base_uci, stats, context + '/baseline')
    tolerance = 25 if len(board.move_stack) < 16 else 15
    selection_policy = decision['policy']
    if label == 'dynamic':
        require(decision['tolerance_cp'] == tolerance, context + ': incorrect tolerance')
        expected_mode = 'opening' if len(board.move_stack) < 16 else 'middlegame'
        require(decision['mode'] == expected_mode, context + ': incorrect policy phase')
        if expected_mode == 'opening':
            original = decision['opening_receipt']
            require(isinstance(original, dict) and original.get('policy') == POLICIES['legacy'],
                    context + ': missing original opening receipt')
            require(decision['selection_rule'] == 'opening_variety_preserved',
                    context + ': opening variety rule was replaced')
            # The dynamic wrapper changes labels only. All original selection,
            # admission, budget and search evidence must be preserved verbatim.
            for field, value in original.items():
                if field not in ('policy', 'selection_rule'):
                    require(decision.get(field) == value, context + ': opening receipt changed ' + field)
            require(original.get('selected_uci') == selected
                    and original.get('selection_pool') == decision['selection_pool']
                    and original.get('selection_weights') == decision['selection_weights'],
                    context + ': opening selection diverged from original receipt')
            require(decision['style_evidence'] == {} and decision['style_exclusions'] == {},
                    context + ': style filtering must be inactive during opening')
            selection_policy = original['policy']
            stats['preserved_opening_receipts'] += 1
        else:
            require(decision['opening_receipt'] is None, context + ': opening receipt during middlegame')
            require(decision['selection_rule'] == 'quality_gated_reply_supported_style',
                    context + ': incorrect middlegame rule')
    probes, qualified = {}, set()
    for probe in decision['probes']:
        uci = probe['uci']
        require(uci not in probes and uci != base_uci, context + ': duplicate/baseline probe')
        require(uci in decision['proposals'] and chess.Move.from_uci(uci) in board.legal_moves, context + ': invalid proposal')
        probes[uci] = probe
        compared, final = probe.get('candidate_compared'), probe.get('candidate_final')
        replay_line(board, compared, uci, stats, context + '/compared/' + uci)
        replay_line(board, final, uci, stats, context + '/final/' + uci)
        if compared is not None:
            require(baseline is not None, context + ': compared without baseline')
            calculated_loss = max(0, baseline['cp'] - compared['cp'])
            # A rejected search with a shallower final packet can expose a
            # common-depth packet but deliberately leave its loss unclaimed.
            if probe['loss_cp'] is not None or probe['admitted']:
                require(probe['loss_cp'] == calculated_loss, context + ': stored loss differs from original CP')
            stats['losses_recomputed_from_cp'] += 1
        if probe['admitted']:
            require(baseline is not None and compared is not None and final is not None, context + ': incomplete admitted evidence')
            require(probe['reason'] == 'within_tolerance' and probe['error'] is None, context + ': erroneous probe admitted')
            require(baseline['depth'] >= 4 and compared['depth'] == baseline['depth'] == probe['compared_depth'],
                    context + ': unequal admission depth')
            require(abs(compared['cp'] - baseline['cp']) <= tolerance, context + ': admission outside original CP bounds')
            require(final['depth'] >= baseline['depth'] and final['cp'] >= baseline['cp'] - tolerance,
                    context + ': deeper result does not support admission')
            require(final['depth'] != baseline['depth'] or final['cp'] == compared['cp'], context + ': conflicting final score')
            raw = probe['candidate_final_raw']
            require(raw['mate'] is None and not raw['lowerbound'] and not raw['upperbound'], context + ': nonexact admitted score')
            require(raw['depth'] == final['depth'] and raw['cp'] == final['cp'], context + ': raw/final score mismatch')
            qualified.add(uci)
            stats['admitted_probes'] += 1
    pool, weights = decision['selection_pool'], decision['selection_weights']
    require(isinstance(pool, list) and len(pool) == len(set(pool)), context + ': duplicated pool')
    require(base_uci in pool and set(pool) <= qualified | {base_uci}, context + ': pool bypasses admission')
    require(selected in pool and set(weights) == set(pool), context + ': selected/weights outside pool')
    require(all(integer(weight) and weight > 0 for weight in weights.values()), context + ': invalid weight')
    require(selected_from_weights(board, seed, selection_policy, weights) == selected, context + ': seeded selection mismatch')
    if selected != base_uci:
        chosen = probes[selected]
        require(decision['candidate_compared'] == chosen['candidate_compared'] and decision['candidate_final'] == chosen['candidate_final'],
                context + ': selected evidence mismatch')
        require(decision['candidate_final_raw'] == chosen['candidate_final_raw'], context + ': selected raw evidence mismatch')
        require(decision['loss_cp'] == chosen['loss_cp'], context + ': selected loss mismatch')
        stats['selected_alternatives'] += 1
    else:
        require(decision['loss_cp'] == 0, context + ': baseline has a fabricated loss')
    if any(probe['reason'] in ('candidate_failed', 'candidate_score_conflict') for probe in probes.values()):
        require(selected == base_uci and pool == [base_uci], context + ': failure did not retain baseline')
    if label == 'dynamic' and decision['mode'] == 'middlegame':
        points = {}
        for uci, evidence in decision['style_evidence'].items():
            line = baseline if uci == base_uci else probes[uci]['candidate_final']
            points[uci] = audit_style(board, line, evidence, stats, context + '/style/' + uci)
        for uci in set(pool) - {base_uci}:
            require(base_uci in points and uci in points and points[uci] - points[base_uci] >= 0,
                    context + ': admitted selection has negative style gain')
            stats['nonnegative_style_pool_alternatives'] += 1
    cap = min(budget, 2000) if len(board.move_stack) < 16 else min(budget // 2, 750)
    require(0 <= decision['extra_budget_ms'] <= cap, context + ': excessive extra budget')
    # These are per-request upper limits, not reserved slices: a probe that
    # reaches the target depth early returns unused time to the phase deadline.
    # Adding its full limit to later limits would therefore double-count time.
    elapsed = 0
    extra = decision['extra_budget_ms']
    for probe in probes.values():
        limit, duration = probe['budget_ms'], probe['elapsed_ms']
        require(math.isfinite(limit) and math.isfinite(duration) and limit > 0 and duration >= 0,
                context + ': invalid probe timing')
        require(limit <= min(extra / 3, extra - elapsed) + .01,
                context + ': probe limit exceeds remaining phase budget')
        elapsed += duration
    phase_elapsed = decision['extra_elapsed_ms']
    require(math.isfinite(phase_elapsed) and phase_elapsed >= max(0, elapsed - .01),
            context + ': phase time excludes completed probes')
    # The recorded implementation allows 100 ms for timeout/close handling.
    require(phase_elapsed <= extra + 100.01, context + ': phase exceeds watchdog grace')
    if phase_elapsed > extra:
        stats['phase_deadline_grace_used'] += 1
    stats['phase_timing_checked'] += 1


def audit(records_path, fixture_path):
    bank = json.loads(fixture_path.read_text(encoding='utf-8'))
    manifest_path = records_path.parent / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    require(bank['schema'] == 'regicide.dynamic-play.development.v1', 'Unexpected frozen bank schema')
    require(digest(fixture_path) == manifest['fixture_sha256'], 'Frozen input hash mismatch')
    seeds, budget = bank['conditions']['seeds'], bank['conditions']['budget_ms']
    require(len(seeds) == len(set(seeds)), 'Duplicate seeds')
    boards, fixtures = {}, {}
    for row in bank['positions']:
        require(row['id'] not in boards, 'Duplicate position ID')
        board = chess.Board(row['initial_fen'])
        require(board.is_valid(), 'Invalid initial fixture board')
        for uci in row['history_uci']:
            move = chess.Move.from_uci(uci)
            require(move in board.legal_moves, 'Illegal fixture history')
            board.push(move)
        require(board.fen(en_passant='fen') == row['fen'] and not board.is_game_over(claim_draw=True), 'Invalid final fixture board')
        require(row['turn'] == ('white' if board.turn else 'black')
                and row['legal_count'] == board.legal_moves.count(), 'Fixture side/legal count mismatch')
        boards[row['id']], fixtures[row['id']] = board, row
    expected = {(name, seed) for name in boards for seed in seeds}
    require(len(expected) == 32, 'This audit expects the frozen 32 paired requests')
    records = [json.loads(line) for line in records_path.read_text(encoding='utf-8').splitlines() if line.strip()]
    require(len(records) == 32, 'Incomplete or excess pair records')
    seen, stats = set(), Counter()
    for row in records:
        key = row['id'], row['seed']
        require(key in expected and key not in seen, 'Unexpected or duplicate pair')
        seen.add(key)
        board, fixture = boards[row['id']], fixtures[row['id']]
        require(row['fen'] == fixture['fen'] and row['history_uci'] == fixture['history_uci'], 'Record does not match frozen fixture')
        for label in ('legacy', 'dynamic'):
            require(row[label]['engine']['binary_sha256'] == manifest['binary_sha256'], 'Observed binary identity mismatch')
            audit_decision(board, row['seed'], label, row[label], budget, stats, f'{row["id"]}/{row["seed"]}/{label}')
            stats['requests'] += 1
    require(seen == expected and stats['requests'] == 64, 'Frozen request matrix is incomplete')
    return {'status': 'passed', 'pairs': len(records), 'positions': len(boards), 'seeds': len(seeds),
            'counts': dict(stats), 'records_sha256': digest(records_path), 'fixture_sha256': digest(fixture_path),
            'measurement_manifest_sha256': digest(manifest_path), 'audit_script_sha256': digest(__file__),
            'scope': 'Record consistency, independent style geometry and own-engine policy gates; no engine executions or playing-strength claim'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--records', type=Path, default=ROOT / 'build/dynamic-play/comparison-002/records.jsonl')
    parser.add_argument('--positions', type=Path, default=ROOT / 'tests/data/dynamic_play_positions_v1.json')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Use a fresh audit output file')
    try:
        result = audit(args.records, args.positions)
    except (ValueError, KeyError, TypeError, OSError) as error:
        result = {'status': 'failed', 'error': str(error), 'audit_script_sha256': digest(__file__)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result))
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
