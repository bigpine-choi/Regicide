"""Conservative consistency of completed UCI iterations for one position.

This is a search-stability heuristic, not a probability of correctness or a
calibrated win probability. Callers supply *individual* completed-depth packets
from one unrestricted search. Merged ``AnalysisResult.info`` is unsuitable:
its depth, score and PV may have arrived in different packets.
"""

import chess
import chess.engine


MIN_DEPTH = 4
MIN_STABLE_DEPTHS = 2
WINDOW_DEPTHS = 3
MAX_CP_SWING = 80


def _sample(board, depth, info):
    if (not isinstance(info, dict) or type(info.get("depth")) is not int
            or info["depth"] != depth or depth < MIN_DEPTH
            or info.get("lowerbound") or info.get("upperbound")
            or info.get("multipv", 1) != 1):
        return None
    score, pv = info.get("score"), info.get("pv")
    if (not isinstance(score, chess.engine.PovScore) or score.turn != board.turn
            or not isinstance(pv, (list, tuple)) or not 1 <= len(pv) <= 128):
        return None
    position = board.copy(stack=True)
    for move in pv:
        if not isinstance(move, chess.Move) or move not in position.legal_moves:
            return None
        position.push(move)
    white_score = score.pov(chess.WHITE)
    cp, mate = white_score.score(), white_score.mate()
    if cp is not None:
        if type(cp) is not int or mate is not None or position.is_checkmate():
            return None
        return {"kind": "cp", "cp": cp, "root": pv[0].uci()}
    if type(mate) is not int:
        return None
    # A legal line ending in the claimed winner's checkmate is required even
    # for a stable *display*. It does not prove that every defense loses.
    winner = chess.WHITE if white_score.score(mate_score=100000) > 0 else chess.BLACK
    if not position.is_checkmate() or (not position.turn) != winner:
        return None
    return {"kind": "mate", "winner": winner, "root": pv[0].uci()}


def assess(board, packets):
    """Return display metadata for depth -> raw UCI packet, without mutation.

    The latest two or three consecutive exact, legal iterations at depth >= 4
    must agree on the first move and score type. CP scores span at most 80 CP; mate lines
    must end in checkmate for the same side. Bounds, missing depths and invalid
    packets cannot become stable by dropping the inconvenient latest sample.
    ``stable_depths`` counts the trailing consecutive, comparable exact samples
    (at most three); it is an iteration count, not the search depth. A volatile
    score can therefore still have three comparable samples.
    """
    result = {"reliability": "provisional", "recent_cp_range_white": None,
              "score_swing_cp": None, "stable_depths": 0}
    if (not isinstance(board, chess.Board) or not board.is_valid()
            or not isinstance(packets, dict) or not packets):
        return result
    if any(type(depth) is not int or depth < 1 for depth in packets):
        return result
    # Shallow warming-up iterations cannot certify stability. Exclude only
    # those depths, never an invalid/bounded latest eligible packet. If three
    # eligible depths exist, all three participate (including disagreement).
    depths = [depth for depth in sorted(packets) if depth >= MIN_DEPTH][-WINDOW_DEPTHS:]
    samples = [_sample(board, depth, packets[depth]) for depth in depths]
    # Report ranges only when the entire displayed window has exact CP data.
    # A mate must never masquerade as an arbitrarily large pawn advantage.
    if samples and all(sample is not None and sample["kind"] == "cp" for sample in samples):
        scores = [sample["cp"] for sample in samples]
        result["recent_cp_range_white"] = [min(scores), max(scores)]
        result["score_swing_cp"] = max(scores) - min(scores)
    previous = None
    for depth, sample in reversed(list(zip(depths, samples))):
        if sample is None or (previous is not None and depth != previous - 1):
            break
        result["stable_depths"] += 1
        previous = depth
    # "Bounded" describes the score currently displayed. A bound in an older
    # iteration still blocks stability, but must not relabel a newer exact
    # score as an upper/lower bound.
    latest = packets[depths[-1]] if depths else None
    if isinstance(latest, dict) and (latest.get("lowerbound") or latest.get("upperbound")):
        result["reliability"] = "bounded"
        return result
    if len(depths) < MIN_STABLE_DEPTHS or result["stable_depths"] != len(depths):
        return result
    first = samples[0]
    agrees = all(sample["root"] == first["root"] and sample["kind"] == first["kind"]
                 for sample in samples)
    if agrees and first["kind"] == "cp":
        agrees = result["score_swing_cp"] <= MAX_CP_SWING
    elif agrees:
        agrees = all(sample["winner"] == first["winner"] for sample in samples)
    result["reliability"] = "stable" if agrees else "volatile"
    return result
