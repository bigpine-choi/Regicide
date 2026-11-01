"""Bounded opening variety; every admitted alternative is scored by Regicide.

The catalogue proposes moves, never evaluates their strength. The four-move
shortlists are a project-authored conservative practice repertoire, not a
popularity ranking. Search, legal-move generation and evaluation stay in the
existing engine. No external engine is imported or queried here.
"""

import hashlib
import json
import re

import chess
import chess.engine

from opening_guide import CatalogError, _catalog, position_key


POLICY = "regicide-opening-variety-v1"
MAX_PLIES = 6
MIN_TOTAL_MS = 500
MIN_DEPTH = 3
TOLERANCE_CP = 35
FIRST_WHITE = ("e2e4", "d2d4", "c2c4", "g1f3")
FIRST_BLACK = {
    "e2e4": ("e7e5", "c7c5", "e7e6", "c7c6"),
    "d2d4": ("d7d5", "g8f6", "e7e6", "f7f5"),
    "c2c4": ("e7e5", "c7c5", "g8f6", "e7e6"),
    "g1f3": ("d7d5", "g8f6", "c7c5", "g7g6"),
}


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
    """All legal immediate moves on exact catalogue prefixes, without UI caps."""
    history = tuple(move.uci() for move in board.move_stack)
    catalog = _catalog()
    candidates = set()
    legal = {move.uci() for move in board.legal_moves}
    for line_id, offset in catalog.positions.get(position_key(board), ()):
        opening = catalog.openings[line_id]
        if offset < len(opening.moves) and opening.moves[:offset] == history:
            move = opening.moves[offset]
            if move in legal:
                candidates.add(move)
    if not history:
        candidates.intersection_update(FIRST_WHITE)
    elif len(history) == 1 and history[0] in FIRST_BLACK:
        candidates.intersection_update(FIRST_BLACK[history[0]])
    return sorted(candidates)


def plan(board, budget_ms, seed, *, forced=False):
    result = {"eligible": False, "reason": "outside_opening", "candidate_uci": None,
              "candidates": [], "catalog_commit": None,
              "baseline_budget_ms": budget_ms, "candidate_budget_ms": 0}
    if len(board.move_stack) >= MAX_PLIES:
        return result
    if board.chess960 or not board.is_valid() or board.root().fen(en_passant="fen") != chess.STARTING_FEN:
        result["reason"] = "custom_root"
    elif forced:
        result["reason"] = "user_forced_game"
    elif board.is_game_over(claim_draw=True):
        result["reason"] = "terminal"
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
        if len(candidates) < 2:
            result["reason"] = "insufficient_catalog_candidates"
        else:
            # Candidate selection has no mutable RNG cursor and does not depend
            # on budget, evaluation, revision, process scheduling or UI polling.
            selection = int(_digest([POLICY, seed, history_key(board)]), 16) % len(candidates)
            first = budget_ms // 2
            result.update(eligible=True, reason="compare", candidate_uci=candidates[selection],
                          baseline_budget_ms=first, candidate_budget_ms=budget_ms - first)
    return result


def score_line(board, info, required_move=None):
    """A whole completed-depth exact score/PV packet, normalized to root side."""
    if not isinstance(info, dict) or type(info.get("depth")) is not int or info["depth"] < 1:
        return None
    score = info.get("score")
    if not isinstance(score, chess.engine.PovScore) or info.get("lowerbound") or info.get("upperbound"):
        return None
    root_score = score.pov(board.turn)
    if root_score.mate() is not None or root_score.score() is None:
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


def receipt(board, search_plan, baseline_move, baseline_info, candidate_packets=None,
            candidate_final=None, candidate_move=None, candidate_error=None):
    """Return a JSON-safe decision. Missing or conflicting proof always falls back."""
    baseline = score_line(board, baseline_info, baseline_move)
    final = score_line(board, candidate_final, candidate_move) if candidate_move else None
    compared = None
    selected = baseline_move.uci()
    reason = search_plan["reason"]
    loss = None
    compared_depth = baseline["depth"] if baseline else None
    if search_plan["eligible"]:
        if baseline is None or baseline["depth"] < MIN_DEPTH:
            reason = "baseline_unreliable"
        elif selected == search_plan["candidate_uci"]:
            reason = "candidate_is_best"
        elif candidate_error:
            reason = "candidate_failed"
        elif candidate_move is None or candidate_move.uci() != search_plan["candidate_uci"] or final is None:
            reason = "candidate_unreliable"
        else:
            compared = score_line(board, (candidate_packets or {}).get(baseline["depth"]), candidate_move)
            if compared is None:
                reason = "no_common_depth"
            else:
                loss = max(0, baseline["cp"] - compared["cp"])
                if loss > TOLERANCE_CP:
                    reason = "outside_tolerance"
                elif final["cp"] < baseline["cp"] - TOLERANCE_CP:
                    # A deeper adverse result is a conservative veto, never a
                    # reason to admit a move that failed the same-depth gate.
                    reason = "deeper_candidate_warning"
                else:
                    selected, reason = candidate_move.uci(), "accepted_alternative"
    return {"policy": POLICY, "ply": len(board.move_stack) + 1,
            "before_fen": board.fen(en_passant="fen"), "history_uci": history_key(board)[1],
            "color": "white" if board.turn else "black", "candidates": search_plan["candidates"],
            "catalog_commit": search_plan["catalog_commit"],
            "candidate_uci": search_plan["candidate_uci"], "baseline_uci": baseline_move.uci(),
            "selected_uci": selected, "reason": reason, "loss_cp": loss,
            "compared_depth": compared_depth, "baseline": baseline,
            "candidate_compared": compared, "candidate_final": final,
            "candidate_final_raw": packet_summary(board, candidate_final),
            "baseline_budget_ms": search_plan["baseline_budget_ms"],
            "candidate_budget_ms": search_plan["candidate_budget_ms"] if candidate_move or candidate_error else 0,
            "reused": False}


def packet_summary(board, info):
    """Diagnostic raw score fields are not admission evidence."""
    if not isinstance(info, dict):
        return None
    score = info.get("score")
    value = score.pov(board.turn) if isinstance(score, chess.engine.PovScore) else None
    return {"depth": info.get("depth") if type(info.get("depth")) is int else None,
            "cp": value.score() if value else None, "mate": value.mate() if value else None,
            "lowerbound": bool(info.get("lowerbound")), "upperbound": bool(info.get("upperbound"))}


def pgn_headers(metadata):
    if not isinstance(metadata, dict) or metadata.get("enabled") is not True or metadata.get("policy") != POLICY:
        return {}
    seed = metadata.get("seed")
    if not isinstance(seed, str) or not re.fullmatch(r"[0-9a-f]{32}", seed):
        return {}
    return {"OpeningVarietySeed": seed, "OpeningVarietyPolicy": POLICY}
