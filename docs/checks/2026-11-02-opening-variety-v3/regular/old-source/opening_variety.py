"""Bounded opening variety; every admitted alternative is scored by Regicide.

The catalogue proposes moves, never evaluates their strength. Search, legal-
move generation and evaluation stay in the
existing engine. No external engine is imported or queried here.
"""

import hashlib
import json
import re

import chess
import chess.engine

from opening_guide import CatalogError, _catalog, position_key


POLICY = "regicide-opening-variety-v2"
KNOWN_POLICIES = ("regicide-opening-variety-v1", POLICY)
MAX_PLIES = 16
MIN_TOTAL_MS = 500
MIN_DEPTH = 4
TOLERANCE_CP = 25
MAX_PROBES = 6
MIN_PROBE_MS = 20
MAX_EXTRA_MS = 2000


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, separators=(",", ":"),
                                     sort_keys=True).encode("utf-8")).hexdigest()


def history_key(board):
    return [board.root().fen(en_passant="fen"), [move.uci() for move in board.move_stack],
            board.fen(en_passant="fen")]


def memo_key(board, budget_ms, binary_sha256, evaluation_id, *, seed="", catalog_id=None):
    # Full history/counters matter to draw rules. The display position key alone
    # is deliberately insufficient for either a decision or an evaluation memo.
    return _digest([POLICY, seed, catalog_id, history_key(board), budget_ms, binary_sha256, evaluation_id])


def candidate_moves(board):
    """Unique legal continuations of this position, including transpositions."""
    catalog = _catalog()
    candidates = set()
    legal = {move.uci() for move in board.legal_moves}
    for line_id, offset in catalog.positions.get(position_key(board), ()):
        opening = catalog.openings[line_id]
        if offset < len(opening.moves):
            move = opening.moves[offset]
            if move in legal:
                candidates.add(move)
    return sorted(candidates)


def extra_budget(budget_ms):
    return min(budget_ms, MAX_EXTRA_MS) if budget_ms >= MIN_TOTAL_MS else 0


def has_repetition(board):
    """Do not re-enter a repertoire after any repeated position in this game."""
    replay = board.root()
    seen = {position_key(replay)}
    for move in board.move_stack:
        replay.push(move)
        key = position_key(replay)
        if key in seen:
            return True
        seen.add(key)
    return False


def plan(board, budget_ms, seed, *, forced=False):
    result = {"eligible": False, "reason": "outside_opening", "candidate_uci": None,
              "candidates": [], "candidate_order": [], "catalog_commit": None,
              "baseline_budget_ms": budget_ms, "candidate_budget_ms": 0, "extra_budget_ms": 0}
    if len(board.move_stack) >= MAX_PLIES:
        return result
    if board.chess960 or not board.is_valid() or board.root().fen(en_passant="fen") != chess.STARTING_FEN:
        result["reason"] = "custom_root"
    elif forced:
        result["reason"] = "user_forced_game"
    elif board.is_game_over(claim_draw=True):
        result["reason"] = "terminal"
    elif has_repetition(board):
        result["reason"] = "repetition"
    elif board.is_check():
        result["reason"] = "in_check"
    elif budget_ms < MIN_TOTAL_MS:
        result["reason"] = "short_budget"
    else:
        try:
            candidates = candidate_moves(board)
            result["catalog_commit"] = _catalog().source["commit"]
        except CatalogError:
            result["reason"] = "catalog_unavailable"
            return result
        result["candidates"] = candidates
        if not candidates:
            result["reason"] = "insufficient_catalog_candidates"
        else:
            # Candidate selection has no mutable RNG cursor and does not depend
            # on budget, evaluation, revision, process scheduling or UI polling.
            order = sorted(candidates, key=lambda uci: (_digest([POLICY, seed, history_key(board), "proposal", uci]), uci))
            extra = extra_budget(budget_ms)
            result.update(eligible=True, reason="compare", candidate_uci=order[0], candidate_order=order,
                          candidate_budget_ms=extra, extra_budget_ms=extra)
    return result


def proposals(search_plan, baseline_move):
    return [uci for uci in search_plan["candidate_order"] if uci != baseline_move.uci()][:MAX_PROBES]


def selected_move(board, seed, pool, weights=None):
    choices = sorted(set(pool))
    if not choices:
        raise ValueError("Opening selection needs at least the baseline move")
    weighted = [(uci, weights[uci] if weights is not None else 1) for uci in choices]
    if any(type(weight) is not int or weight < 1 for _uci, weight in weighted):
        raise ValueError("Opening selection weights must be positive integers")
    draw = int(_digest([POLICY, seed, history_key(board), "selection", weighted]), 16) % sum(weight for _uci, weight in weighted)
    for uci, weight in weighted:
        if draw < weight:
            return uci
        draw -= weight


def score_line(board, info, required_move=None):
    """A whole completed-depth exact score/PV packet, normalized to root side."""
    if not isinstance(info, dict) or type(info.get("depth")) is not int or info["depth"] < 1:
        return None
    score = info.get("score")
    if not isinstance(score, chess.engine.PovScore) or info.get("lowerbound") or info.get("upperbound"):
        return None
    root_score = score.pov(board.turn)
    if root_score.mate() is not None or type(root_score.score()) is not int:
        return None
    pv = info.get("pv")
    if not isinstance(pv, (list, tuple)) or not pv or len(pv) > 128:
        return None
    if required_move is not None and pv[0] != required_move:
        return None
    replay = board.copy(stack=True)
    uci = []
    for move in pv:
        if not isinstance(move, chess.Move) or move not in replay.legal_moves:
            return None
        uci.append(move.uci())
        replay.push(move)
    return {"uci": uci[0], "pv_uci": uci, "depth": info["depth"],
            "cp": root_score.score(), "cp_white": score.pov(chess.WHITE).score(),
            "pov": "white" if board.turn else "black"}


def assess(board, baseline_move, baseline_info, proposed_uci, candidate_packets=None,
           candidate_final=None, candidate_move=None, candidate_error=None, *, budget_ms=0, elapsed_ms=0):
    """One probe's evidence, never permission to replace the baseline by itself."""
    baseline = score_line(board, baseline_info, baseline_move)
    final = score_line(board, candidate_final, candidate_move) if candidate_move else None
    raw = packet_summary(board, candidate_final)
    compared = None
    loss = None
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
            if compared["cp"] > baseline["cp"] + TOLERANCE_CP or (
                    final["depth"] == baseline["depth"] and final["cp"] != compared["cp"]):
                reason = "candidate_score_conflict"
            elif loss > TOLERANCE_CP:
                reason = "outside_tolerance"
            elif final["cp"] < baseline["cp"] - TOLERANCE_CP:
                reason = "deeper_candidate_warning"
            else:
                reason = "within_tolerance"
    return {"uci": proposed_uci, "admitted": reason == "within_tolerance", "reason": reason,
            "loss_cp": loss, "compared_depth": baseline["depth"] if baseline else None,
            "candidate_compared": compared, "candidate_final": final, "candidate_final_raw": raw,
            "budget_ms": budget_ms, "elapsed_ms": round(elapsed_ms, 3), "error": candidate_error}


def receipt(board, search_plan, baseline_move, baseline_info, probes=(), seed="", *,
            baseline_elapsed_ms=0, extra_elapsed_ms=0):
    """The full-budget baseline remains eligible, including on every failure."""
    baseline = score_line(board, baseline_info, baseline_move)
    proposed = proposals(search_plan, baseline_move) if search_plan["eligible"] else []
    selection_pool = {baseline_move.uci()}
    proposal_count = sum(uci != baseline_move.uci() for uci in search_plan["candidate_order"])
    tested_count = sum(probe["candidate_compared"] is not None and
                       probe["reason"] not in ("candidate_failed", "candidate_unreliable", "no_common_depth") for probe in probes)
    weights = {baseline_move.uci(): 1}
    reason = search_plan["reason"]
    selected = baseline_move.uci()
    if search_plan["eligible"]:
        if baseline is None or baseline["depth"] < MIN_DEPTH:
            reason = "baseline_unreliable"
        elif any(probe["reason"] == "candidate_failed" for probe in probes):
            reason = "candidate_failed"
        elif any(probe["reason"] == "candidate_score_conflict" for probe in probes):
            reason = "candidate_score_conflict"
        else:
            selection_pool.update(probe["uci"] for probe in probes if probe["admitted"] and probe["uci"] in proposed)
            # Baseline inclusion is certain, while each alternative was sampled
            # from N proposals. Correct that inclusion bias with integer m:N
            # weights, without relaxing the admission threshold.
            weights = {uci: (max(1, min(tested_count, proposal_count)) if uci == baseline_move.uci()
                             else max(1, proposal_count)) for uci in selection_pool}
            selected = selected_move(board, seed, selection_pool, weights)
            reason = ("accepted_alternative" if selected != baseline_move.uci() else
                      "baseline_selected" if len(selection_pool) > 1 else
                      "no_qualified_alternative" if proposed else "candidate_is_best")
    chosen = next((probe for probe in probes if probe["uci"] == selected), None)
    return {"policy": POLICY, "ply": len(board.move_stack) + 1,
            "before_fen": board.fen(en_passant="fen"), "history_uci": history_key(board)[1],
            "color": "white" if board.turn else "black", "candidates": search_plan["candidates"],
            "catalog_commit": search_plan["catalog_commit"],
            "candidate_uci": chosen["uci"] if chosen else (proposed[0] if proposed else None),
            "baseline_uci": baseline_move.uci(), "selected_uci": selected, "reason": reason,
            "loss_cp": chosen["loss_cp"] if chosen else 0,
            "compared_depth": baseline["depth"] if baseline else None, "baseline": baseline,
            "candidate_compared": chosen["candidate_compared"] if chosen else None,
            "candidate_final": chosen["candidate_final"] if chosen else None,
            "candidate_final_raw": chosen["candidate_final_raw"] if chosen else None,
            "proposals": proposed, "probes": list(probes), "selection_pool": sorted(selection_pool),
            "proposal_count": proposal_count, "tested_count": tested_count, "selection_weights": weights,
            "baseline_budget_ms": search_plan["baseline_budget_ms"],
            "candidate_budget_ms": search_plan["extra_budget_ms"] if probes else 0,
            "probe_budget_sum_ms": round(sum(probe["budget_ms"] for probe in probes), 3),
            "extra_budget_ms": search_plan["extra_budget_ms"],
            "baseline_elapsed_ms": round(baseline_elapsed_ms, 3), "extra_elapsed_ms": round(extra_elapsed_ms, 3),
            "reused": False}


def packet_summary(board, info):
    """Diagnostic raw score fields are not admission evidence."""
    if not isinstance(info, dict):
        return None
    score = info.get("score")
    value = score.pov(board.turn) if isinstance(score, chess.engine.PovScore) else None
    cp, mate = (value.score(), value.mate()) if value is not None else (None, None)
    return {"depth": info.get("depth") if type(info.get("depth")) is int else None,
            "cp": cp if type(cp) is int else None, "mate": mate if type(mate) is int else None,
            "lowerbound": bool(info.get("lowerbound")), "upperbound": bool(info.get("upperbound"))}


def pgn_headers(metadata):
    if not isinstance(metadata, dict) or metadata.get("enabled") is not True or metadata.get("policy") not in KNOWN_POLICIES:
        return {}
    seed = metadata.get("seed")
    if not isinstance(seed, str) or not re.fullmatch(r"[0-9a-f]{32}", seed):
        return {}
    return {"OpeningVarietySeed": seed, "OpeningVarietyPolicy": metadata["policy"]}
