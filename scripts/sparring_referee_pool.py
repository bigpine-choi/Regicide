"""Validate and reuse native Regicide candidate receipts without any search.

This helper neither invents moves nor evaluates positions. The original host choices require matching
position/history and legal UCI moves. Additional candidates require the native
producer's admitted, natural, completed-depth CP evidence to remain coherent.

``boss_play.score_line`` normalizes exact CP packets to
{uci, pv_uci, depth, cp, cp_white, pov}; it omits mate and bound. Those omissions
mean CP/exact only after the full normalized schema is validated below.
Original choices lacking score evidence remain distinguishable from verified
probes. A mate baseline or missing baseline evidence never expands the pool.
"""

import copy

import chess


MAX_CANDIDATES = 4
MIN_DEPTH = 4
MAX_DEPTH = 256
MAX_PV_PLIES = 128
MAX_PROBES = 64
MAX_CP = 100_000
REFERENCE_TOLERANCE_CP = 25


def _integer(value):
    return type(value) is int


def _legal_move(board, uci):
    if not isinstance(uci, str) or len(uci) not in (4, 5):
        return None
    try:
        move = chess.Move.from_uci(uci)
    except ValueError:
        return None
    return move if move in board.legal_moves and move.uci() == uci else None


def _context(board, decision):
    if (not isinstance(board, chess.Board) or board.chess960 or not board.is_valid()
            or not isinstance(decision, dict)):
        return None
    fen = board.fen(en_passant="fen")
    history = [move.uci() for move in board.move_stack]
    receipt_history = decision.get("history_uci")
    if (decision.get("before_fen") != fen or not isinstance(receipt_history, (list, tuple))
            or any(not isinstance(value, str) for value in receipt_history)
            or list(receipt_history) != history):
        return None
    if "color" in decision and decision["color"] != ("white" if board.turn else "black"):
        return None
    if "ply" in decision and (not _integer(decision["ply"]) or decision["ply"] != len(history) + 1):
        return None
    return {"before_fen": fen, "history_uci": history,
            "pov": "white" if board.turn else "black", "policy": decision.get("policy")}


def _line(board, value, uci):
    if not isinstance(value, dict) or value.get("uci") != uci:
        return None
    depth, cp, white_cp = value.get("depth"), value.get("cp"), value.get("cp_white")
    pov = "white" if board.turn else "black"
    if (not _integer(depth) or not MIN_DEPTH <= depth <= MAX_DEPTH
            or not _integer(cp) or abs(cp) > MAX_CP or not _integer(white_cp)
            or white_cp != cp * (1 if board.turn else -1) or value.get("pov") != pov
            or value.get("mate") is not None or value.get("mate_white") is not None
            or value.get("bound", "exact") != "exact"
            or value.get("score_bound", "exact") != "exact"
            or value.get("lowerbound") or value.get("upperbound")
            or not _integer(value.get("multipv", 1)) or value.get("multipv", 1) != 1):
        return None
    fen = board.fen(en_passant="fen")
    if any(key in value and value[key] != fen for key in ("fen", "before_fen", "source_fen")):
        return None
    pv = value.get("pv_uci")
    if not isinstance(pv, (list, tuple)) or not 1 <= len(pv) <= MAX_PV_PLIES or pv[0] != uci:
        return None
    replay = board.copy(stack=True)
    for item in pv:
        move = _legal_move(replay, item)
        if move is None:
            return None
        replay.push(move)
    if replay.is_checkmate():
        return None  # A finite CP score cannot certify a mating PV.
    return {"uci": uci, "pv_uci": list(pv), "depth": depth, "cp": cp,
            "cp_white": white_cp, "pov": pov, "mate": None, "bound": "exact"}


def _baseline(board, decision, uci):
    baseline = _line(board, decision.get("baseline"), uci)
    if baseline is None:
        return None
    for key in ("actual_reference_depth", "verification_depth", "compared_depth"):
        depth = decision.get(key)
        if depth is not None and (not _integer(depth) or depth != baseline["depth"]):
            return None
    return baseline


def _probe(board, probe, baseline):
    if (not isinstance(probe, dict) or probe.get("admitted") is not True
            or not isinstance(probe.get("naturalness"), dict)
            or probe["naturalness"].get("admitted") is not True
            or probe.get("error") is not None):
        return None
    uci = probe.get("uci")
    if _legal_move(board, uci) is None:
        return None
    common = _line(board, probe.get("candidate_compared"), uci)
    final = _line(board, probe.get("candidate_final"), uci)
    if (common is None or final is None or not _integer(probe.get("compared_depth"))
            or common["depth"] != probe["compared_depth"] or common["depth"] != baseline["depth"]
            or final["depth"] < common["depth"]
            or final["depth"] == common["depth"] and final["cp"] != common["cp"]):
        return None
    delta = baseline["cp"] - common["cp"]
    recorded_delta = probe.get("reference_delta_cp")
    if (delta < -REFERENCE_TOLERANCE_CP
            or recorded_delta is not None and (not _integer(recorded_delta) or recorded_delta != delta)):
        return None
    loss = probe.get("loss_cp")
    if loss is not None and (not _integer(loss) or loss != max(0, delta)):
        return None
    raw = probe.get("candidate_final_raw")
    if raw is not None:
        if (not isinstance(raw, dict) or raw.get("mate") is not None
                or raw.get("mate_white") is not None
                or raw.get("bound", "exact") != "exact"
                or raw.get("score_bound", "exact") != "exact"
                or raw.get("lowerbound") or raw.get("upperbound")
                or not _integer(raw.get("depth")) or raw["depth"] != final["depth"]
                or not _integer(raw.get("cp")) or raw["cp"] != final["cp"]):
            return None
    return {"uci": uci, "candidate_compared": common, "candidate_final": final,
            "compared_depth": common["depth"], "reference_delta_cp": delta,
            "naturalness": copy.deepcopy(probe["naturalness"]),
            "probe_reason": probe.get("reason")}


def build_pool(board, decision, max_candidates=MAX_CANDIDATES):
    """Return baseline/proposal first, then coherent admitted native probes.

    Invalid original context or either original choice returns an empty pool.
    Missing/malformed finite baseline evidence disables expansion but preserves
    the legal original host choices. Probe order is preserved, duplicates are
    removed, and at most four moves are returned. No engine or file is opened.
    """
    if not _integer(max_candidates) or not 1 <= max_candidates <= MAX_CANDIDATES:
        raise ValueError("Candidate pool size must be an integer between one and four")
    context = _context(board, decision)
    if context is None:
        return []
    baseline_uci, proposal_uci = decision.get("baseline_uci"), decision.get("selected_uci")
    if _legal_move(board, baseline_uci) is None or _legal_move(board, proposal_uci) is None:
        return []
    baseline = _baseline(board, decision, baseline_uci)
    result, seen = [], set()
    for uci, origin in ((baseline_uci, "baseline"), (proposal_uci, "proposal")):
        if uci in seen or len(result) >= max_candidates:
            continue
        result.append({"uci": uci, "origin": origin,
            "native_evidence": {**copy.deepcopy(context), "validation": "host_receipt_legal_choice_only",
                                "baseline": copy.deepcopy(baseline)}})
        seen.add(uci)
    probes = decision.get("probes", [])
    if baseline is None or not isinstance(probes, (list, tuple)) or len(probes) > MAX_PROBES:
        return result
    for probe in probes:
        if len(result) >= max_candidates:
            break
        verified = _probe(board, probe, baseline)
        if verified is None or verified["uci"] in seen:
            continue
        result.append({"uci": verified["uci"], "origin": "verified_probe",
            "native_evidence": {**copy.deepcopy(context), **verified,
                "validation": "same_context_native_completed_depth_probe",
                "baseline": copy.deepcopy(baseline)}})
        seen.add(verified["uci"])
    # A completed native depth-6 result is still a genuine native candidate
    # when its depth-7 comparison was unfinished. It supplies no native loss
    # estimate: the separate Stockfish round must compare it to the baseline.
    for probe in probes:
        if len(result) >= max_candidates:
            break
        if (not isinstance(probe, dict) or probe.get("reason") != "no_common_depth"
                or probe.get("error") is not None or probe.get("admitted") is not False):
            continue
        row = {"uci": probe.get("uci"), "line": probe.get("candidate_final"),
               "naturalness": probe.get("naturalness"), "original_reason": probe["reason"]}
        line = _line(board, row["line"], row["uci"])
        raw = probe.get("candidate_final_raw")
        if (line is None or not isinstance(raw, dict) or raw.get("mate") is not None
                or raw.get("mate_white") is not None or raw.get("bound", "exact") != "exact"
                or raw.get("score_bound", "exact") != "exact"
                or raw.get("lowerbound") or raw.get("upperbound")
                or not _integer(raw.get("depth")) or not _integer(raw.get("cp"))
                or raw.get("depth") != line["depth"] or raw.get("cp") != line["cp"]):
            continue
        result = extend_pool(board, decision, result, [row], origin="partial_probe", max_candidates=max_candidates)
    return result


def extend_pool(board, decision, pool, candidates, *, origin="shallow_discovery", max_candidates=MAX_CANDIDATES):
    """Attach individually completed native lines; never compare unlike depths."""
    if origin not in ("partial_probe", "shallow_discovery"):
        raise ValueError("Unknown native candidate origin")
    context = _context(board, decision)
    if context is None or not isinstance(pool, list) or not isinstance(candidates, (list, tuple)):
        return copy.deepcopy(pool) if isinstance(pool, list) else []
    if not _integer(max_candidates) or not 1 <= max_candidates <= MAX_CANDIDATES or len(candidates) > MAX_PROBES:
        raise ValueError("Invalid native candidate pool limit")
    result = copy.deepcopy(pool[:max_candidates])
    seen = {row["uci"] for row in result}
    for row in candidates:
        if len(result) >= max_candidates:
            break
        if (not isinstance(row, dict) or _legal_move(board, row.get("uci")) is None or row.get("uci") in seen
                or not isinstance(row.get("naturalness"), dict)
                or row["naturalness"].get("admitted") is not True):
            continue
        line = _line(board, row.get("line"), row.get("uci"))
        if line is None:
            continue
        if origin == "shallow_discovery" and line["depth"] != MIN_DEPTH:
            continue
        final = _line(board, row.get("completed_final_line", row["line"]), row["uci"])
        if (final is None or final["depth"] < line["depth"]
                or final["depth"] == line["depth"] and final["cp"] != line["cp"]):
            continue
        result.append({"uci": line["uci"], "origin": origin, "native_evidence": {
            **copy.deepcopy(context), "line": line, "completed_final_line": final,
            "naturalness": copy.deepcopy(row["naturalness"]),
            "baseline": copy.deepcopy(decision.get("baseline")),
            "original_reason": row.get("original_reason"),
            "validation": "individual_native_completed_depth; no baseline CP comparison",
            "native_comparison_available": False}})
        seen.add(line["uci"])
    return result
