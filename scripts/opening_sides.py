"""Conservative roles within a recognized opening, not two invented openings.

The supplied catalogue match proves that its named position was reached. Only
explicit families/variants below assign that joint name to a side. Unmapped
responses describe moves actually played, without guessing a named defense.
"""
from __future__ import annotations

import chess
from opening_names import catalog_family_name, family_display_name


NOTE = "백의 전개와 흑의 대응을 나눠 표시합니다. 전체 변형명은 두 선수의 수순으로 함께 결정됩니다."

_PAWN_DEFENSES = {
    "Sicilian Defense", "French Defense", "Caro-Kann Defense", "Pirc Defense",
    "Alekhine Defense", "Scandinavian Defense", "Nimzowitsch Defense",
    "Petrov's Defense", "Philidor Defense", "Latvian Gambit", "Latvian Gambit Accepted",
    "Elephant Gambit", "Owen Defense", "St. George Defense",
}
_QUEEN_DEFENSES = {
    "King's Indian Defense", "Queen's Indian Defense", "Queen's Indian Defense, with e3",
    "Nimzo-Indian Defense", "Bogo-Indian Defense", "Grünfeld Defense", "Neo-Grünfeld Defense",
    "Indian Defense", "Old Indian Defense", "Dutch Defense", "Benoni Defense",
    "Benko Gambit", "Benko Gambit Accepted", "Benko Gambit Declined", "Englund Gambit",
    "Englund Gambit Declined", "English Defense", "Budapest Defense", "Blumenfeld Countergambit",
}
_QUEEN_GAMBIT_DEFENSES = {
    "Queen's Gambit Declined", "Queen's Gambit Accepted", "Slav Defense",
    "Semi-Slav Defense", "Tarrasch Defense", "Semi-Tarrasch Defense",
}
_WHITE_FAMILIES = {
    "Ruy Lopez", "Italian Game", "Scotch Game", "Vienna Game", "Vienna Gambit",
    "Four Knights Game", "Three Knights Opening", "Bishop's Opening", "Center Game",
    "Ponziani Opening", "Danish Gambit", "Réti Opening", "Zukertort Opening",
    "English Opening", "Catalan Opening", "King's Indian Attack", "Trompowsky Attack",
    "Torre Attack", "Richter-Veresov Attack", "Colle System", "London System",
    "Blackmar-Diemer Gambit", "Bird Opening", "Bird's Opening", "Polish Opening",
    "King's Fianchetto Opening", "Hungarian Opening", "Nimzowitsch-Larsen Attack",
    "Nimzo-Larsen Attack", "Van Geet Opening", "Grob Opening", "Ware Opening",
    "Sodium Attack", "Anderssen's Opening", "Van't Kruijs Opening", "Barnes Opening",
    "Mieses Opening", "Kádas Opening", "Saragossa Opening", "Amar Opening", "Portuguese Opening",
}

# Prefixes are explicit catalogue classifications, never arbitrary colon splits.
# These variants are White's confirmed central/setup choice within a defense.
_WHITE_VARIANTS = (
    ("Sicilian Defense: Alapin Variation", "Alapin Variation (Sicilian)"),
    ("Sicilian Defense: Closed", "Closed Sicilian"),
    ("Sicilian Defense: Grand Prix Attack", "Grand Prix Attack"),
    ("Sicilian Defense: Smith-Morra Gambit", "Smith-Morra Gambit"),
    ("Sicilian Defense: Wing Gambit", "Sicilian Wing Gambit"),
    ("Sicilian Defense: Bowdler Attack", "Bowdler Attack"),
    ("French Defense: Advance Variation", "Advance Variation (French)"),
    ("French Defense: Exchange Variation", "Exchange Variation (French)"),
    ("French Defense: Tarrasch Variation", "Tarrasch Variation (French)"),
    ("Caro-Kann Defense: Advance Variation", "Advance Variation (Caro-Kann)"),
    ("Caro-Kann Defense: Exchange Variation", "Exchange Variation (Caro-Kann)"),
    ("Caro-Kann Defense: Panov Attack", "Panov Attack"),
    ("Caro-Kann Defense: Two Knights Attack", "Two Knights Attack (Caro-Kann)"),
)
_BLACK_VARIANTS = (
    ("Ruy Lopez: Morphy Defense", "Morphy Defense"),
    ("Ruy Lopez: Berlin Defense", "Berlin Defense"),
    ("Ruy Lopez: Schliemann Defense", "Schliemann Defense"),
    ("Ruy Lopez: Steinitz Defense", "Steinitz Defense"),
    ("Ruy Lopez: Bird Variation", "Bird Variation"),
    ("Ruy Lopez: Classical Variation", "Classical Defense (Ruy Lopez)"),
    ("Italian Game: Giuoco Piano", "Giuoco Piano"),
    ("Italian Game: Two Knights Defense", "Two Knights Defense"),
    ("Italian Game: Hungarian Defense", "Hungarian Defense"),
    ("Italian Game: Paris Defense", "Paris Defense"),
    ("French Defense: Winawer Variation", "French Defense: Winawer Variation"),
    ("English Opening: Symmetrical Variation", "Symmetrical English"),
    ("English Opening: King's English Variation", "King's English Variation"),
    ("English Opening: Anglo-Indian Defense", "Anglo-Indian Defense"),
    ("English Opening: Anglo-Dutch Defense", "Anglo-Dutch Defense"),
    ("English Opening: Agincourt Defense", "Agincourt Defense"),
    ("Réti Opening: Kingside Fianchetto Variation", "Kingside Fianchetto Variation"),
    ("Réti Opening: Sicilian Invitation", "Sicilian Invitation"),
    ("Réti Opening: Dutch Variation", "Dutch Variation"),
    ("Réti Opening: Dutch", "Dutch Variation"),
    ("Réti Opening: Black Mustang Defense", "Black Mustang Defense"),
    ("Réti Opening: Slav Invitation", "Slav Invitation"),
)


def _starts(name, prefix):
    """A family/variant boundary, not a coincidental substring."""
    return name == prefix or name.startswith(prefix + ",") or name.startswith(prefix + ":")


def _row(name=None, status="pending", detail="아직 첫 수를 두지 않았습니다.", matched_ply=None):
    return {"name": name, "status": status, "detail": detail, "matched_ply": matched_ply}


def empty_sides(status="unavailable"):
    detail = "오프닝 자료를 사용할 수 없습니다." if status == "unavailable" else "이전 오프닝 수순을 알 수 없습니다."
    return {"white": _row(status=status, detail=detail),
            "black": _row(status=status, detail=detail), "note": NOTE}


def _roles(name, reference_moves):
    name = catalog_family_name(name)
    family = name.split(":", 1)[0]
    white = black = None
    if family in _PAWN_DEFENSES:
        white, black = "King's Pawn Opening", family
    elif family in _QUEEN_DEFENSES:
        white, black = "Queen's Pawn Opening", family
    elif family in _QUEEN_GAMBIT_DEFENSES:
        white, black = "Queen's Gambit", family
    elif family == "Queen's Gambit":
        white = "Queen's Gambit"
    elif family in ("King's Gambit Accepted", "King's Gambit Declined"):
        white, black = "King's Gambit", family
    elif family == "King's Gambit":
        white = "King's Gambit"
    elif family == "King's Pawn Game":
        white = "King's Pawn Opening"
    elif family == "Queen's Pawn Game":
        white = "Queen's Pawn Opening"
    elif family in _WHITE_FAMILIES:
        white = family
    elif family == "Modern Defense":
        # The Modern also occurs against d4/c4; never infer an e4 start.
        black = family
        white = ("King's Pawn Opening" if "e2e4" in reference_moves else
                 "Queen's Pawn Opening" if "d2d4" in reference_moves else None)

    for prefix, label in _WHITE_VARIANTS:
        if _starts(name, prefix):
            white = label
            break
    else:
        if family == "Sicilian Defense" and "d2d4" in reference_moves and "c5d4" in reference_moves:
            white = "Open Sicilian"

    for prefix, label in _BLACK_VARIANTS:
        if _starts(name, prefix):
            black = label
            break

    # These registered white systems can be nested under a broad black family.
    if any(_starts(name, prefix) for prefix in (
            "Queen's Pawn Game: London System", "Indian Defense: London System",
            "Queen's Pawn Game: Accelerated London System", "Indian Defense: Accelerated London System")):
        white = "London System"
    if name.startswith("Queen's Pawn Game: Colle System"):
        white = "Colle System"
    return (family_display_name(white)[0] if white else None,
            family_display_name(black)[0] if black else None)


def get_opening_sides(board, catalog, guide):
    """Describe the actual recognized position and the extent of its evidence."""
    standard = guide["history_scope"] == "standard"
    matched = guide.get("matched_opening")
    total = len(board.move_stack)
    result = {"white": _row(), "black": _row(detail="백의 첫 수를 기다리고 있습니다."), "note": NOTE}
    if not standard:
        result = empty_sides("unknown")
    if total and standard:
        result["white"] = _row(status="unknown", detail="백의 등록 오프닝 이름을 아직 확인하지 못했습니다.")
        result["black"] = _row(detail="흑의 첫 대응을 기다리고 있습니다.")

    # SAN comes only from the available played history, never an imagined line.
    replay = board.root()
    black_moves = []
    first_white = None
    for index, move in enumerate(board.move_stack, 1):
        san = replay.san(move)
        if standard and index == 1:
            first_white = move.uci()
        if not replay.turn and len(black_moves) < 3:
            black_moves.append("…" + san)
        replay.push(move)
    if black_moves:
        result["black"] = _row(status="pending" if standard else "unknown",
                               detail="확인한 대응: " + ", ".join(black_moves) + " · 세부 방어 미확정")
    if standard and first_white:
        first = next((item for item in catalog.openings if item.moves == (first_white,)), None)
        if first:
            white, _ = _roles(first.name, first.moves)
            result["white"] = _row(white or first.name, "confirmed", "백이 실제로 선택한 첫 수의 분류입니다.", 1)
    if not matched:
        return result

    # The current match's identity already follows exact-history-first ordering.
    # Recover that same reference line for structural variant labels only.
    at_match = board.copy(stack=True)
    while len(at_match.move_stack) > matched["ply"]:
        at_match.pop()
    history = tuple(move.uci() for move in at_match.move_stack)
    references = [item for item in catalog.openings
                  if item.eco == matched["eco"] and item.name == matched["name"]
                  and item.keys[-1] == " ".join(at_match.fen(en_passant="legal").split()[:4])]
    if not references:
        return result
    reference = min(references, key=lambda item: (item.moves != history, len(item.moves), item.moves))
    white, black = _roles(matched["name"], reference.moves)
    status = ("last_known" if matched["ply"] < total else
              "confirmed" if standard else "position")
    detail = ("마지막으로 확인한 오프닝 구상입니다. 현재 위치의 확정 이름은 아닙니다." if status == "last_known" else
              "현재 배치의 분류입니다. 이 배치에 이른 실제 오프닝 선택은 알 수 없습니다." if status == "position" else
              "다른 수순을 거쳐 이 오프닝 위치로 전환됐습니다." if matched["match_type"] == "transposition" else
              "실제로 둔 수순에서 확인했습니다.")
    for side, name in (("white", white), ("black", black)):
        if name:
            result[side] = _row(name, status, detail, matched["ply"])
    return result
