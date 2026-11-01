"""Small, local coach dialogue grounded in legally replayed engine variations.

These templates describe observable events, not the engine's private reasoning.
No attack-map guess is presented as a proven fork, pin or inevitable loss.
"""

import chess


COACH_NAME = "루크"
COACH_VERSION = 1
PIECE_NAMES = {chess.PAWN: "폰", chess.KNIGHT: "나이트", chess.BISHOP: "비숍",
               chess.ROOK: "룩", chess.QUEEN: "퀸", chess.KING: "왕"}
PIECE_POINTS = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9}
CRITICAL = {"inaccuracy", "mistake", "blunder", "missed_mate", "allows_mate"}


def _material(board, color):
    return sum(points * (len(board.pieces(piece, color)) - len(board.pieces(piece, not color)))
               for piece, points in PIECE_POINTS.items())


def _evidence(board, branch, line_name):
    if not isinstance(branch, dict) or not branch.get("pv_uci"):
        return []
    current = board.copy(stack=True)
    start_material = _material(current, board.turn)
    facts = []
    for index, uci in enumerate(branch["pv_uci"], 1):
        try:
            move = chess.Move.from_uci(uci)
        except (ValueError, TypeError):
            return []
        if move not in current.legal_moves:
            return []
        actor = "백" if current.turn else "흑"
        san = current.san(move)
        source, target = chess.square_name(move.from_square), chess.square_name(move.to_square)
        capture_square = move.to_square
        if current.is_en_passant(move):
            capture_square += -8 if current.turn else 8
        captured = current.piece_at(capture_square)
        castle = current.is_castling(move)
        current.push(move)
        common = {"line": line_name, "ply_index": index, "squares": [source, target]}
        if captured is not None:
            square = chess.square_name(capture_square)
            facts.append({**common, "kind": "capture", "squares": list(dict.fromkeys([source, target, square])),
                          "text": f"이 예상 수순에서는 {actor}의 {san}으로 {square}의 {PIECE_NAMES[captured.piece_type]}을 잡습니다."})
        if castle:
            facts.append({**common, "kind": "castle", "text": f"이 예상 수순에서는 {actor}이 {san}으로 왕과 룩을 함께 옮깁니다."})
        if move.promotion:
            facts.append({**common, "kind": "promotion", "text": f"이 예상 수순에서는 {actor}의 폰이 {target}에서 {PIECE_NAMES[move.promotion]}으로 승격합니다."})
        if current.is_checkmate():
            king = current.king(current.turn)
            squares = [target] + ([chess.square_name(king)] if king is not None else [])
            facts.append({**common, "kind": "checkmate", "squares": squares,
                          "text": f"이 예상 수순에서는 {san} 뒤 {'백' if current.turn else '흑'} 왕이 체크메이트입니다. 합법적인 응수가 없습니다."})
        elif current.is_check():
            king = current.king(current.turn)
            squares = [target] + ([chess.square_name(king)] if king is not None else [])
            facts.append({**common, "kind": "check", "squares": squares,
                          "text": f"이 예상 수순에서는 {san}이 {'백' if current.turn else '흑'} 왕에게 체크를 겁니다."})
    difference = _material(current, board.turn) - start_material
    if difference:
        actor = "백" if board.turn else "흑"
        facts.append({"kind": "material", "line": line_name, "ply_index": len(branch["pv_uci"]), "squares": [],
                      "text": f"이 예상 수순에서는 표시한 {len(branch['pv_uci'])}반수의 끝까지 {actor}의 기물 점수 차이가 {difference:+d}만큼 변합니다. "
                              "폰 1·나이트/비숍 3·룩 5·퀸 9로 센 값이며, 엔진 평가나 그 뒤의 결과를 뜻하지 않습니다."})
    priority = {"checkmate": 0, "material": 1, "capture": 2, "promotion": 3, "castle": 4, "check": 5}
    return sorted(facts, key=lambda fact: (priority[fact["kind"]], fact["ply_index"]))[:4]


def describe_move(board, row):
    """Return a coach packet, never trusting SAN strings as tactical evidence."""
    kind = row.get("classification", "pending")
    if kind == "pending":
        return None
    played_facts = _evidence(board, row.get("played"), "played")
    best_facts = _evidence(board, row.get("best"), "best")
    # Identical legally replayed lines have identical evidence. Preserve the
    # actual-move provenance instead of showing the same observation twice.
    if (played_facts and isinstance(row.get("best"), dict) and
            row["played"]["pv_uci"] == row["best"].get("pv_uci")):
        best_facts = []
    facts = best_facts + played_facts if kind == "missed_mate" else played_facts + best_facts
    if kind == "uncertain":
        mood, dialogue = "think", "이 국면은 아직 단정하기 어려워요. 탐색 결과가 충분하지 않거나 서로 맞지 않아서 판단을 보류했어요. 더 긴 분석으로 다시 확인해 볼까요?"
    elif kind == "best":
        mood, dialogue = "celebrate", "좋아요. 실제로 둔 수가 이번 탐색에서 찾은 최선 후보와 같아요. 왜 유지할 만한 수인지 응수까지 함께 살펴봐요."
    elif kind == "good":
        mood, dialogue = "encourage", "좋은 대안도 여러 개일 수 있어요. 이 수는 이번 탐색에서 최선 후보와 비슷한 평가를 받았어요. 이어지는 상대의 응수를 확인해 봐요."
    elif kind == "allows_mate":
        mood, dialogue = "warning", "여기서는 수를 둔 쪽 왕의 안전을 먼저 살펴볼게요. 실제 수 뒤에 상대의 메이트 가능성이 발견됐어요. 상대의 응수와 이를 피할 대안을 확인해 봐요."
    elif kind == "missed_mate":
        mood, dialogue = "think", "이번에는 상대 왕을 공격할 기회에 주목해요. 최선 후보에서는 메이트 가능성을 찾았지만 실제 수에서는 그 기회를 확인하지 못했어요. 공격을 이어가는 방법을 비교해 봐요."
    else:
        mood, dialogue = "think", "여기는 잠깐 멈춰볼게요. 실제로 둔 수와 최선 후보 사이에 평가 차이가 있어요. 상대의 응수를 예상하고 대안을 다시 찾아봐요."
    # One concrete, scoped observation is more useful than unverified prose.
    observations = best_facts if kind == "missed_mate" else played_facts
    if kind != "uncertain" and observations:
        dialogue += " " + observations[0]["text"]
    kinds = {fact["kind"] for fact in facts}
    if kind == "allows_mate":
        hint = "상대가 왕에게 걸 수 있는 체크부터 찾아보세요. 그 위협을 막거나 피하면서 상대의 다음 응수에도 대응할 수 있는지 확인해 보세요."
    elif kind == "missed_mate":
        hint = "상대 왕에게 걸 수 있는 체크를 살펴보세요. 체크 뒤 왕이 도망가거나 막거나 잡을 수 있는지도 하나씩 확인해 보세요."
    elif "checkmate" in kinds or "check" in kinds:
        hint = "먼저 양쪽 왕에게 걸 수 있는 체크와 막아야 할 위협을 살펴보세요. 내 수 뒤 상대가 어떻게 응수할지도 생각해 보세요."
    elif "capture" in kinds or "material" in kinds:
        hint = "잡는 수가 보인다면 상대가 되잡는 수까지 세어 보세요. 교환이 끝난 뒤 남는 기물도 비교해 보세요."
    else:
        hint = "상대가 바로 위협하는 것이 무엇인지 살펴보세요. 후보를 정한 뒤 상대의 다음 응수까지 생각해 보세요."
    # Hints deliberately omit solution SAN/UCI/destination squares. Clients
    # hide evidence highlights as well while asking the player to retry.
    focus = list(dict.fromkeys(square for fact in observations for square in fact["squares"]))[:8]
    return {"mood": mood, "dialogue": dialogue, "hint": hint, "evidence": facts, "focus_squares": focus}


def describe_report(rows, critical_plies):
    ready = [row for row in rows if row.get("classification") != "pending"]
    available = {row["ply"]: row for row in ready}
    positives = [row["ply"] for row in ready if row.get("classification") in ("best", "good")]
    important = [ply for ply in critical_plies if ply in available]
    limit = 4 if positives else 5
    chosen = []
    # Prefer separated moments before filling gaps with adjacent mistakes.
    # This preserves severity ordering within each pass without presenting a
    # single tactical sequence as an entire five-lesson review.
    for ply in important:
        if all(abs(ply - previous) >= 3 for previous in chosen):
            chosen.append(ply)
            if len(chosen) == limit:
                break
    for ply in important:
        if len(chosen) == limit:
            break
        if ply not in chosen:
            chosen.append(ply)
    if positives:
        positive = max(positives, key=lambda ply: min(abs(ply - value) for value in chosen)) if chosen else positives[0]
        chosen.append(positive)
    lessons = sorted(set(chosen))[:5]
    if not ready:
        summary = "안녕하세요, 루크예요. 수순을 확인하고 있어요. 분석된 국면부터 실수와 잘 둔 수를 함께 돌아볼게요."
    elif lessons:
        counts = {color: sum(row.get("classification") in CRITICAL and
                            row.get("color", "white" if row["ply"] % 2 else "black") == color for row in ready)
                  for color in ("white", "black")}
        allows = sum(row.get("classification") == "allows_mate" for row in ready)
        missed = sum(row.get("classification") == "missed_mate" for row in ready)
        prefix = "아직 분석 중인 수를 제외하면 " if len(ready) < len(rows) else ""
        summary = prefix + f"다시 볼 수는 백 {counts['white']}개, 흑 {counts['black']}개예요. "
        if allows:
            summary += f"특히 메이트 허용으로 평가된 {allows}개 국면의 왕 안전부터 확인해 봐요. "
        elif missed:
            summary += f"메이트 기회를 놓친 것으로 평가된 {missed}개 국면에서는 상대 왕을 공격하는 방법을 살펴봐요. "
        elif important:
            summary += "평가가 달라진 국면에서 상대의 응수를 한 수씩 확인해 봐요. "
        else:
            summary += "이번 탐색에서 좋게 평가된 수의 응수도 살펴봐요. "
        summary += f"골라 둔 {len(lessons)}개 국면" + ("에는 잘 둔 수도 함께 넣었어요." if positives and important else "을 차례로 돌아봐요.")
    else:
        summary = "루크예요. 아직 확실하게 골라 설명할 국면이 없어요. 판단 보류인 수는 더 긴 분석으로 확인할 수 있어요."
    if len(ready) < len(rows) and not lessons:
        summary += " 아직 분석 중인 수는 수업 목록에 넣지 않았어요."
    return {"name": COACH_NAME, "summary": summary, "lesson_plies": lessons, "version": COACH_VERSION}
