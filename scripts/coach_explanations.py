"""Concise local English coaching grounded in legal moves and short PVs."""
import chess

COACH_NAME = "Rook"
COACH_VERSION = 2
PIECE_NAMES = {chess.PAWN: "pawn", chess.KNIGHT: "knight", chess.BISHOP: "bishop",
               chess.ROOK: "rook", chess.QUEEN: "queen", chess.KING: "king"}
PIECE_POINTS = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9}
CRITICAL = {"inaccuracy", "mistake", "blunder", "missed_mate", "allows_mate"}


def _side(color):
    return "White" if color else "Black"


def _material(board, color):
    return sum(points * (len(board.pieces(piece, color)) - len(board.pieces(piece, not color)))
               for piece, points in PIECE_POINTS.items())


def _replay(board, branch, required=None):
    if not isinstance(branch, dict) or not isinstance(branch.get("pv_uci"), (list, tuple)) or not branch["pv_uci"]:
        return None
    if required is not None and branch["pv_uci"][0] != required:
        return None
    current = board.copy(stack=True)
    start_material, events = _material(current, board.turn), []
    for index, uci in enumerate(branch["pv_uci"], 1):
        try:
            move = chess.Move.from_uci(uci)
        except (ValueError, TypeError, AttributeError):
            return None
        if move not in current.legal_moves:
            return None
        color, piece = current.turn, current.piece_type_at(move.from_square)
        source, target = chess.square_name(move.from_square), chess.square_name(move.to_square)
        capture_square = move.to_square
        if current.is_en_passant(move):
            capture_square += -8 if color else 8
        captured = current.piece_type_at(capture_square)
        san, castle, was_check = current.san(move), current.is_castling(move), current.is_check()
        recapture = bool(captured and events and events[-1]["captured"]
                         and events[-1]["target"] == chess.square_name(capture_square))
        current.push(move)
        king = current.king(current.turn)
        events.append({"uci": uci, "san": san, "index": index, "color": color,
                       "source": source, "target": target, "piece": piece,
                       "captured": captured, "capture_square": chess.square_name(capture_square),
                       "recapture": recapture, "castle": castle, "promotion": move.promotion,
                       "was_check": was_check, "check": current.is_check(), "mate": current.is_checkmate(),
                       "king": chess.square_name(king) if king is not None else None})
    return {"events": events, "material_change": _material(current, board.turn) - start_material, "color": board.turn}


def _evidence(replay, line_name, max_ply=4, include_later_mate=False):
    if replay is None:
        return []
    facts = []
    for event in replay["events"]:
        if event["index"] > max_ply and not (include_later_mate and event["mate"]):
            continue
        actor, other, san = _side(event["color"]), _side(not event["color"]), event["san"]
        common = {"line": line_name, "ply_index": event["index"], "squares": [event["source"], event["target"]]}
        if event["captured"]:
            square, piece = event["capture_square"], PIECE_NAMES[event["captured"]]
            facts.append({**common, "kind": "capture", "squares": list(dict.fromkeys(common["squares"] + [square])),
                          "text": f"{actor}'s {san} captures {other}'s {piece} on {square} in this line."})
        if event["castle"]:
            rook_to = ("f" if event["target"][0] == "g" else "d") + event["target"][1]
            facts.append({**common, "kind": "castle", "text": f"{actor}'s {san}: king to {event['target']}, rook to {rook_to}."})
        if event["promotion"]:
            facts.append({**common, "kind": "promotion", "text": f"{actor}'s pawn promotes to a {PIECE_NAMES[event['promotion']]} on {event['target']}."})
        if event["mate"]:
            facts.append({**common, "kind": "checkmate", "squares": [event["target"], event["king"]],
                          "text": f"{san} checkmates {other} in this line; there is no legal reply."})
        elif event["check"]:
            facts.append({**common, "kind": "check", "squares": [event["target"], event["king"]],
                          "text": f"{actor}'s {san} checks {other}'s king."})
    difference = replay["material_change"]
    if difference and len(replay["events"]) <= max_ply:
        facts.append({"kind": "material", "line": line_name, "ply_index": len(replay["events"]), "squares": [],
                      "text": f"At this line's endpoint: {_side(replay['color'])}'s material balance changes by {difference:+d}, counting pieces only."})
    priority = {"checkmate": 0, "capture": 1, "promotion": 2, "castle": 3, "check": 4, "material": 5}
    return sorted(facts, key=lambda fact: (priority[fact["kind"]],
                  -PIECE_POINTS.get(replay["events"][fact["ply_index"] - 1]["captured"], 0) if fact["kind"] == "capture" else 0,
                  fact["ply_index"]))[:4]


def _develops(event):
    homes = {chess.KNIGHT: ("b1", "g1", "b8", "g8"), chess.BISHOP: ("c1", "f1", "c8", "f8")}
    return event["source"] in homes.get(event["piece"], ()) and event["target"][1] != event["source"][1]


def _actual_description(event):
    actor, other, san = _side(event["color"]), _side(not event["color"]), event["san"]
    if event["mate"]:
        return f"{actor}'s {san} is checkmate."
    if event["promotion"]:
        capture = f"takes {other}'s {PIECE_NAMES[event['captured']]} and " if event["captured"] else ""
        return f"{actor}'s {san} {capture}promotes to a {PIECE_NAMES[event['promotion']]}."
    if event["castle"]:
        rook_to = ("f" if event["target"][0] == "g" else "d") + event["target"][1]
        return f"{actor}'s {san} moves the king to {event['target']} and rook to {rook_to}."
    if event["captured"]:
        return f"{actor}'s {san} takes {other}'s {PIECE_NAMES[event['captured']]} on {event['capture_square']}."
    if event["was_check"]:
        return f"{actor}'s {san} gets out of check."
    if event["check"]:
        return f"{actor}'s {san} checks {other}'s king."
    if _develops(event):
        return f"{actor} develops the {PIECE_NAMES[event['piece']]} with {san}."
    if event["piece"] == chess.PAWN and event["target"] in ("d4", "e4", "d5", "e5"):
        return f"{actor} puts a pawn in the center with {san}."
    return f"{actor} repositions the {PIECE_NAMES[event['piece']]} with {san}."


def _alternative(actual, best):
    if best is None or best["uci"] == actual["uci"]:
        return "Check that reply before committing."
    return f"{_side(actual['color'])} should consider {best['san']} instead."


def _dialogue(kind, actual, played, best):
    if actual is None:
        return "This move could not be verified from the position."
    actor, other, san = _side(actual["color"]), _side(not actual["color"]), actual["san"]
    if kind == "uncertain":
        return f"The verdict on {actor}'s {san} is unclear; the replies need a closer look."
    if played is None:
        return _actual_description(actual) + " The continuation is unverified."
    events, best_event = played["events"], best["events"][0] if best else None
    if kind == "missed_mate" and best:
        mate = next((event for event in best["events"] if event["mate"] and event["color"] == actual["color"]), None)
        if mate:
            if mate["index"] == 1:
                return f"{actor} had {mate['san']}, checkmate immediately. {san} missed that finish."
            return f"{actor}'s {best_event['san']} reaches {mate['san']} in the shown line, checkmating {other}. That attack was worth pursuing."
    if kind == "allows_mate":
        mate = next((event for event in events if event["mate"] and event["color"] != actual["color"]), None)
        if mate:
            lead = (f"After {actor}'s {san}, {other} can finish with {mate['san']}." if mate["index"] == 2 else
                    f"{actor}'s {san} reaches {mate['san']} in the shown line, checkmating {actor}.")
        else:
            lead = f"{actor}'s {san} allows a mating threat in the analysis."
        return lead + " " + _alternative(actual, best_event)
    if actual["mate"]:
        return _actual_description(actual) + f" {other} has no legal reply."
    if kind in CRITICAL:
        # A near response is relevant; a remote PV event is not a reason for
        # this move's grade. Captures remain conditional, not forced net losses.
        captures = [event for event in events[1:4] if event["color"] != actual["color"] and event["captured"]]
        if captures:
            event = max(captures, key=lambda event: (PIECE_POINTS.get(event["captured"], 0), -event["index"]))
            piece = PIECE_NAMES[event["captured"]]
            if event["index"] == 2:
                verb = "recapture" if event["recapture"] else "take"
                lead = f"After {actor}'s {san}, {other} can {verb} the {piece} with {event['san']}."
            else:
                line = " ".join(step["san"] for step in events[1:event["index"]])
                lead = f"After {actor}'s {san}, the line {line} captures {actor}'s {piece}."
            return lead + " " + _alternative(actual, best_event)
        if best_event and best_event["uci"] != actual["uci"]:
            if actual["target"] == best_event["target"] and actual["piece"] != best_event["piece"]:
                return (f"{actor}'s {san} puts a {PIECE_NAMES[actual['piece']]} on {actual['target']}. "
                        f"{best_event['san']} would use that square for the {PIECE_NAMES[best_event['piece']]} instead.")
            return f"{actor} had a stronger option in {best_event['san']}; check its immediate reply against the one after {san}."
    if len(events) > 1 and events[1]["recapture"]:
        reply = events[1]
        taken, given = PIECE_NAMES[actual["captured"]], PIECE_NAMES[reply["captured"]]
        return f"{actor}'s {san} takes a {taken}, but {other} can recapture a {given} with {reply['san']}. Count both sides of the exchange."
    lead = _actual_description(actual)
    if len(events) > 1:
        reply = events[1]
        if reply["mate"]:
            return lead + f" {other} can reply {reply['san']}, checkmating {actor}."
        if reply["captured"]:
            return lead + f" {other} can answer {reply['san']}, taking {actor}'s {PIECE_NAMES[reply['captured']]}."
        return lead + f" Watch for {other}'s {reply['san']} in response."
    return lead + " The reply still needs checking."


def _hint(board, kind, actual, played, best):
    actor, other = _side(board.turn), _side(not board.turn)
    if kind == "allows_mate":
        return f"Check the threats against {actor}'s king before choosing a move."
    if kind == "missed_mate":
        return f"Look for checks against {other}'s king, then test every escape."
    if board.is_check():
        return f"Get {actor}'s king out of check, then test the reply."
    if kind == "uncertain":
        return "Choose a candidate and check the immediate reply before trusting it."
    events = played["events"] if played else []
    if any(event["color"] != board.turn and event["captured"] for event in events[1:4]):
        return "Look for checks and captures the other side gets after your candidate."
    best_event = best["events"][0] if best else None
    if any(event and event["captured"] for event in (actual, best_event)):
        return "Count what each side can take, including the first recapture."
    if any(event and event["check"] for event in (actual, best_event)):
        return "Check the king's escapes, captures and blocks against each checking move."
    if any(event and _develops(event) for event in (actual, best_event)):
        return "Consider bringing a back-rank piece into play, then test the reply."
    return "Identify the immediate threat, then test one reply to your candidate."


def describe_move(board, row):
    """Stored SAN/color are not tactical evidence; replay them from the board."""
    kind = row.get("classification", "pending")
    if kind == "pending":
        return None
    actual_line = _replay(board, {"pv_uci": [row.get("uci")]})
    actual = actual_line["events"][0] if actual_line else None
    played = _replay(board, row.get("played"), required=row.get("uci")) if actual else None
    best = _replay(board, row.get("best"))
    window = 2 if kind in ("best", "good") else 4
    show_mate = kind in ("allows_mate", "missed_mate")
    played_facts = _evidence(played, "played", window, show_mate)
    best_facts = _evidence(best, "best", window, show_mate)
    if played and best and row["played"]["pv_uci"] == row["best"]["pv_uci"]:
        best_facts = []
    facts = best_facts + played_facts if kind == "missed_mate" else played_facts + best_facts
    mood = {"best": "celebrate", "good": "encourage", "allows_mate": "warning"}.get(kind, "think")
    if actual is None or played is None:
        mood = "think"
    focus_event = best["events"][0] if kind == "missed_mate" and best else actual
    focus = [focus_event["source"], focus_event["target"]] if focus_event else []
    if focus_event and focus_event["check"] and focus_event["king"]:
        focus.append(focus_event["king"])
    if kind in CRITICAL and played:
        captures = [event for event in played["events"][1:4] if event["color"] != board.turn and event["captured"]]
        if captures:
            focus.append(max(captures, key=lambda event: PIECE_POINTS.get(event["captured"], 0))["capture_square"])
    return {"mood": mood, "dialogue": _dialogue(kind, actual, played, best),
            "hint": _hint(board, kind, actual, played, best), "evidence": facts, "focus_squares": list(dict.fromkeys(focus))}


def describe_report(rows, critical_plies):
    ready = [row for row in rows if row.get("classification") != "pending"]
    available = {row["ply"]: row for row in ready}
    positives = [row["ply"] for row in ready if row.get("classification") in ("best", "good")]
    important = [ply for ply in critical_plies if ply in available]
    limit, chosen = (4 if positives else 5), []
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
        summary = "I'll select the turning points and a good move to revisit as the analysis comes in."
    elif lessons:
        counts = {color: sum(row.get("classification") in CRITICAL and
                            row.get("color", "white" if row["ply"] % 2 else "black") == color for row in ready)
                  for color in ("white", "black")}
        prefix = "So far: " if len(ready) < len(rows) else ""
        noun = "move" if counts["white"] == 1 else "moves"
        summary = prefix + f"White has {counts['white']} {noun} to revisit; Black has {counts['black']}. "
        if any(row.get("classification") == "allows_mate" for row in ready):
            summary += "Start with the mating threats. "
        elif any(row.get("classification") == "missed_mate" for row in ready):
            summary += "Start with the missed mating chances. "
        else:
            summary += "Watch the immediate replies. "
        summary += f"The {len(lessons)} selected scenes" + (" include a good move, too." if positives and important else " are ready to review.")
    else:
        summary = "There isn't a clear lesson yet; the inconclusive positions need a closer look."
    if len(ready) < len(rows) and not lessons:
        summary += " Unanalyzed moves are excluded."
    return {"name": COACH_NAME, "summary": summary, "lesson_plies": lessons, "version": COACH_VERSION}
