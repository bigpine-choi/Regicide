"""Human-oriented position complexity and bounded per-turn time guidance.

This is an original, transparent heuristic, not a measured human error model.
It never searches or chooses a chess move. Structural difficulty is independent
of engine score/depth. An exact, stable, position-matching score may modestly
adjust time allocation, but cannot change the complexity score.

The recommended total is anchored to remaining time + elapsed current-turn
time. Repeated polling therefore consumes a fixed allowance instead of moving
its deadline forward. Neither the expected moves remaining nor the recommended
seconds guarantee that a game can be completed before flag fall.
"""

import math

import chess

METHOD = "regicide-human-decision-support-v2"
NOTE = "사람의 판단 부담을 구조적으로 추정한 실험 지표이며, 실제 난이도나 정답 확률을 측정한 값은 아닙니다."


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _milliseconds(value):
    return max(0, int(value)) if _number(value) else None


def _color_name(color):
    return "white" if color else "black"


def _inactive(board, human_color, reason):
    return {"method": METHOD, "experimental": True, "status": "inactive",
            "fen": board.fen(en_passant="fen") if isinstance(board, chess.Board) else None,
            "human_color": _color_name(human_color) if type(human_color) is bool else None,
            "complexity": {"score": None, "label": "대기", "reasons": [reason],
                           "features": {}, "basis": "position_features"},
            "recommendation": {"status": "inactive", "total_ms": None,
                               "remaining_ms": None, "spent_ms": None, "reserve_ms": None,
                               "urgency": "normal", "reason": reason,
                               "estimated_moves_left": None},
            "note": NOTE}


def _advanced_passers(board, color):
    result = 0
    enemy = board.pieces(chess.PAWN, not color)
    for square in board.pieces(chess.PAWN, color):
        rank, file = chess.square_rank(square), chess.square_file(square)
        relative_rank = rank if color else 7 - rank
        if relative_rank < 4:
            continue
        ahead = lambda other: chess.square_rank(other) > rank if color else chess.square_rank(other) < rank
        if not any(abs(chess.square_file(other) - file) <= 1 and ahead(other) for other in enemy):
            result += 1
    return result


def _features(board, human_color, legal):
    pieces = board.piece_map()
    attacked_own, loose_own, loose_enemy, pinned = [], 0, 0, 0
    contacts = 0
    for square, piece in pieces.items():
        if piece.piece_type == chess.KING:
            continue
        attacked = board.is_attacked_by(not piece.color, square)
        defended = board.is_attacked_by(piece.color, square)
        if attacked and defended:
            contacts += 1
        if piece.color == human_color:
            if attacked:
                attacked_own.append(square)
                if not defended and piece.piece_type != chess.PAWN:
                    loose_own += 1
            if board.is_pinned(human_color, square):
                pinned += 1
        elif attacked and not defended and piece.piece_type != chess.PAWN:
            loose_enemy += 1
    overloaded = sum(sum(square in board.attacks(defender) for square in attacked_own) >= 2
                     for defender, piece in pieces.items()
                     if piece.color == human_color and piece.piece_type != chess.KING)
    king = board.king(human_color)
    king_contacts = (sum(board.is_attacked_by(not human_color, sq)
                         for sq in chess.SquareSet(chess.BB_KING_ATTACKS[king]))
                     if king is not None else 0)
    captures = sum(board.is_capture(move) for move in legal)
    checks = sum(board.gives_check(move) for move in legal)
    promotions = len({move.from_square for move in legal if move.promotion})
    own_passers, enemy_passers = _advanced_passers(board, human_color), _advanced_passers(board, not human_color)
    nonpawns = sum(piece.piece_type not in (chess.KING, chess.PAWN) for piece in pieces.values())
    pawns = sum(piece.piece_type == chess.PAWN for piece in pieces.values())
    return {"legal_choices": len(legal), "capture_choices": captures,
            "checking_choices": checks, "promotion_pawns": promotions,
            "own_loose_pieces": loose_own, "enemy_loose_pieces": loose_enemy,
            "own_pinned_pieces": pinned, "overloaded_defenders": overloaded,
            "tactical_contacts": contacts, "attacked_king_neighbors": king_contacts,
            "in_check": board.is_check(), "own_advanced_passers": own_passers,
            "enemy_advanced_passers": enemy_passers,
            "nonpawn_pieces": nonpawns, "pawns": pawns,
            "pawn_ending": nonpawns == 0 and pawns > 0}


def _complexity(features):
    f = features
    if f["legal_choices"] == 1:
        return {"score": 5, "label": "낮음",
                "reasons": ["합법적인 응수가 하나여서 현재 선택 부담이 작습니다."],
                "features": f, "basis": "position_features"}
    # Choice count is deliberately only one small component: a broad quiet
    # opening should not rank above a narrow forcing tactical decision.
    choices = min(12, max(0, f["legal_choices"] - 2) // 3)
    tactics = (min(12, f["capture_choices"] * 2) + min(12, f["checking_choices"] * 3)
               + min(15, f["own_loose_pieces"] * 7)
               + min(6, f["enemy_loose_pieces"] * 3)
               + min(10, f["own_pinned_pieces"] * 5)
               + min(10, f["overloaded_defenders"] * 5)
               + min(8, f["tactical_contacts"] * 2))
    danger = min(10, f["attacked_king_neighbors"] * 2) + (10 if f["in_check"] else 0)
    race = min(12, (f["own_advanced_passers"] + f["enemy_advanced_passers"]) * 4)
    if f["own_advanced_passers"] and f["enemy_advanced_passers"]:
        race += 8
    ending = 9 if f["pawn_ending"] else 0
    promotion = min(8, f["promotion_pawns"] * 4)
    score = int(min(100, 10 + choices + tactics + danger + race + ending + promotion))
    label = "낮음" if score < 25 else "보통" if score < 50 else "높음" if score < 75 else "매우 높음"
    reasons = []
    if f["in_check"]:
        reasons.append("체크를 해소하는 응수의 안전성을 먼저 확인해야 합니다.")
    if f["own_loose_pieces"] or f["own_pinned_pieces"] or f["overloaded_defenders"]:
        reasons.append("공격받는 기물과 수비 관계를 함께 확인할 필요가 있습니다.")
    if f["capture_choices"] + f["checking_choices"] >= 4:
        reasons.append("잡기·체크가 여러 갈래여서 짧은 후속 수순 비교가 필요합니다.")
    if f["attacked_king_neighbors"] >= 3 and not f["in_check"]:
        reasons.append("킹 주변의 공격과 방어를 함께 살펴야 합니다.")
    if f["own_advanced_passers"] or f["enemy_advanced_passers"]:
        reasons.append("전진한 통과폰의 승격 속도를 비교해야 합니다.")
    elif f["pawn_ending"]:
        reasons.append("기물이 적어도 킹의 자리와 폰의 템포가 중요합니다.")
    if not reasons:
        reasons.append("당장의 강제 수순은 적고 후보 구상을 비교할 여지가 있습니다.")
    return {"score": score, "label": label, "reasons": reasons[:3],
            "features": f, "basis": "position_features"}


def _stable_cp(board, evaluation):
    if (not isinstance(evaluation, dict)
            or evaluation.get("fen") != board.fen(en_passant="fen")
            or evaluation.get("reliability") != "stable"
            or evaluation.get("bound") is not None
            or evaluation.get("source") != "search"
            or type(evaluation.get("depth")) is not int or evaluation["depth"] < 4
            or type(evaluation.get("cp")) is not int or evaluation.get("mate") is not None):
        return None
    return evaluation["cp"]


def _recommend(board, human_color, clock, complexity, evaluation):
    inactive = {"status": "inactive", "total_ms": None, "remaining_ms": None,
                "spent_ms": None, "reserve_ms": None, "urgency": "normal",
                "reason": "시간제 대국의 내 차례에 생각시간을 추천합니다.",
                "estimated_moves_left": None}
    if (not isinstance(clock, dict) or clock.get("enabled") is not True
            or clock.get("status") != "running"
            or clock.get("running_color") != _color_name(human_color)):
        return inactive
    current = _milliseconds(clock.get("white_ms" if human_color else "black_ms"))
    other = _milliseconds(clock.get("black_ms" if human_color else "white_ms"))
    elapsed = _milliseconds(clock.get("turn_elapsed_ms"))
    increment = _milliseconds(clock.get("increment_ms"))
    if None in (current, other, elapsed, increment):
        return inactive
    # For a fixed position, current + elapsed stays constant. Never use
    # current + a freshly chosen allowance as a rolling future deadline.
    at_start = current + elapsed
    f = complexity["features"]
    if board.fullmove_number <= 10 and f["nonpawn_pieces"] >= 10:
        moves_left = 36
    elif f["nonpawn_pieces"] >= 6:
        moves_left = 28
    else:
        moves_left = 24
    reserve = (min(500, at_start // 4) if at_start <= 10000
               else min(30000, max(1000, int(at_start * .08))))
    reserve = min(reserve, max(0, at_start - 1))
    spendable = max(0, at_start - reserve)
    base = spendable / moves_left + .70 * increment
    factor = .65 + complexity["score"] / 65
    if f["legal_choices"] == 1:
        factor = .25
    elif f["in_check"] or f["own_loose_pieces"]:
        factor += .12
    # This small adjustment uses the clock at turn start, never a ticking
    # current-time ratio, and cannot turn an easy position into a hard one.
    behind = at_start < other * .5 and at_start < 120000
    if behind:
        factor *= .85
    stable_cp = _stable_cp(board, evaluation)
    balanced = stable_cp is not None and abs(stable_cp) <= 150
    if balanced and f["legal_choices"] > 1:
        factor *= 1.08
    nominal = min(90000, max(1000, int(base * factor)))
    # One turn cannot consume more than a bounded fraction of the initial
    # clock, even if a fresh score increases the recommendation mid-turn.
    cap = max(1000, int(at_start * .18 + min(increment * .5, 5000)))
    total = min(nominal, cap, spendable)
    urgency = "normal"
    if at_start <= 10000:
        total = min(total, max(1, int(at_start * .12)), max(0, at_start - reserve))
    elif at_start <= 30000:
        total = min(total, max(500, int(at_start * .10)))
    # Halve the final per-turn allowance; elapsed thinking time stays real.
    total //= 2
    remaining = max(0, min(total - elapsed, current - reserve))
    if current <= 10000:
        urgency = "critical"
        remaining = min(remaining, max(0, int(current * .12)))
        reason = "시간이 매우 적습니다. 긴 계산보다 합법적이고 안전한 착수를 우선하세요."
    elif current <= 30000:
        urgency = "time_pressure"
        reason = "시간 압박 구간입니다. 후보를 좁히고 짧게 확인하세요."
    elif remaining == 0:
        urgency = "budget_used"
        reason = "이 수의 권장 생각시간을 썼습니다. 추가 숙고보다 착수를 우선하세요."
    elif f["legal_choices"] == 1:
        reason = "응수가 하나라 시간을 아끼고 다음 결정에 남겨 두세요."
    elif complexity["score"] >= 50:
        reason = "판단할 관계가 많아 평소보다 시간을 더 배분했습니다."
    else:
        reason = "남은 시간·증분·국면을 고려해 다음 수들에 쓸 시간을 남겼습니다."
    if behind and remaining > 0 and urgency == "normal":
        reason += " 상대보다 시간이 적어 권장량을 줄였습니다."
    return {"status": "ready", "total_ms": int(total), "remaining_ms": int(remaining),
            "spent_ms": elapsed, "reserve_ms": int(reserve), "urgency": urgency,
            "reason": reason, "estimated_moves_left": moves_left,
            "balanced_evaluation_used": balanced, "rating_used": False}


def assess(board, human_color, clock_snapshot, evaluation=None, rating=None):
    """Return non-mutating, JSON-safe guidance for the actual human turn.

    ``rating`` is reserved for future measured personalization. The current
    uncalibrated sparring estimate is deliberately not treated as known skill.
    Clock values are authoritative host milliseconds, not client wall times.
    """
    if not isinstance(board, chess.Board) or type(human_color) is not bool or not board.is_valid():
        return _inactive(board, human_color, "현재 배치에서는 판단 부담을 평가할 수 없습니다.")
    if board.is_game_over(claim_draw=False) or (isinstance(clock_snapshot, dict)
                                              and clock_snapshot.get("status") == "finished"):
        return _inactive(board, human_color, "대국이 끝났습니다.")
    if (isinstance(evaluation, dict) and evaluation.get("fen") == board.fen(en_passant="fen")
            and evaluation.get("status") == "terminal"):
        return _inactive(board, human_color, "대국이 끝났습니다.")
    if board.turn != human_color:
        return _inactive(board, human_color, "상대 차례입니다. 내 차례가 되면 갱신합니다.")
    legal = list(board.legal_moves)
    complexity = _complexity(_features(board, human_color, legal))
    return {"method": METHOD, "experimental": True, "status": "ready",
            "fen": board.fen(en_passant="fen"), "human_color": _color_name(human_color),
            "complexity": complexity,
            "recommendation": _recommend(board, human_color, clock_snapshot, complexity, evaluation),
            "note": NOTE}
