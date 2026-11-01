"""An artistic preference among moves independently validated by Regicide.

Style points order/select middlegame candidates; they never change evaluation
centipawns. Opening v3 selection is preserved without an artist preference.
The full-budget engine result remains the fallback. No other engine is queried.
"""

import math
import re

import chess
import chess.engine

import opening_variety as opening


POLICY = "regicide-dynamic-play-v1"
MAX_PLIES = 80
OPENING_PLIES = opening.MAX_PLIES
MIN_TOTAL_MS = 500
MIN_DEPTH = 4
MIN_PROBE_MS = 20
MAX_PROBES = 4
TOLERANCE_CP = 15
OPENING_TOLERANCE_CP = opening.TOLERANCE_CP
CONVERSION_CP = 300
MAX_EXTRA_MS = 750


def memo_key(board, budget_ms, binary_sha256, evaluation_id, *, seed="", catalog_id=None):
    return opening._digest([POLICY, opening.POLICY, seed, catalog_id, opening.history_key(board),
                            budget_ms, binary_sha256, evaluation_id])


def extra_budget(budget_ms, *, ply=0):
    if ply < OPENING_PLIES:
        return opening.extra_budget(budget_ms)
    return min(budget_ms // 2, MAX_EXTRA_MS) if budget_ms >= MIN_TOTAL_MS and ply < MAX_PLIES else 0


def _phase(board):
    return sum(len(board.pieces(kind, color)) * weight
               for color in chess.COLORS for kind, weight in
               ((chess.KNIGHT, 1), (chess.BISHOP, 1), (chess.ROOK, 2), (chess.QUEEN, 4)))


def _nonpawns(board):
    return sum(len(board.pieces(kind, color)) for color in chess.COLORS
               for kind in (chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN))


def _features(board, color):
    """Pseudo activity, not legal/safe mobility or a strength estimate."""
    pawn_attacks = 0
    for square in board.pieces(chess.PAWN, not color):
        pawn_attacks |= board.attacks_mask(square)
    excluded = board.occupied_co[color] | pawn_attacks
    king = board.king(not color)
    ring = (chess.BB_KING_ATTACKS[king] | chess.BB_SQUARES[king]) if king is not None else 0
    mobility = developed = rook_files = attackers = 0
    pressure_squares = 0
    for kind, cap, weight in ((chess.KNIGHT, 8, 2), (chess.BISHOP, 12, 2),
                              (chess.ROOK, 14, 1), (chess.QUEEN, 20, 1)):
        for square in board.pieces(kind, color):
            attacks = board.attacks_mask(square) & ~excluded
            mobility += min(cap, attacks.bit_count()) * weight
            rank = chess.square_rank(square) if color else 7 - chess.square_rank(square)
            if kind in (chess.KNIGHT, chess.BISHOP) and rank >= 2 and not pawn_attacks & chess.BB_SQUARES[square]:
                developed += 1
            if kind == chess.ROOK and not board.pieces_mask(chess.PAWN, color) & chess.BB_FILES[chess.square_file(square)]:
                rook_files += 1
            # One roaming queen does not constitute coordinated king pressure.
            if attacks & ring:
                attackers += 1
                pressure_squares |= attacks & ring
    pressure = ((min(attackers, 4) - 1) * 3 + min(pressure_squares.bit_count(), 6)) if attackers >= 2 else 0
    return {"mobility": mobility, "developed_minors": developed, "rook_files": rook_files,
            "king_attackers": attackers, "coordinated_pressure": pressure}


def _opened_lines(before, after_move, after_reply, move, color):
    piece = before.piece_at(move.from_square)
    if piece != chess.Piece(chess.PAWN, color) or chess.square_file(move.from_square) not in (2, 3, 4, 5):
        return 0
    opened = 0
    for kind in (chess.BISHOP, chess.ROOK, chess.QUEEN):
        for square in before.pieces(kind, color):
            expected = chess.Piece(kind, color)
            if after_move.piece_at(square) != expected or after_reply.piece_at(square) != expected:
                continue
            newly_reached = after_move.attacks_mask(square) & ~before.attacks_mask(square)
            retained = newly_reached & after_reply.attacks_mask(square)
            if retained.bit_count() >= 2:
                opened += 1
    return min(opened, 3)


def _style(board, pv):
    """Require an actual opponent reply, not an invented two-ply plan."""
    if not isinstance(pv, (tuple, list)) or len(pv) < 2:
        return None
    replay = board.copy(stack=True)
    first = None
    for index, item in enumerate(pv[:2]):
        try:
            move = chess.Move.from_uci(item) if isinstance(item, str) else item
        except (TypeError, ValueError):
            return None
        if not isinstance(move, chess.Move) or move not in replay.legal_moves:
            return None
        replay.push(move)
        if replay.is_game_over(claim_draw=True):
            return None
        if index == 0:
            first = replay.copy(stack=True)
            first_move = move
    color = board.turn
    before, after = _features(board, color), _features(replay, color)
    lines = _opened_lines(board, first, replay, first_move, color)
    components = {"activity": after["mobility"] - before["mobility"],
                  "development": 3 * (after["developed_minors"] - before["developed_minors"]),
                  "rook_files": 3 * (after["rook_files"] - before["rook_files"]),
                  "king_pressure": 4 * (after["coordinated_pressure"] - before["coordinated_pressure"]),
                  "central_break": 6 * lines}
    return {"points": sum(components.values()), "components": components,
            "before": before, "after_opponent_reply": after,
            "retained_open_lines": lines, "reply_fen": replay.fen(en_passant="fen"),
            "pv_prefix_uci": [first_move.uci(), move.uci()], "units": "style points; not centipawns"}


def _proposal_points(board, uci):
    """Only order probes here; a real reply is required for final preference."""
    move = chess.Move.from_uci(uci)
    after = board.copy(stack=True)
    after.push(move)
    a, b = _features(board, board.turn), _features(after, board.turn)
    return (b["mobility"] - a["mobility"] + 3 * (b["developed_minors"] - a["developed_minors"])
            + 3 * (b["rook_files"] - a["rook_files"])
            + 4 * (b["coordinated_pressure"] - a["coordinated_pressure"]))


def plan(board, budget_ms, seed, *, forced=False, opening_plan=None):
    ply = len(board.move_stack)
    if ply < OPENING_PLIES:
        original = opening_plan if opening_plan is not None else opening.plan(board, budget_ms, seed, forced=forced)
        result = dict(original)
        result.update(mode="opening", tolerance_cp=OPENING_TOLERANCE_CP,
                      style_policy=POLICY, opening_plan=dict(original))
        return result
    result = {"eligible": False, "reason": "outside_dynamic_phase", "mode": "middlegame",
              "candidate_uci": None, "candidates": [], "candidate_order": [], "catalog_commit": None,
              "early_exploration": False, "candidate_source": "dynamic_legal", "proposal_limit": MAX_PROBES,
              "baseline_budget_ms": budget_ms, "candidate_budget_ms": 0, "extra_budget_ms": 0,
              "tolerance_cp": TOLERANCE_CP, "style_policy": POLICY}
    if ply >= MAX_PLIES:
        return result
    if board.chess960 or not board.is_valid() or board.root().fen(en_passant="fen") != chess.STARTING_FEN:
        result["reason"] = "custom_root"
    elif forced:
        result["reason"] = "user_forced_game"
    elif board.is_game_over(claim_draw=True):
        result["reason"] = "terminal"
    elif board.is_check():
        result["reason"] = "in_check"
    elif opening.has_repetition(board):
        result["reason"] = "repetition"
    elif budget_ms < MIN_TOTAL_MS:
        result["reason"] = "short_budget"
    elif _phase(board) < 8 or _nonpawns(board) < 5:
        result["reason"] = "simplified_position"
    else:
        moves = sorted(move.uci() for move in board.legal_moves)
        if len(moves) <= 1:
            result["reason"] = "forced_reply"
            return result
        scores = {uci: _proposal_points(board, uci) for uci in moves}
        def order_key(uci):
            digest = opening._digest([POLICY, seed, opening.history_key(board), "proposal", uci])
            unit = (int(digest[:13], 16) + 1) / (16 ** 13 + 1)
            weight = 1 + min(12, max(0, scores[uci]))
            return -math.log(unit) / weight, uci
        order = sorted(moves, key=order_key)
        extra = extra_budget(budget_ms, ply=ply)
        result.update(eligible=True, reason="compare", candidates=moves, candidate_order=order,
                      candidate_uci=order[0], candidate_budget_ms=extra, extra_budget_ms=extra,
                      proposal_style_points=scores)
    return result


def proposals(search_plan, baseline_move):
    return opening.proposals(search_plan, baseline_move)


def score_line(board, info, required_move=None):
    if len(board.move_stack) < OPENING_PLIES:
        return opening.score_line(board, info, required_move)
    score = info.get("score") if isinstance(info, dict) else None
    if isinstance(score, chess.engine.PovScore) and score.turn != board.turn:
        return None
    return opening.score_line(board, info, required_move)


def assess(board, baseline_move, baseline_info, proposed_uci, candidate_packets=None,
           candidate_final=None, candidate_move=None, candidate_error=None, *, budget_ms=0, elapsed_ms=0):
    if len(board.move_stack) < OPENING_PLIES:
        result = opening.assess(board, baseline_move, baseline_info, proposed_uci,
                                candidate_packets, candidate_final, candidate_move, candidate_error,
                                budget_ms=budget_ms, elapsed_ms=elapsed_ms)
        result["tolerance_cp"] = OPENING_TOLERANCE_CP
        return result
    tolerance = TOLERANCE_CP
    baseline = score_line(board, baseline_info, baseline_move)
    final = score_line(board, candidate_final, candidate_move) if candidate_move else None
    raw = opening.packet_summary(board, candidate_final)
    compared = loss = None
    reason = "candidate_unreliable"
    if baseline is None or baseline["depth"] < MIN_DEPTH:
        reason = "baseline_unreliable"
    elif candidate_error:
        reason = "candidate_failed"
    elif raw and raw["mate"] is not None:
        reason = "candidate_score_conflict"
    elif candidate_move is not None and candidate_move.uci() == proposed_uci and final is not None:
        compared = score_line(board, (candidate_packets or {}).get(baseline["depth"]), candidate_move)
        if compared is None or final["depth"] < baseline["depth"]:
            reason = "no_common_depth"
        else:
            loss = max(0, baseline["cp"] - compared["cp"])
            if compared["cp"] > baseline["cp"] + tolerance or (final["depth"] == baseline["depth"] and final["cp"] != compared["cp"]):
                reason = "candidate_score_conflict"
            elif loss > tolerance:
                reason = "outside_tolerance"
            elif final["cp"] < baseline["cp"] - tolerance:
                reason = "deeper_candidate_warning"
            else:
                reason = "within_tolerance"
    return {"uci": proposed_uci, "admitted": reason == "within_tolerance", "reason": reason,
            "loss_cp": loss, "compared_depth": baseline["depth"] if baseline else None,
            "candidate_compared": compared, "candidate_final": final, "candidate_final_raw": raw,
            "budget_ms": budget_ms, "elapsed_ms": round(elapsed_ms, 3), "error": candidate_error,
            "tolerance_cp": tolerance}


def _pv_has_terminal(board, pv):
    replay = board.copy(stack=True)
    for uci in pv:
        replay.push_uci(uci)
        if replay.is_game_over(claim_draw=True):
            return True
    return False


def _select(board, seed, weights):
    weighted = sorted(weights.items())
    draw = int(opening._digest([POLICY, seed, opening.history_key(board), "selection", weighted]), 16) % sum(weights.values())
    for uci, weight in weighted:
        if draw < weight:
            return uci
        draw -= weight


def baseline_guard(board, baseline_move, baseline_info):
    """Why optional probes should be skipped, or None when useful to compare."""
    baseline = score_line(board, baseline_info, baseline_move)
    if baseline is None or baseline["depth"] < MIN_DEPTH:
        return "baseline_unreliable"
    if len(board.move_stack) < OPENING_PLIES:
        # Preserve v3's complete opening policy, including which exact packets
        # permit probes. Artist preferences start only after the opening phase.
        return None
    if baseline["cp"] >= CONVERSION_CP:
        return "conversion_priority"
    if _pv_has_terminal(board, baseline["pv_uci"]):
        return "forcing_line_preserved"
    if _style(board, baseline["pv_uci"]) is None:
        return "insufficient_reply_evidence"
    return None


def receipt(board, search_plan, baseline_move, baseline_info, probes=(), seed="", *,
            baseline_elapsed_ms=0, extra_elapsed_ms=0):
    if search_plan.get("mode") == "opening":
        original = opening.receipt(board, search_plan["opening_plan"], baseline_move,
                                   baseline_info, probes, seed, baseline_elapsed_ms=baseline_elapsed_ms,
                                   extra_elapsed_ms=extra_elapsed_ms)
        result = dict(original)
        result.update(policy=POLICY, mode="opening", selection_rule="opening_variety_preserved",
                      opening_receipt=original, quality_pool=list(original["selection_pool"]),
                      style_evidence={}, style_exclusions={},
                      style_units="style preference is inactive during the opening",
                      tolerance_cp=OPENING_TOLERANCE_CP, conversion_cp=None)
        return result
    baseline = score_line(board, baseline_info, baseline_move)
    proposed = proposals(search_plan, baseline_move) if search_plan["eligible"] else []
    base_uci = baseline_move.uci()
    selected, reason = base_uci, search_plan["reason"]
    pool, quality_pool, weights = [base_uci], [base_uci], {base_uci: 3}
    styles, exclusions = {}, {}
    if search_plan["eligible"]:
        guard = baseline_guard(board, baseline_move, baseline_info)
        if guard is not None:
            reason = guard
        elif any(probe["reason"] == "candidate_failed" for probe in probes):
            reason = "candidate_failed"
        elif any(probe["reason"] == "candidate_score_conflict" for probe in probes):
            reason = "candidate_score_conflict"
        else:
            base_style = _style(board, baseline["pv_uci"])
            if base_style is None:
                reason = "insufficient_reply_evidence"
            else:
                styles[base_uci] = base_style
                for probe in probes:
                    uci = probe["uci"]
                    if not probe["admitted"] or uci not in proposed:
                        continue
                    quality_pool.append(uci)
                    evidence = probe["candidate_final"]
                    if _pv_has_terminal(board, evidence["pv_uci"]):
                        exclusions[uci] = "terminal_continuation"
                        continue
                    style = _style(board, evidence["pv_uci"])
                    if style is None:
                        exclusions[uci] = "insufficient_reply_evidence"
                        continue
                    styles[uci] = style
                    gain = style["points"] - base_style["points"]
                    if gain < 0:
                        exclusions[uci] = "less_dynamic_after_reply"
                        continue
                    quality = 3 if probe["loss_cp"] <= 5 else 2 if probe["loss_cp"] <= 10 else 1
                    weight = quality * (1 + min(4, gain // 4))
                    if search_plan.get("early_exploration", False):
                        weight *= 2
                    pool.append(uci)
                    weights[uci] = weight
                selected = _select(board, seed, weights)
                reason = ("accepted_dynamic_alternative" if selected != base_uci else
                          "baseline_selected" if len(pool) > 1 else "no_qualified_dynamic_alternative")
    chosen = next((probe for probe in probes if probe["uci"] == selected), None)
    return {"policy": POLICY, "mode": search_plan.get("mode"), "ply": len(board.move_stack) + 1,
            "before_fen": board.fen(en_passant="fen"), "history_uci": opening.history_key(board)[1],
            "color": "white" if board.turn else "black", "candidates": search_plan["candidates"],
            "candidate_source": search_plan["candidate_source"], "proposal_limit": search_plan["proposal_limit"],
            "catalog_commit": search_plan.get("catalog_commit"), "baseline_uci": base_uci,
            "selected_uci": selected, "reason": reason, "loss_cp": chosen["loss_cp"] if chosen else 0,
            "compared_depth": baseline["depth"] if baseline else None, "baseline": baseline,
            "candidate_compared": chosen["candidate_compared"] if chosen else None,
            "candidate_final": chosen["candidate_final"] if chosen else None,
            "candidate_final_raw": chosen["candidate_final_raw"] if chosen else None,
            "proposals": proposed, "probes": list(probes), "quality_pool": sorted(set(quality_pool)),
            "selection_pool": sorted(set(pool)), "selection_weights": weights,
            "selection_rule": "quality_gated_reply_supported_style", "style_evidence": styles,
            "style_exclusions": exclusions, "style_units": "style points; not centipawns",
            "tolerance_cp": search_plan["tolerance_cp"], "conversion_cp": CONVERSION_CP,
            "baseline_budget_ms": search_plan["baseline_budget_ms"],
            "candidate_budget_ms": search_plan["extra_budget_ms"] if probes else 0,
            "probe_budget_sum_ms": round(sum(probe["budget_ms"] for probe in probes), 3),
            "extra_budget_ms": search_plan["extra_budget_ms"],
            "baseline_elapsed_ms": round(baseline_elapsed_ms, 3), "extra_elapsed_ms": round(extra_elapsed_ms, 3),
            "opening_receipt": None, "reused": False}


def pgn_headers(metadata):
    if not isinstance(metadata, dict) or metadata.get("enabled") is not True or metadata.get("policy") != POLICY:
        return {}
    seed = metadata.get("seed")
    if not isinstance(seed, str) or not re.fullmatch(r"[0-9a-f]{32}", seed):
        return {}
    return {"DynamicPlaySeed": seed, "DynamicPlayPolicy": POLICY}
