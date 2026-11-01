"""Small, verifiable chess plans from legal Regicide lines.

Geometry identifies ideas, not move strength. A single PV supplies conditional
examples, never a proof that a threat wins material or the opponent must comply.
No search, external model, mutable session state, or private game data is used.
"""
import chess


CRITICAL = {"inaccuracy", "mistake", "blunder", "missed_mate", "allows_mate"}
NAMES = {chess.PAWN: "pawn", chess.KNIGHT: "knight", chess.BISHOP: "bishop",
         chess.ROOK: "rook", chess.QUEEN: "queen", chess.KING: "king"}
VALUES = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5,
          chess.QUEEN: 9, chess.KING: 100}
PRINCIPLES = {
    "fork": "Check whether the opponent can remove the attacker before counting both threats as gains.",
    "absolute_pin": "A pin may allow captures or moves along its line; check their legality.",
    "discovered_attack": "When moving a blocker, look at the line it opens behind it.",
    "rook_file": "An open file becomes useful when the rook can reach a concrete target.",
    "passed_pawn": "Advance passed pawns with support, and check the square directly in front of them.",
    "outpost": "A useful outpost combines pawn support with resistance to enemy pawn advances.",
    "king_activity": "With queens off, improve the king while still checking the opponent's forcing moves.",
    "development": "Develop toward a useful central square or a concrete target.",
    "pawn_lever": "A pawn lever changes the position only after you account for captures and pawn advances.",
}
TITLES = {
    "fork": "One piece, two threats", "absolute_pin": "Keep a piece tied to its king",
    "discovered_attack": "Open the line behind the blocker", "rook_file": "Give the rook a route in",
    "passed_pawn": "Advance a supported passed pawn", "outpost": "Use a pawn-supported outpost",
    "king_activity": "Bring the king into the ending", "development": "Develop with a purpose",
    "pawn_lever": "Challenge the pawn structure",
}


def _side(color):
    return "White" if color else "Black"


def _owner(color, perspective):
    return "your" if color == perspective else _side(color) + "'s"


def _perspective(value):
    if value in ("white", "black"):
        return value == "white"
    return value if isinstance(value, bool) else None


def _squares(*values):
    return list(dict.fromkeys(chess.square_name(value) if isinstance(value, int) else value
                             for value in values if value is not None))


def _line(board, branch, required):
    if (not isinstance(branch, dict) or branch.get("bound") != "exact"
            or not isinstance(branch.get("pv_uci"), (list, tuple)) or not branch["pv_uci"]
            or branch.get("uci") != required or branch["pv_uci"][0] != required):
        return None
    frames = branch.get("pv_frames")
    if frames is not None and (not isinstance(frames, list) or len(frames) != len(branch["pv_uci"]) + 1):
        return None
    position, events = board.copy(stack=True), []
    if frames is not None and (not isinstance(frames[0], dict) or frames[0].get("fen") != position.fen(en_passant="fen")):
        return None
    for index, uci in enumerate(branch["pv_uci"], 1):
        try:
            move = chess.Move.from_uci(uci)
        except (TypeError, ValueError, AttributeError):
            return None
        if position.is_game_over(claim_draw=False) or move not in position.legal_moves:
            return None
        before = position.copy(stack=True)
        capture_square = move.to_square + ((-8 if position.turn else 8) if position.is_en_passant(move) else 0)
        event = {"move": move, "uci": uci, "san": position.san(move), "ply_index": index,
                 "color": position.turn, "piece": position.piece_type_at(move.from_square),
                 "captured": position.piece_type_at(capture_square), "capture_square": capture_square,
                 "before": before, "source": move.from_square, "target": move.to_square}
        position.push(move)
        event["after"] = position.copy(stack=True)
        events.append(event)
        if frames is not None and (not isinstance(frames[index], dict)
                                   or frames[index].get("fen") != position.fen(en_passant="fen")):
            return None
    return events


def _targets(board, source, color, minimum=1):
    """Attacks that can legally capture, plus verified direct checks.

    Giving the mover the next turn is a local legality test, not a prediction
    that the opponent will pass. It excludes pinned attackers and king suicide.
    """
    current = board.copy(stack=True)
    current.turn = color
    current.ep_square = None
    result = []
    for target in board.attacks(source):
        piece = board.piece_at(target)
        if piece is None or piece.color == color or VALUES[piece.piece_type] < minimum:
            continue
        if piece.piece_type == chess.KING:
            legal = source in board.checkers() and board.turn != color
        else:
            promotion = chess.QUEEN if board.piece_type_at(source) == chess.PAWN and chess.square_rank(target) in (0, 7) else None
            legal = chess.Move(source, target, promotion=promotion) in current.legal_moves
        if legal:
            result.append({"square": target, "piece": piece.piece_type})
    return sorted(result, key=lambda item: (-VALUES[item["piece"]], item["square"]))


def _supporters(board, target, color):
    """Can this friendly piece recapture an enemy on the occupied target?"""
    current = board.copy(stack=True)
    current.set_piece_at(target, chess.Piece(chess.PAWN, not color))
    current.turn, current.ep_square = color, None
    return [{"square": source, "piece": board.piece_type_at(source)}
            for source in board.attackers(color, target)
            if chess.Move(source, target) in current.legal_moves]


def _passed(board, square, color):
    file, rank = chess.square_file(square), chess.square_rank(square)
    return not any(abs(chess.square_file(pawn) - file) <= 1 and
                   (chess.square_rank(pawn) > rank if color else chess.square_rank(pawn) < rank)
                   for pawn in board.pieces(chess.PAWN, not color))


def _center_distance(square):
    return min(abs(chess.square_file(square) - chess.square_file(center)) +
               abs(chess.square_rank(square) - chess.square_rank(center))
               for center in (chess.D4, chess.E4, chess.D5, chess.E5))


def _motif(events):
    first = events[0]
    before, after, source, target, color = (first[key] for key in ("before", "after", "source", "target", "color"))
    piece = after.piece_type_at(target)
    targets = _targets(after, target, color)
    valuable = [item for item in targets if VALUES[item["piece"]] >= 3]
    old = {item["square"] for item in _targets(before, source, color, 3)}
    if len(valuable) >= 2 and any(item["square"] not in old for item in valuable):
        return "fork", {"targets": valuable[:2]}
    if piece in (chess.BISHOP, chess.ROOK, chess.QUEEN):
        for victim in targets:
            square = victim["square"]
            if victim["piece"] != chess.KING and after.is_pinned(not color, square) and target in after.pin(not color, square):
                return "absolute_pin", {"victim": victim, "king": after.king(not color)}
    for attacker in after.piece_map():
        if attacker == target or after.color_at(attacker) != color or after.piece_type_at(attacker) not in (chess.BISHOP, chess.ROOK, chess.QUEEN):
            continue
        if before.piece_at(attacker) != after.piece_at(attacker):
            continue
        for victim in _targets(after, attacker, color, 3):
            square = victim["square"]
            if (before.piece_at(square) == after.piece_at(square) and square not in before.attacks(attacker)
                    and source in chess.SquareSet(chess.between(attacker, square))):
                return "discovered_attack", {"attacker": attacker, "victim": victim}
    if first["piece"] == chess.PAWN and not first["move"].promotion:
        support = _supporters(after, target, color)
        if _passed(after, target, color) and support:
            return "passed_pawn", {"support": min(support, key=lambda item: (item["piece"] != chess.PAWN, item["square"]))}
        pawns = [item for item in targets if item["piece"] == chess.PAWN]
        if pawns:
            return "pawn_lever", {"victim": pawns[0]}
    if piece in (chess.KNIGHT, chess.BISHOP) and 2 <= chess.square_file(target) <= 5:
        rank = chess.square_rank(target)
        support = [item for item in _supporters(after, target, color) if item["piece"] == chess.PAWN]
        challenged = any(abs(chess.square_file(pawn) - chess.square_file(target)) == 1 and
                         (chess.square_rank(pawn) > rank if color else chess.square_rank(pawn) < rank)
                         for pawn in after.pieces(chess.PAWN, not color))
        if support and (rank >= 3 if color else rank <= 4) and not challenged:
            return "outpost", {"support": support[0]}
    if piece == chess.ROOK:
        file = chess.square_file(target)
        own_pawns = [pawn for pawn in after.pieces(chess.PAWN, color) if chess.square_file(pawn) == file]
        enemy_pawns = [pawn for pawn in after.pieces(chess.PAWN, not color) if chess.square_file(pawn) == file]
        advanced = chess.square_rank(target) > chess.square_rank(source) if color else chess.square_rank(target) < chess.square_rank(source)
        if not own_pawns and (chess.square_file(source) != file or advanced):
            return "rook_file", {"file": chess.FILE_NAMES[file], "open": not enemy_pawns}
    # Queen trades alone do not turn a full-army middlegame into an ending.
    nonpawn_material = sum(VALUES[kind] * len(after.pieces(kind, side))
                           for kind in (chess.KNIGHT, chess.BISHOP, chess.ROOK)
                           for side in (chess.WHITE, chess.BLACK))
    if (piece == chess.KING and not before.is_check() and not after.queens and nonpawn_material <= 14
            and _center_distance(target) < _center_distance(source)):
        return "king_activity", {}
    homes = {chess.KNIGHT: (chess.B1, chess.G1, chess.B8, chess.G8),
             chess.BISHOP: (chess.C1, chess.F1, chess.C8, chess.F8)}
    if (source in homes.get(first["piece"], ()) and chess.square_rank(source) == (0 if color else 7)
            and chess.square_rank(target) != chess.square_rank(source)):
        central = [square for square in (chess.D4, chess.E4, chess.D5, chess.E5)
                   if square in after.attacks(target) and square in after.pin(color, target)]
        if targets or central:
            return "development", {"victim": targets[0] if targets else None, "central": central}
    return None, None


def _idea(theme, facts, first, perspective):
    color, target = first["color"], first["target"]
    own, other = _owner(color, perspective), _owner(not color, perspective)
    piece = NAMES[first["after"].piece_type_at(target)]
    square = chess.square_name(target)
    focus = _squares(first["source"], target)
    if theme == "fork":
        a, b = facts["targets"]
        objects = [f"{NAMES[item['piece']]} on {chess.square_name(item['square'])}" for item in (a, b)]
        text = f"{own.capitalize() if own == 'your' else own} {piece} attacks the {objects[0]} and the {objects[1]} at once."
        detail = "Both targets are attacked, but the opponent may capture the attacker or create a stronger threat."
        focus += _squares(a["square"], b["square"])
    elif theme == "absolute_pin":
        victim = facts["victim"]
        text = f"The {piece} pins {other} {NAMES[victim['piece']]} on {chess.square_name(victim['square'])} to the king."
        detail = "Moving that piece off the pin line would expose check; moves along the line may still be legal."
        focus += _squares(victim["square"], facts["king"])
    elif theme == "discovered_attack":
        victim = facts["victim"]
        attacker = NAMES[first["after"].piece_type_at(facts["attacker"])]
        text = f"Moving the {NAMES[first['piece']]} uncovers {own} {attacker}'s attack on the {NAMES[victim['piece']]} on {chess.square_name(victim['square'])}."
        detail = "The threat comes from the piece behind the blocker, so moving the blocker and moving the target both matter."
        focus += _squares(facts["attacker"], victim["square"])
    elif theme == "rook_file":
        kind = "open" if facts["open"] else "semi-open"
        text = f"{own.capitalize() if own == 'your' else own} rook reaches the {kind} {facts['file']}-file."
        detail = ("There are no pawns on this file; other pieces can still block entry squares." if facts["open"] else
                  "No friendly pawn blocks this file, but an enemy pawn still occupies it.")
    elif theme == "passed_pawn":
        support = facts["support"]
        text = f"The passed pawn reaches {square}, supported by {own} {NAMES[support['piece']]} on {chess.square_name(support['square'])}."
        detail = "No enemy pawn is ahead on its file or a neighboring file, but pieces can still blockade it."
        focus += _squares(support["square"])
    elif theme == "outpost":
        support = facts["support"]
        text = f"{own.capitalize() if own == 'your' else own} {piece} on {square} has pawn support, with no enemy pawn advance available from an adjacent file."
        detail = "That makes the square a useful base in this pawn structure; enemy pieces can still challenge it."
        focus += _squares(support["square"])
    elif theme == "king_activity":
        text = f"{own.capitalize() if own == 'your' else own} king steps closer to the center on {square}, with queens off the board."
        detail = "The king can help in the ending, but checks from the remaining pieces still matter."
    elif theme == "pawn_lever":
        victim = facts["victim"]
        text = f"The pawn on {square} challenges {other} pawn on {chess.square_name(victim['square'])}."
        detail = "The resulting structure depends on whether the opponent captures, advances, or keeps the tension."
        focus += _squares(victim["square"])
    else:
        victim = facts["victim"]
        goal = (f"attacks the {NAMES[victim['piece']]} on {chess.square_name(victim['square'])}" if victim else
                "influences " + " and ".join(chess.square_name(square) for square in facts["central"][:2]))
        text = f"{own.capitalize() if own == 'your' else own} {piece} develops to {square} and {goal}."
        detail = "The move puts an undeveloped piece to work with a specific target, rather than just changing squares."
        focus += _squares(victim["square"]) if victim else _squares(*facts["central"][:2])
    return text, detail, list(dict.fromkeys(focus))


def _effect(event, perspective):
    own, other = _owner(event["color"], perspective), _owner(not event["color"], perspective)
    piece = NAMES[event["move"].promotion or event["piece"]]
    target = chess.square_name(event["target"])
    if event["after"].is_checkmate():
        return f"delivering checkmate in the shown line"
    if event["move"].promotion:
        return f"promoting the pawn to a {piece}"
    if event["captured"]:
        return f"capturing {other} {NAMES[event['captured']]} on {chess.square_name(event['capture_square'])}"
    if event["after"].is_check():
        return "giving check"
    targets = _targets(event["after"], event["target"], event["color"])
    if targets:
        victim = targets[0]
        return f"attacking the {NAMES[victim['piece']]} on {chess.square_name(victim['square'])}"
    rank = chess.square_rank(event["target"])
    if (event["piece"] == chess.ROOK and chess.square_rank(event["source"]) != rank
            and rank == (6 if event["color"] else 1)):
        return f"bringing the rook onto the {'seventh' if event['color'] else 'second'} rank"
    if event["piece"] == chess.PAWN and _passed(event["after"], event["target"], event["color"]):
        return f"advancing the passed pawn to {target}"
    return f"bringing {own} {piece} to {target}"


def _followup(events):
    first, tracked = events[0], events[0]["target"]
    fallback = None
    for event in events[1:6]:
        if event["color"] != first["color"] and event["captured"] and event["capture_square"] == tracked:
            tracked = None
        if event["color"] == first["color"]:
            fallback = fallback or event
            if tracked is not None and event["source"] == tracked:
                return event
    return fallback


def _terminal_section(event):
    outcome = event["after"].outcome(claim_draw=False)
    if outcome is None:
        return None
    if outcome.winner is not None:
        ending = "checkmate for " + _side(outcome.winner)
    else:
        reason = {chess.Termination.STALEMATE: "stalemate",
                  chess.Termination.INSUFFICIENT_MATERIAL: "insufficient mating material",
                  chess.Termination.SEVENTYFIVE_MOVES: "the seventy-five-move rule",
                  chess.Termination.FIVEFOLD_REPETITION: "fivefold repetition"}.get(outcome.termination, "a draw rule")
        ending = "a draw by " + reason
    return {"event": event, "text": f"With the shown replies, {event['san']} ends this line in {ending}.",
            "squares": _squares(event["source"], event["target"],
                                event["after"].king(not event["color"]) if outcome.winner is not None else None)}


def _plan(theme, facts, events, perspective):
    event = _followup(events)
    if event is None:
        # A terminal opponent reply means there is no next move to plan.
        return _terminal_section(events[-1])
    ending = _terminal_section(event)
    if ending:
        return ending
    first = events[0]
    own = _owner(event["color"], perspective)
    prefix = (f"After {_side(events[1]['color'])}'s {events[1]['san']}, this line continues with {event['san']}" if event["ply_index"] == 3 else
              f"Following the shown replies, this line continues with {event['san']}")
    support_target = None
    if event["ply_index"] == 3:
        reply = events[1]
        for victim in _targets(reply["after"], reply["target"], reply["color"]):
            square = victim["square"]
            if victim["piece"] == chess.KING or event["before"].piece_at(square) != event["after"].piece_at(square):
                continue
            before_support = _supporters(event["before"], square, event["color"])
            after_support = _supporters(event["after"], square, event["color"])
            if (any(item["square"] == event["target"] for item in after_support)
                    and not any(item["square"] == event["source"] for item in before_support)):
                support_target = victim
                break
    focus = _squares(event["source"], event["target"], event["capture_square"] if event["captured"] else None)
    if support_target:
        square = chess.square_name(support_target["square"])
        text = (f"After {_side(events[1]['color'])}'s {events[1]['san']} attacks the {NAMES[support_target['piece']]} on {square}, "
                f"{event['san']} adds {NAMES[event['move'].promotion or event['piece']]} support to that square.")
        focus += _squares(support_target["square"], events[1]["target"])
    elif theme == "rook_file" and event["source"] == first["target"] and chess.square_file(event["target"]) == chess.square_file(first["target"]):
        rank = chess.square_rank(event["target"])
        destination = ("the seventh rank on " if rank == 6 and event["color"] else
                       "the second rank on " if rank == 1 and not event["color"] else "") + chess.square_name(event["target"])
        text = prefix + f", using the {facts['file']}-file to reach {destination}."
    elif theme == "passed_pawn" and event["source"] == first["target"]:
        remaining = 7 - chess.square_rank(event["target"]) if event["color"] else chess.square_rank(event["target"])
        text = prefix + (f", leaving the pawn {remaining} {'square' if remaining == 1 else 'squares'} from promotion." if not event["move"].promotion else ", promoting the pawn.")
    elif theme == "outpost" and event["source"] == first["target"]:
        text = prefix + f"; the {NAMES[first['piece']]} leaves the outpost for {chess.square_name(event['target'])} in this continuation."
    elif theme == "king_activity" and event["piece"] == chess.KING and _center_distance(event["target"]) < _center_distance(event["source"]):
        text = prefix + f", bringing {own} king another step closer to the center."
    else:
        text = prefix + ", " + _effect(event, perspective) + "."
    text += " That continuation depends on the replies shown."
    return {"event": event, "text": text, "squares": list(dict.fromkeys(focus))}


def _counterplay(theme, facts, events, perspective):
    if len(events) < 2:
        return None
    first, reply = events[:2]
    ending = _terminal_section(reply)
    if ending:
        return ending
    after, target = reply["after"], first["target"]
    actor = _side(reply["color"])
    prefix = f"{actor}'s {reply['san']}"
    focus = _squares(reply["source"], reply["target"])
    victim_square = (facts.get("victim") or {}).get("square")
    if theme == "absolute_pin" and reply["source"] == victim_square:
        victim_square = reply["target"]
    pin_remains = (theme == "absolute_pin" and after.color_at(victim_square) == reply["color"]
                   and after.is_pinned(reply["color"], victim_square)
                   and target in after.pin(reply["color"], victim_square))
    if (theme == "discovered_attack" and reply["captured"]
            and reply["capture_square"] == facts["attacker"]):
        text = prefix + f" captures the {NAMES[reply['captured']]} behind the blocker. That removes this discovered attack."
    elif reply["captured"] and reply["capture_square"] == target:
        text = prefix + f" captures the {NAMES[first['move'].promotion or first['piece']]} on {chess.square_name(target)}. The resulting exchanges still need calculation."
    elif theme == "absolute_pin" and not pin_remains:
        text = prefix + " removes this pin. The pinned piece isn't tied to the king's old line anymore."
    elif theme == "absolute_pin" and reply["source"] == facts["victim"]["square"]:
        text = prefix + " moves along the pin line, so the piece remains pinned. A pin restricts movement; it doesn't forbid every move."
    elif (theme == "pawn_lever" and after.piece_type_at(target) == chess.PAWN
            and after.color_at(target) == first["color"] and not _passed(first["after"], target, first["color"])
            and _passed(after, target, first["color"])):
        text = prefix + f" sidesteps the capture but leaves the {chess.square_name(target)}-pawn passed. No enemy pawn remains ahead on its own or an adjacent file."
        focus += _squares(target)
    elif theme in ("fork", "discovered_attack", "pawn_lever"):
        victims = facts.get("targets", [facts.get("victim")])
        moved_victim = next((victim for victim in victims if victim and victim["square"] == reply["source"]), None)
        attacker = facts.get("attacker", target)
        if (moved_victim and after.piece_at(attacker) == first["after"].piece_at(attacker)
                and after.piece_at(attacker) is not None and reply["target"] not in after.attacks(attacker)):
            text = prefix + f" moves the {NAMES[moved_victim['piece']]} out of that attack. The reply changes which targets remain available."
        elif first["after"].is_check():
            text = prefix + " answers the check in this line. Check whether another legal reply changes the follow-up."
        else:
            text = prefix + " is the response shown here, " + _effect(reply, perspective) + ". This line alone doesn't establish a forced sequence."
    elif theme == "passed_pawn" and reply["target"] == target + (8 if first["color"] else -8):
        text = prefix + " blocks the square directly ahead of the passed pawn. Support alone doesn't remove a blockade."
    elif any(item["square"] == target for item in _targets(after, reply["target"], reply["color"])):
        text = prefix + f" attacks { _owner(first['color'], perspective)} {NAMES[first['move'].promotion or first['piece']]}. The opponent can challenge the idea directly."
        focus += _squares(target)
    else:
        text = prefix + " is the response shown here, " + _effect(reply, perspective) + ". The plan remains conditional on the opponent's choices."
    return {"event": reply, "text": text, "squares": list(dict.fromkeys(focus))}


def analyze_strategy(board, row, perspective=None):
    """Explain a supported motif, or leave existing coaching unchanged."""
    if not isinstance(board, chess.Board) or not isinstance(row, dict) or not board.is_valid():
        return None
    kind, actual = row.get("classification"), row.get("uci")
    if kind not in CRITICAL | {"best", "good"}:
        return None
    if row.get("fen_before") is not None and row["fen_before"] != board.fen(en_passant="fen"):
        return None
    played = _line(board, row.get("played"), actual)
    if played is None:
        return None
    name = "best" if kind in CRITICAL else "played"
    selected = row.get(name)
    if name == "best":
        if not isinstance(selected, dict) or selected.get("uci") == actual:
            return None
        events = _line(board, selected, selected.get("uci"))
    else:
        events = played
    # The existing checkmate/draw explanation takes priority over a positional
    # motif when the selected move already ends the game.
    if not events or events[0]["after"].is_game_over(claim_draw=False):
        return None
    theme, facts = _motif(events)
    if theme is None:
        return None
    perspective = _perspective(perspective)
    first = events[0]
    idea, detail, focus = _idea(theme, facts, first, perspective)
    prefix = f"Try {first['san']}: " if name == "best" else f"With {first['san']}, "
    # Lowercase only ordinary sentence starts, never the proper names White/Black.
    body = idea[0].lower() + idea[1:] if idea.startswith(("The ", "Your ", "With ", "Moving ")) else idea
    summary = prefix + body
    plan, counter = _plan(theme, facts, events, perspective), _counterplay(theme, facts, events, perspective)
    sections = [{"id": "idea", "title": "The idea", "text": idea + " " + detail,
                 "line": name, "ply_index": 1, "squares": focus}]
    notes = [{"line": name, "ply_index": 1, "text": idea, "squares": focus}]
    for key, title, value in (("plan", "The follow-up", plan), ("counterplay", "The opponent's reply", counter)):
        if value:
            index = value["event"]["ply_index"]
            sections.append({"id": key, "title": title, "text": value["text"], "line": name,
                             "ply_index": index, "squares": value["squares"]})
            # Full sections retain the constraint; the replay already labels
            # the branch as conditional, so speak its concrete event once.
            note = value["text"].split(". ", 1)[0].rstrip(".") + "."
            if not value["event"]["after"].is_game_over(claim_draw=False):
                notes.append({"line": name, "ply_index": index, "text": note, "squares": value["squares"]})
        else:
            text = (f"The stored line stops before {_side(first['color'])}'s next move. It doesn't establish a concrete follow-up yet." if key == "plan" else
                    "No reply is recorded here. The idea is visible on the board, but the opponent's answer still needs analysis.")
            sections.append({"id": key, "title": title, "text": text, "line": name, "ply_index": 1, "squares": focus})
    return {"theme": theme, "title": TITLES[theme], "summary": summary, "principle": PRINCIPLES[theme],
            "sections": sections, "evidence": [{"kind": theme, "line": name, "ply_index": 1, "text": idea, "squares": focus}],
            "step_notes": sorted(notes, key=lambda item: item["ply_index"])}
