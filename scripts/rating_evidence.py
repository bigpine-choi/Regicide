"""Strict, same-position move-quality evidence for the descriptive rating.

No engine calls or rating scale live here. A mate is a separate event, never a
fabricated centipawn value. Stability describes repeat search evidence only;
even an exact legal mate PV does not prove that every defense loses.
"""

import chess

import sparring_evaluation


METHOD = "regicide-rating-evidence-v1"
MIN_DEPTH = 4
MAX_SPAN_CP = 80
REFERENCE_TOLERANCE_CP = 25


def empty(reason="not_observed"):
    return {"method": METHOD, "valid": False, "kind": None, "depth": None,
            "best_cp": None, "played_cp": None, "best_mate": None, "played_mate": None,
            "best_uci": None, "played_uci": None, "reliability": "limited",
            "root_changes": None, "cp_span": None, "reason": reason}


def _sample(board, info, move):
    value = sparring_evaluation.validate_packet(board, info)
    if (value is None or value["depth"] < MIN_DEPTH or not isinstance(move, chess.Move)
            or value["root"] != move.uci()):
        return None
    sign = 1 if board.turn else -1
    return {"depth": value["depth"], "uci": value["root"], "pv_uci": value["pv_uci"],
            "cp": None if value["cp"] is None else value["cp"] * sign,
            "mate": None if value["mate"] is None else value["mate"] * sign}


def reference(board, move, info, packets):
    """Require the final evidence to exist as one raw completed packet."""
    value = _sample(board, info, move)
    if value is None or not isinstance(packets, dict):
        return None
    raw = _sample(board, packets.get(value["depth"]), move)
    if raw != value:
        return None
    completed = [depth for depth, packet in packets.items()
                 if type(depth) is int and isinstance(packet, dict)
                 and packet.get("depth") == depth and "score" in packet]
    if completed and max(completed) > value["depth"]:
        # Do not silently borrow an older exact line after a newer result.
        return None
    return raw


def _kind(best, played):
    first, actual = best["mate"], played["mate"]
    if first is None and actual is None:
        return "cp_loss" if played["cp"] <= best["cp"] + REFERENCE_TOLERANCE_CP else None
    if first is not None and first < 0:
        return "already_lost" if actual is not None and actual < 0 else None
    if actual is not None and actual < 0:
        return "allowed_mate"
    if first is not None and first > 0:
        return "mate_preserved" if actual is not None and actual > 0 else "missed_mate"
    # A restricted move finding a win that the reference did not see is a
    # reference disagreement, not evidence of extraordinary player skill.
    return None


def _stability(board, best_move, played_move, best_packets, played_packets, depth, kind):
    depths = sorted(number for number, packet in best_packets.items()
                    if type(number) is int and MIN_DEPTH <= number <= depth
                    and isinstance(packet, dict) and packet.get("depth") == number
                    and "score" in packet)[-3:]
    best, actual, roots = [], [], []
    complete = len(depths) >= 2 and depths == list(range(depth - len(depths) + 1, depth + 1))
    for number in depths:
        raw_best = sparring_evaluation.validate_packet(board, best_packets[number])
        roots.append(raw_best["root"] if raw_best else None)
        first = _sample(board, best_packets[number], best_move)
        second = _sample(board, played_packets.get(number), played_move)
        best.append(first)
        actual.append(second)
        complete = complete and first is not None and second is not None
    root_changes = sum(left != right for left, right in zip(roots, roots[1:]))
    spans = []
    for samples in (best, actual):
        if samples and all(sample is not None and sample["cp"] is not None for sample in samples):
            values = [sample["cp"] for sample in samples]
            spans.append(max(values) - min(values))
    cp_span = max(spans) if len(spans) == 2 else None
    stable = complete and root_changes == 0 and all(_kind(a, b) == kind for a, b in zip(best, actual))
    if stable and kind == "cp_loss":
        losses = [max(0, a["cp"] - b["cp"]) for a, b in zip(best, actual)]
        stable = cp_span <= MAX_SPAN_CP and max(losses) - min(losses) <= MAX_SPAN_CP
    elif stable:
        # A mate event is only repeated evidence when score kinds, winners,
        # and reported distances agree across the completed depths.
        stable = len({(a["mate"], b["mate"]) for a, b in zip(best, actual)}) == 1
        if stable and spans:
            stable = max(spans) <= MAX_SPAN_CP
    return {"reliability": "stable" if stable else "limited",
            "root_changes": root_changes, "cp_span": cp_span}


def assess(board, best_move, best_info, best_packets, played_move, *,
           played_info=None, played_packets=None, returned_move=None):
    """Compare actual and best roots at the same completed depth in human POV."""
    result = empty("reference_unreliable")
    if not isinstance(board, chess.Board) or not board.is_valid():
        return result
    for key, move in (("best_uci", best_move), ("played_uci", played_move)):
        if not isinstance(move, chess.Move) or move not in board.legal_moves:
            result["reason"] = "illegal_root_move"
            return result
        result[key] = move.uci()
    first = reference(board, best_move, best_info, best_packets)
    if first is None:
        return result
    if best_move == played_move and played_info is None:
        second, final = first, first
        played_packets = best_packets
    else:
        result["reason"] = "actual_move_unreliable"
        if returned_move != played_move:
            return result
        final = reference(board, played_move, played_info, played_packets)
        if final is None or final["depth"] < first["depth"]:
            return result
        second = _sample(board, played_packets.get(first["depth"]), played_move)
        if second is None or second["depth"] != first["depth"]:
            result["reason"] = "no_common_depth"
            return result
    kind = _kind(first, second)
    if kind is None:
        result["reason"] = "reference_conflict"
        return result
    result.update(valid=True, kind=kind, depth=first["depth"],
                  best_cp=first["cp"], played_cp=second["cp"],
                  best_mate=first["mate"], played_mate=second["mate"],
                  reason="same_move_completed_depth" if best_move == played_move else "same_position_completed_depth")
    result.update(_stability(board, best_move, played_move, best_packets, played_packets, first["depth"], kind))
    if final["depth"] > first["depth"] and (final["mate"] != second["mate"]
            or final["cp"] is not None and second["cp"] is not None
               and abs(final["cp"] - second["cp"]) > MAX_SPAN_CP):
        result["reliability"] = "limited"
    return result
