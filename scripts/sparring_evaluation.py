"""Exact-packet and convergence rules for the sparring position gauge.

These rules spend more of the same engine's time on unsettled positions. They
are evidence filters, not calibrated correctness or win probabilities. A legal
mate PV verifies that line only, not every possible defense.
"""

import chess
import chess.engine


POLICY = "regicide-sparring-evaluation-v1"
BASE_BUDGET_MS = 3000
MAX_BUDGET_MS = 10000
MIN_REFINED_DEPTH = 8
CONSECUTIVE_DEPTHS = 3
MAX_CP_SPAN = 35


def validate_packet(board, info):
    """Return one exact completed iteration in White's POV, or None.

    Never pass merged ``AnalysisResult.info`` here: depth, score and PV must
    have arrived together. This function does not mutate the position/packet.
    """
    if (not isinstance(board, chess.Board) or not board.is_valid()
            or not isinstance(info, dict)
            or type(info.get("depth")) is not int or info["depth"] < 1
            or info.get("lowerbound") or info.get("upperbound")
            or type(info.get("multipv", 1)) is not int or info.get("multipv", 1) != 1):
        return None
    score, pv = info.get("score"), info.get("pv")
    if (not isinstance(score, chess.engine.PovScore) or score.turn != board.turn
            or not isinstance(pv, (tuple, list)) or not 0 < len(pv) <= 128):
        return None
    after = board.copy(stack=True)
    for move in pv:
        if not isinstance(move, chess.Move) or move not in after.legal_moves:
            return None
        after.push(move)
    white_score = score.white()
    cp, mate = white_score.score(), white_score.mate()
    winner = None
    if cp is not None:
        if type(cp) is not int or mate is not None or after.is_checkmate():
            return None
    else:
        relative_mate = score.relative.mate()
        if type(mate) is not int or type(relative_mate) is not int or relative_mate == 0:
            return None
        winning_color = board.turn if relative_mate > 0 else not board.turn
        expected_plies = 2 * abs(relative_mate) - (1 if relative_mate > 0 else 0)
        if (not after.is_checkmate() or after.turn == winning_color
                or len(pv) != expected_plies):
            return None
        winner = "white" if winning_color else "black"
    return {"depth": info["depth"], "cp": cp, "mate": mate,
            "root": pv[0].uci(), "winner": winner,
            "pv_uci": [move.uci() for move in pv]}


def converged(board, packets):
    """Whether the latest three exact consecutive iterations permit stopping.

    Include invalid/bounded score+depth iterations in ``packets``. Dropping an
    inconvenient latest iteration could falsely certify the previous score.
    Depth-only progress messages are not completed iterations.
    """
    if (not isinstance(packets, dict) or len(packets) < CONSECUTIVE_DEPTHS
            or any(type(depth) is not int or depth < 1 for depth in packets)):
        return False
    depths = sorted(packets)[-CONSECUTIVE_DEPTHS:]
    if (depths[-1] < MIN_REFINED_DEPTH
            or depths != list(range(depths[-1] - CONSECUTIVE_DEPTHS + 1, depths[-1] + 1))):
        return False
    samples = [validate_packet(board, packets[depth]) for depth in depths]
    if any(sample is None or sample["depth"] != depth for sample, depth in zip(samples, depths)):
        return False
    if len({sample["root"] for sample in samples}) != 1:
        return False
    if all(sample["cp"] is not None for sample in samples):
        values = [sample["cp"] for sample in samples]
        return max(values) - min(values) <= MAX_CP_SPAN
    return (all(sample["mate"] is not None for sample in samples)
            and len({(sample["mate"], sample["winner"]) for sample in samples}) == 1)
