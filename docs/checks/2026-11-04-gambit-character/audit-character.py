"""Replay isolated gambit matches and separate preparation, offers and debt.

This auditor never chooses a move or calls a production selector. The authored
catalog is read as static declarations; legal moves, material, score floors and
receipt contexts are checked independently. A successful integrity audit does
not imply that the experimental character goal was observed or that Elo rose.
"""
import argparse
import ast
from collections import Counter
import hashlib
import io
import json
import math
from pathlib import Path
import sys

import chess
import chess.pgn

ROOT = next(parent for parent in Path(__file__).resolve().parents if (parent / 'scripts/play_human.py').is_file())
VALUES = {chess.PAWN: 100, chess.KNIGHT: 300, chess.BISHOP: 300,
          chess.ROOK: 500, chess.QUEEN: 900, chess.KING: 0}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def inside(directory, name):
    path = (directory / name).resolve()
    if not path.is_relative_to(directory.resolve()):
        raise ValueError('Artifact path escaped its match directory')
    return path


def material(board, own):
    return sum((1 if p.color == own else -1) * VALUES[p.piece_type] for p in board.piece_map().values())


def capture_value(board, move):
    if board.is_en_passant(move):
        return 100
    piece = board.piece_at(move.to_square)
    return VALUES[piece.piece_type] if piece else 0


def capture_fact(board, move, own, ply):
    """Actual capture at any square, including material already invested early."""
    if not board.is_capture(move):
        return None
    square = move.to_square
    if board.is_en_passant(move):
        square += -8 if board.turn == chess.WHITE else 8
    victim = board.piece_at(square)
    after = board.copy(stack=True)
    after.push(move)
    before_balance, after_balance = material(board, own), material(after, own)
    return {'ply': ply, 'uci': move.uci(), 'san': board.san(move),
            'from_square': chess.square_name(move.from_square), 'to_square': chess.square_name(move.to_square),
            'captured_square': chess.square_name(square), 'victim': victim.symbol() if victim else None,
            'victim_cp': capture_value(board, move), 'victim_owned_by_gambit_side': victim.color == own if victim else False,
            'balance_before_own_cp': before_balance, 'balance_after_own_cp': after_balance,
            'balance_delta_own_cp': after_balance - before_balance,
            'net_debt_after_own_cp': max(0, -after_balance)}


def catalog():
    path = ROOT / 'scripts/boss_openings.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    result = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.func.id != '_Seed':
            continue
        args = node.args
        if len(args) < 4 or not isinstance(args[2], ast.Attribute):
            raise ValueError('Unsupported static seed declaration')
        row = {'line_id': ast.literal_eval(args[0]), 'name': ast.literal_eval(args[1]),
               'color': args[2].attr.lower(), 'moves': list(ast.literal_eval(args[3]))}
        if row['color'] not in ('white', 'black'):
            raise ValueError('Unknown seed color')
        if row['line_id'] in result and result[row['line_id']] != row:
            raise ValueError('Conflicting static seed declaration')
        result[row['line_id']] = row
    if not result:
        raise ValueError('No authored seed declarations found')
    return result, {'file': str(path), 'sha256': sha(path), 'lines': len(result),
                    'method': 'static AST declarations only; no selector import or execution'}


def pv_valid(board, line, first=None):
    if not isinstance(line, dict) or type(line.get('depth')) is not int or line['depth'] < 1 or not number(line.get('cp')):
        return False
    pv = line.get('pv_uci')
    if not isinstance(pv, list) or not pv or first is not None and pv[0] != first:
        return False
    replay = board.copy(stack=True)
    for uci in pv:
        try:
            move = chess.Move.from_uci(uci)
        except (TypeError, ValueError):
            return False
        if move not in replay.legal_moves:
            return False
        replay.push(move)
    expected_white = line['cp'] * (1 if board.turn else -1)
    return line.get('cp_white', expected_white) == expected_white


def exchange_gain(board, square, counter):
    """Optional-stop legal same-square exchange minimax, independent oracle."""
    counter[0] += 1
    if counter[0] > 8192:
        raise RuntimeError('Independent local exchange audit exceeded its node cap')
    best = 0
    for move in list(board.legal_moves):
        if move.to_square != square or not board.is_capture(move):
            continue
        gain = capture_value(board, move) + (VALUES[move.promotion] - 100 if move.promotion else 0)
        board.push(move)
        gain -= exchange_gain(board, square, counter)
        board.pop()
        best = max(best, gain)
    return best


def initiative_screen(board, move, screen):
    if not isinstance(screen, dict) or screen.get('admitted') is not True:
        return False
    gain = capture_value(board, move) + (VALUES[move.promotion] - 100 if move.promotion else 0)
    after = board.copy(stack=True)
    after.push(move)
    if after.is_game_over(claim_draw=True):
        return False
    observed = {r.get('reply_uci'): r for r in screen.get('captures', [])}
    required = set()
    for reply in list(after.legal_moves):
        victim = after.piece_at(reply.to_square)
        after.push(reply)
        try:
            if after.is_checkmate():
                return False
            if victim is None or victim.color != board.turn or VALUES[victim.piece_type] < 300:
                continue
            required.add(reply.uci())
            row = observed.get(reply.uci(), {})
            recovery = exchange_gain(after, reply.to_square, [0])
            net = gain - VALUES[victim.piece_type] + recovery - (VALUES[reply.promotion] - 100 if reply.promotion else 0)
            logged = row.get('recapture_exchange') or {}
            if (row.get('victim_cp') != VALUES[victim.piece_type] or row.get('candidate_gain_cp') != gain
                    or row.get('net_material_cp') != net or net < -100
                    or logged.get('complete') is not True or logged.get('gain_cp') != recovery):
                return False
        finally:
            after.pop()
    return required == set(observed)


def audit_game(directory, entry, meta, book):
    path = inside(directory, entry['record_file'])
    game = json.loads(path.read_text(encoding='utf-8'))
    issues, warnings = [], []
    check = lambda condition, label: issues.append(label) if not condition else None
    own = game['boss_color'] == 'white'
    board = chess.Board(game['initial_fen'])
    check(game['initial_fen'] == chess.STARTING_FEN, 'not_standard_start')
    check(game['boss_seed'] == entry['boss_seed'], 'schedule_seed_mismatch')
    check(game['boss_color'] == entry['boss_color'], 'schedule_color_mismatch')
    boards, ucis = [board.copy(stack=True)], []
    for ply, turn in enumerate(game['moves'], 1):
        check(turn['ply'] == ply and turn['before_fen'] == board.fen(en_passant='fen'), f'pre_context:{ply}')
        move = chess.Move.from_uci(turn['uci'])
        if move not in board.legal_moves:
            raise ValueError(f'Illegal recorded move at {path}:{ply}')
        check(turn['san'] == board.san(move), f'san:{ply}')
        check(turn['actor'] == ('boss' if board.turn == own else 'stockfish'), f'actor:{ply}')
        board.push(move)
        check(turn['after_fen'] == board.fen(en_passant='fen'), f'post_context:{ply}')
        ucis.append(move.uci())
        boards.append(board.copy(stack=True))
    check(ucis == game['uci_moves'] and len(ucis) == game['plies'], 'whole_history_mismatch')
    check(board.fen(en_passant='fen') == game['final_fen'], 'final_fen_mismatch')
    trace = [json.loads(line) for line in inside(directory, f"game-{game['game']:03d}.moves.jsonl").read_text(encoding='utf-8').splitlines()]
    check(trace == game['moves'], 'jsonl_mismatch')
    pgn = chess.pgn.read_game(io.StringIO(inside(directory, entry['pgn_file']).read_text(encoding='utf-8')))
    check(pgn is not None and not pgn.errors, 'pgn_parse_error')
    if pgn is not None:
        check([m.uci() for m in pgn.mainline_moves()] == ucis, 'pgn_move_mismatch')
        check(pgn.headers['Result'] == game['result'], 'pgn_result_mismatch')
        check(pgn.end().board().fen(en_passant='fen') == game['final_fen'], 'pgn_final_fen_mismatch')
    outcome = board.outcome(claim_draw=True)
    if game['capped']:
        check(game['result'] == '*' and not game['completed'] and not game['legal_result'], 'cap_scored_as_completed')
        category = 'capped_unfinished'
    elif game.get('error'):
        category = 'failed'
        issues.append('game_error:' + str(game['error']))
    else:
        check(outcome is not None and game['completed'] and game['legal_result'], 'missing_legal_outcome')
        check(outcome is not None and outcome.result() == game['result'], 'legal_result_mismatch')
        category = 'draw' if game['result'] == '1/2-1/2' else ('win' if game['result'] == ('1-0' if own else '0-1') else 'loss')
    decisions = [t['boss_decision'] for t in game['moves'] if t['actor'] == 'boss']
    check(decisions == game.get('boss_play', {}).get('decisions'), 'final_host_decisions_mismatch')
    counts = Counter()
    openings, signatures, initiative_rows, budget_rows = [], [], [], []
    for turn in game['moves']:
        if turn['actor'] != 'boss':
            continue
        ply = turn['ply']; before = boards[ply - 1]; decision = turn.get('boss_decision') or {}
        check(decision.get('policy') == meta['policy'], f'decision_policy:{ply}')
        check(decision.get('selected_uci') == turn['uci'] and decision.get('before_fen') == turn['before_fen']
              and decision.get('history_uci') == ucis[:ply - 1], f'decision_context:{ply}')
        check(decision.get('seed') == game['boss_seed'], f'decision_seed:{ply}')
        check(decision.get('baseline_budget_ms') == meta['config']['boss_ms'], f'baseline_budget:{ply}')
        extra = decision.get('extra_budget_ms')
        check(number(extra) and 0 <= extra <= min(meta['config']['boss_ms'], 1000) + .001, f'extra_budget:{ply}')
        tactical = decision.get('tactical_budget_ms')
        if tactical is not None:
            check(number(tactical) and number(extra) and 0 <= tactical <= extra + .001, f'tactical_budget:{ply}')
        elapsed = decision.get('extra_elapsed_ms')
        if number(extra) and number(elapsed) and elapsed > extra + 120:
            warnings.append(f'extra_wall_overrun:{ply}:{elapsed - extra:.3f}')
        budget_rows.append({'ply': ply, 'baseline_ms': decision.get('baseline_budget_ms'),
                            'extra_ms': extra, 'extra_elapsed_ms': elapsed, 'turn_wall_ms': turn['elapsed_ms']})
        source = book.get(decision.get('proposed_line_id'))
        if source:
            candidate = decision.get('candidate_uci')
            valid = source['color'] == game['boss_color'] and source['moves'][:ply - 1] == ucis[:ply - 1]
            valid &= len(source['moves']) >= ply and source['moves'][ply - 1] == candidate
            check(valid, f'book_proposal:{ply}')
            stage = 'signature' if len(source['moves']) == ply else 'preparation'
            counts[stage + '_proposed'] += 1
            selected = decision.get('line_id') == source['line_id'] and candidate == turn['uci']
            if selected:
                counts[stage + '_selected'] += 1
            openings.append({'ply': ply, 'line_id': source['line_id'], 'name': source['name'], 'stage': stage,
                             'candidate_uci': candidate, 'selected': selected, 'reason': decision.get('reason'),
                             'loss_cp': decision.get('loss_cp'), 'compared_depth': decision.get('compared_depth')})
            check(decision.get('line_complete') is (selected and stage == 'signature'), f'book_completion:{ply}')
            if selected:
                base = decision.get('baseline') or {}
                check(pv_valid(before, base, decision.get('baseline_uci')) and base.get('depth', 0) >= 4, f'book_baseline_evidence:{ply}')
                if candidate != decision.get('baseline_uci'):
                    probe = next((p for p in decision.get('probes', []) if p.get('uci') == candidate and p.get('admitted')), {})
                    common, final = probe.get('candidate_compared') or {}, probe.get('candidate_final') or {}
                    check(pv_valid(before, common, candidate) and pv_valid(before, final, candidate)
                          and common.get('depth') == base.get('depth') == decision.get('compared_depth')
                          and final.get('depth', -1) >= base.get('depth', 0), f'book_common_depth:{ply}')
                if stage == 'signature':
                    moved = chess.Move.from_uci(turn['uci']); after = boards[ply]
                    piece = after.piece_at(moved.to_square)
                    captures = [m.uci() for m in after.legal_moves if m.to_square == moved.to_square and after.is_capture(m)]
                    gross = VALUES[piece.piece_type] if captures and piece else 0
                    accepted, accepted_value = False, 0
                    capture_anywhere = None
                    if ply < len(ucis):
                        reply = chess.Move.from_uci(ucis[ply])
                        accepted = reply.to_square == moved.to_square and after.is_capture(reply)
                        accepted_value = capture_value(after, reply) if accepted else 0
                        capture_anywhere = capture_fact(after, reply, own, ply + 1)
                    previous_capture = (capture_fact(boards[ply - 2], chess.Move.from_uci(ucis[ply - 2]), own, ply - 1)
                                        if ply >= 2 else None)
                    signature_balance = material(after, own)
                    trace_material = [{'ply': k, 'balance_own_cp': material(boards[k], own),
                                       'change_since_signature_cp': material(boards[k], own) - signature_balance}
                                      for k in range(ply, min(len(boards), ply + 5))]
                    response = boards[ply + 2] if ply + 2 < len(boards) else None
                    recovered = 0
                    if response is not None:
                        response_move = chess.Move.from_uci(ucis[ply + 1])
                        if response_move.to_square == moved.to_square and boards[ply + 1].is_capture(response_move):
                            recovered = capture_value(boards[ply + 1], response_move)
                    signatures.append({'ply': ply, 'line_id': source['line_id'], 'uci': moved.uci(),
                        'offered_on_signature_square_gross_cp': gross, 'legal_capture_uci': captures,
                        'immediate_opponent_accepted': accepted, 'accepted_gross_cp': accepted_value,
                        'net_balance_before_signature_cp': material(before, own),
                        'net_balance_at_signature_cp': signature_balance,
                        'net_debt_at_signature_cp': max(0, -signature_balance),
                        'previous_opponent_capture_before_signature': previous_capture,
                        'immediate_opponent_capture_any_square': capture_anywhere,
                        'immediate_opponent_capture_off_signature_square': bool(capture_anywhere and not accepted),
                        'net_debt_after_next_opponent_move_cp': max(0, -material(boards[ply + 1], own)) if ply + 1 < len(boards) else None,
                        'net_balance_after_next_owner_move_cp': material(response, own) if response is not None else None,
                        'net_debt_after_next_owner_move_cp': max(0, -material(response, own)) if response is not None else None,
                        'recaptured_on_signature_square_gross_cp': recovered,
                        'four_ply_balance_trace': trace_material,
                        'basis': 'signature-square capture is narrow; a pawn may be captured elsewhere or already invested before the signature. Gross channels overlap and must not be summed as sacrifice cost. Current debt is not final sacrifice cost.'})
        detail = decision.get('initiative')
        if not isinstance(detail, dict):
            continue
        ctx = detail.get('context') or {}
        stage = ctx.get('stage', 'offered')
        counts['initiative_' + stage + '_turns'] += 1
        source_ply = ctx.get('source_ply', ctx.get('signature_ply'))
        line = book.get(ctx.get('line_id'))
        valid_anchor = line is not None and type(source_ply) is int and 1 <= source_ply < ply
        if valid_anchor:
            anchor_turn = game['moves'][source_ply - 1]; anchor = anchor_turn.get('boss_decision') or {}
            valid_anchor = (anchor_turn['actor'] == 'boss' and anchor.get('line_id') == line['line_id']
                and anchor.get('selected_uci') == anchor.get('candidate_uci') == ucis[source_ply - 1]
                and line['moves'][:source_ply] == ucis[:source_ply]
                and anchor.get('line_complete') is (stage == 'offered'))
        check(valid_anchor, f'initiative_anchor:{ply}')
        if 'entry_ply' in ctx:
            check(ctx['entry_ply'] == source_ply, f'initiative_entry_ply:{ply}')
        if type(source_ply) is int and 1 <= source_ply < ply:
            if 'source_fen' in ctx:
                check(ctx['source_fen'] == boards[source_ply].fen(en_passant='fen'), f'initiative_source_fen:{ply}')
            if 'source_before_fen' in ctx:
                check(ctx['source_before_fen'] == boards[source_ply - 1].fen(en_passant='fen'), f'initiative_source_before_fen:{ply}')
            if 'source_uci' in ctx:
                check(ctx['source_uci'] == ucis[source_ply - 1], f'initiative_source_uci:{ply}')
        check(ctx.get('source_policy') == meta['policy'] and detail.get('policy') == ctx.get('policy'), f'initiative_source_policy:{ply}')
        check(ctx.get('fen') == before.fen(en_passant='fen') and ctx.get('history_uci') == ucis[:ply - 1]
              and ctx.get('turn') == game['boss_color'] and ctx.get('seed') == game['boss_seed'], f'initiative_context:{ply}')
        check(ctx.get('material_debt_cp') == max(0, -material(before, own)), f'initiative_net_debt:{ply}')
        check(type(source_ply) is int and 0 <= ply - 1 - source_ply <= 20, f'initiative_window:{ply}')
        if stage == 'preparation':
            check(ctx.get('signature_ply') is None and ctx.get('signature_uci') is None
                  and not ctx.get('signature_complete') and not ctx.get('accepted')
                  and ctx.get('material_offered_cp') == ctx.get('material_accepted_cp') == 0, f'preparation_not_signature:{ply}')
        elif stage == 'offered':
            signature = next((s for s in signatures if s['ply'] == source_ply and s['line_id'] == ctx.get('line_id')), None)
            check(signature is not None, f'initiative_real_signature:{ply}')
            if signature is not None:
                check(ctx.get('signature_ply') == source_ply and ctx.get('signature_uci') == signature['uci']
                      and ctx.get('signature_fen') == boards[source_ply].fen(en_passant='fen'), f'initiative_signature_context:{ply}')
                check(ctx.get('material_offered_cp') == signature['offered_on_signature_square_gross_cp']
                      and ctx.get('accepted') is signature['immediate_opponent_accepted']
                      and ctx.get('material_accepted_cp') == signature['accepted_gross_cp'], f'initiative_gross_material:{ply}')
                if 'signature_complete' in ctx:
                    check(ctx['signature_complete'] is True, f'initiative_signature_complete:{ply}')
        else:
            issues.append(f'unknown_initiative_stage:{ply}:{stage}')
        applied = detail.get('applied') is True
        counts['initiative_' + stage + '_applied'] += applied
        row = {'ply': ply, 'stage': stage, 'source_ply': source_ply, 'applied': applied,
               'selected_uci': turn['uci'], 'baseline_uci': decision.get('baseline_uci'),
               'reason': detail.get('reason'), 'loss_cp': detail.get('loss_cp'),
               'net_material_debt_cp': ctx.get('material_debt_cp'),
               'accepted_signature_gross_cp': ctx.get('material_accepted_cp')}
        if applied:
            check(not before.is_check() and not before.is_game_over(claim_draw=True)
                  and before.legal_moves.count() > 1, f'initiative_not_forced:{ply}')
            base, evidence = detail.get('baseline') or {}, detail.get('evidence') or {}
            common, final = evidence.get('candidate_compared') or {}, evidence.get('candidate_final') or {}
            valid = (pv_valid(before, base, decision.get('baseline_uci')) and pv_valid(before, common, turn['uci'])
                and pv_valid(before, final, turn['uci']) and base.get('depth', 0) >= 4
                and common.get('depth') == base.get('depth') and final.get('depth', -1) >= base.get('depth', 0))
            check(valid, f'initiative_completed_depth:{ply}')
            if valid:
                loss = max(0, base['cp'] - common['cp'])
                row['recomputed_loss_cp'] = loss
                check(min(common['cp'], final['cp']) >= base['cp'] - 35 and detail.get('loss_cp') == loss, f'initiative_quality_floor:{ply}')
                check(len(common['pv_uci']) >= 2 and len(final['pv_uci']) >= 2, f'initiative_reply_evidence:{ply}')
                if final['depth'] == common['depth']:
                    check(final['cp'] == common['cp'], f'initiative_same_depth_score_conflict:{ply}')
            check(detail.get('selected_uci') == turn['uci'] != decision.get('baseline_uci'), f'initiative_applied_selection:{ply}')
            check(initiative_screen(before, chess.Move.from_uci(turn['uci']), evidence.get('material_screen')), f'initiative_material_screen:{ply}')
            style, baseline_style = evidence.get('style') or {}, detail.get('baseline_evidence') or {}
            check(number(style.get('points')) and number(baseline_style.get('points')) and style['points'] > baseline_style['points'], f'initiative_style_gain:{ply}')
            for label, value in [('candidate', style), ('baseline', baseline_style)]:
                components = value.get('gambit_components') or {}
                check(bool(components) and all(number(x) for x in components.values()) and sum(components.values()) == value.get('points'), f'initiative_style_arithmetic:{ply}:{label}')
        else:
            check(turn['uci'] == decision.get('baseline_uci') == detail.get('selected_uci'), f'initiative_fallback:{ply}')
        initiative_rows.append(row)
    counts['accepted_signature_captures'] = sum(s['immediate_opponent_accepted'] for s in signatures)
    counts['accepted_gross_cp'] = sum(s['accepted_gross_cp'] for s in signatures)
    counts['opponent_captures_after_signature_any_square'] = sum(s['immediate_opponent_capture_any_square'] is not None for s in signatures)
    counts['opponent_captures_after_signature_off_square'] = sum(s['immediate_opponent_capture_off_signature_square'] for s in signatures)
    counts['signatures_already_in_net_material_debt'] = sum(s['net_debt_at_signature_cp'] > 0 for s in signatures)
    counts['signatures_with_net_debt_after_owner_reply'] = sum((s['net_debt_after_next_owner_move_cp'] or 0) > 0 for s in signatures)
    return {'game_file': str(path), 'game_sha256': sha(path), 'color': game['boss_color'],
            'seed': game['boss_seed'], 'result': game['result'], 'category': category, 'plies': game['plies'],
            'integrity_passed': not issues, 'issues': issues, 'warnings': warnings, 'counts': dict(counts),
            'opening_events': openings, 'signature_events': signatures, 'initiative_events': initiative_rows,
            'budget_observations': budget_rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--matches', nargs='+', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--allow-partial', action='store_true')
    parser.add_argument('--require-live-source-match', action='store_true')
    args = parser.parse_args()
    if args.output.exists():
        parser.error('output already exists; preserve prior audit evidence')
    book, book_meta = catalog()
    report = {'schema': 'regicide.gambit-character-audit.v1', 'auditor_sha256': sha(__file__),
              'catalog': book_meta, 'preliminary': args.allow_partial, 'matches': [], 'games': [],
              'scope': 'legal/artifact/context/score-floor audit, not an Elo estimate or proof of attack soundness'}
    for directory in args.matches:
        directory = directory.resolve(strict=True)
        path = directory / 'match.json'; meta = json.loads(path.read_text(encoding='utf-8'))
        if not args.allow_partial and meta.get('status') != 'completed':
            raise ValueError('Match is not complete: ' + str(path))
        live = []
        for name, digest in meta['source_sha256'].items():
            local = inside(ROOT, name)
            if not local.is_file() or sha(local) != digest:
                live.append(name)
        binaries = {key: sha(meta['config']['boss_binary' if key == 'boss' else 'stockfish'])
                    for key in ('boss', 'stockfish')}
        scheduled = meta['schedule']
        if not args.allow_partial and len(meta['games']) != len(scheduled):
            raise ValueError('Scheduled game count mismatch: ' + str(path))
        entry_rows = []
        for entry in scheduled:
            artifact = directory / f"game-{entry['game']:03d}.json"
            if args.allow_partial and not artifact.is_file():
                continue
            recorded = next((row for row in meta['games'] if row['game'] == entry['game']), None)
            if recorded is None:
                raise ValueError('Game absent from completed match records')
            entry_rows.append(audit_game(directory, {**entry, **recorded}, meta, book))
        observed_colors = Counter(g['color'] for g in entry_rows)
        source_ok = meta.get('sources_unchanged') is True if not args.allow_partial else meta.get('sources_unchanged') is not False
        binary_ok = binaries == meta['binary_sha256'] and meta.get('binaries_unchanged', True) is True
        complete_pair = observed_colors['white'] == observed_colors['black'] == len(entry_rows) / 2
        seed_colors = {}
        for g in entry_rows:
            seed_colors.setdefault(g['seed'], []).append(g['color'])
        pairing_ok = all(sorted(colors) == ['black', 'white'] for colors in seed_colors.values()) if not args.allow_partial else True
        item = {'file': str(path), 'sha256': sha(path), 'policy': meta['policy'],
                'source_hashes_recorded_unchanged': source_ok, 'current_source_mismatches': live,
                'binary_hashes_match': binary_ok, 'color_balance': dict(observed_colors), 'pairing_ok': pairing_ok,
                'complete_pair': complete_pair, 'historical_source_scope': 'recorded unchanged during run; later source edits are listed separately'}
        item['integrity_passed'] = (source_ok and binary_ok and pairing_ok
            and (complete_pair or args.allow_partial) and (not live or not args.require_live_source_match))
        report['matches'].append(item); report['games'].extend(entry_rows)
    totals = Counter()
    for game in report['games']:
        totals.update(game['counts'])
    report['totals'] = dict(totals)
    report['outcomes'] = dict(Counter(g['category'] for g in report['games']))
    report['integrity_passed'] = all(m['integrity_passed'] for m in report['matches']) and all(g['integrity_passed'] for g in report['games'])
    report['character_observation'] = {'authored_signatures': totals['signature_selected'],
        'signature_followup_initiative_turns': totals['initiative_offered_turns'],
        'preparation_followup_initiative_turns': totals['initiative_preparation_turns'],
        'signature_alternative_applications': totals['initiative_offered_applied'],
        'preparation_alternative_applications': totals['initiative_preparation_applied'],
        'signature_square_capture_count': totals['accepted_signature_captures'],
        'next_opponent_capture_any_square_count': totals['opponent_captures_after_signature_any_square'],
        'next_opponent_capture_off_signature_square_count': totals['opponent_captures_after_signature_off_square'],
        'signatures_already_in_net_material_debt': totals['signatures_already_in_net_material_debt'],
        'signatures_with_net_material_debt_after_owner_reply': totals['signatures_with_net_debt_after_owner_reply'],
        'signature_goal_observed': totals['signature_selected'] > 0 and totals['initiative_offered_turns'] > 0,
        'scope': 'preparation activity never counts as an actual signature. Signature-square capture is not total gambit acceptance: offered pawns can be captured elsewhere or before the signature. Gross channels overlap; use actual net debt in parallel. Integrity PASS is independent of the character goal.'}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False); stream.write('\n')
    print(json.dumps({'integrity_passed': report['integrity_passed'], 'games': len(report['games']),
                      'outcomes': report['outcomes'], 'totals': report['totals'],
                      'character_observation': report['character_observation'],
                      'issues': [{'game': g['game_file'], 'issues': g['issues']} for g in report['games'] if g['issues']]}))
    return 0 if report['integrity_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
