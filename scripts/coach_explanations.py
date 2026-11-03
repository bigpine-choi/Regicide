"""Concise local English coaching grounded in legal moves and short PVs."""
import chess
from coach_strategy import analyze_strategy
from coach_openings import describe_opening

COACH_NAME = "Rook"
COACH_VERSION = 8
PIECE_NAMES = {chess.PAWN: "pawn", chess.KNIGHT: "knight", chess.BISHOP: "bishop",
               chess.ROOK: "rook", chess.QUEEN: "queen", chess.KING: "king"}
PIECE_POINTS = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9}
CRITICAL = {"inaccuracy", "mistake", "blunder", "missed_mate", "allows_mate"}


def _side(color):
    return "White" if color else "Black"


def _material(board, color):
    return sum(points * (len(board.pieces(piece, color)) - len(board.pieces(piece, not color)))
               for piece, points in PIECE_POINTS.items())


def _targets(board, square, color):
    pin = board.pin(color, square)
    return [{"square": chess.square_name(target), "piece": board.piece_type_at(target)}
            for target in board.attacks(square)
            if board.color_at(target) == (not color) and board.piece_type_at(target) != chess.KING
            and target in pin]


def _defenders(board, square, color):
    return [{"square": chess.square_name(source), "piece": board.piece_type_at(source)}
            for source in board.attackers(color, square)
            if board.piece_type_at(source) != chess.KING and square in board.pin(color, source)]


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
        bishop_rooms = {square: len(current.attacks(square) & ~current.occupied_co[color])
                        for square in current.pieces(chess.BISHOP, color)} if piece == chess.PAWN else {}
        current.push(move)
        king = current.king(current.turn)
        events.append({"uci": uci, "san": san, "index": index, "color": color,
                       "source": source, "target": target, "piece": piece,
                       "captured": captured, "capture_square": chess.square_name(capture_square),
                       "recapture": recapture, "castle": castle, "promotion": move.promotion,
                       "was_check": was_check, "check": current.is_check(), "mate": current.is_checkmate(),
                       "king": chess.square_name(king) if king is not None else None,
                       "targets": _targets(current, move.to_square, color),
                       "defenders": _defenders(current, move.to_square, color),
                       "opens_bishop": any(len(current.attacks(square) & ~current.occupied_co[color]) > room
                                           for square, room in bishop_rooms.items())})
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
        if event["targets"] and not event["mate"]:
            target = max(event["targets"], key=lambda target: PIECE_POINTS[target["piece"]])
            facts.append({**common, "kind": "attack", "squares": [event["target"], target["square"]],
                          "text": f"{actor}'s {PIECE_NAMES[event['promotion'] or event['piece']]} attacks {other}'s {PIECE_NAMES[target['piece']]} on {target['square']}."})
    difference = replay["material_change"]
    if difference and len(replay["events"]) <= max_ply:
        facts.append({"kind": "material", "line": line_name, "ply_index": len(replay["events"]), "squares": [],
                      "text": f"At this line's endpoint: {_side(replay['color'])}'s material balance changes by {difference:+d}, counting pieces only."})
    priority = {"checkmate": 0, "capture": 1, "promotion": 2, "attack": 3, "castle": 4, "check": 5, "material": 6}
    return sorted(facts, key=lambda fact: (priority[fact["kind"]],
                  -PIECE_POINTS.get(replay["events"][fact["ply_index"] - 1]["captured"], 0) if fact["kind"] == "capture" else 0,
                  fact["ply_index"]))[:4]


def _develops(event):
    homes = {chess.KNIGHT: ("b1", "g1", "b8", "g8"), chess.BISHOP: ("c1", "f1", "c8", "f8")}
    return event["source"] in homes.get(event["piece"], ()) and event["target"][1] != event["source"][1]


def _owner(color, perspective):
    return "your" if color == perspective else _side(color) + "'s"


def _subject(color, perspective):
    return "You" if color == perspective else _side(color)


def _support_advice(actual, best, perspective):
    if (best and actual["piece"] == best["piece"] and actual["source"] == best["source"]
            and not actual["defenders"] and best["defenders"]):
        support = min(best["defenders"], key=lambda item: (item["piece"] != chess.PAWN, item["square"]))
        piece = f"{support['square'][0]}-pawn" if support["piece"] == chess.PAWN else PIECE_NAMES[support["piece"]]
        return f"{best['san']} gives {_owner(actual['color'], perspective)} {PIECE_NAMES[best['promotion'] or best['piece']]} protection from the {piece}."
    return None


def _alternative(board, actual, best, perspective, capture=None):
    if best is None or best["uci"] == actual["uci"]:
        return "Check that reply before committing to the move."
    if capture and best["source"] == capture["capture_square"] and best["piece"] == capture["captured"]:
        # Only describe escaping this particular attack, never general safety.
        position = board.copy(stack=True)
        position.push_uci(best["uci"])
        source, target = chess.parse_square(capture["source"]), chess.parse_square(best["target"])
        if (position.piece_type_at(source) == capture["piece"] and position.color_at(source) != actual["color"]
                and target not in position.attacks(source)):
            return f"{best['san']} moves the {PIECE_NAMES[best['piece']]} away from that attack."
    support = _support_advice(actual, best, perspective)
    if support:
        return support
    if actual["target"] == best["target"] and actual["piece"] != best["piece"] and _develops(best):
        return f"{best['san']} brings the {PIECE_NAMES[best['piece']]} into play on that square instead."
    if _develops(best):
        return f"Consider {best['san']} to bring the {PIECE_NAMES[best['piece']]} into play."
    return f"Look at {best['san']} instead, then check the opponent's strongest reply."


def _after_actual(board, actual):
    position = board.copy(stack=True)
    position.push_uci(actual["uci"])
    return position


def _immediate_capture(board, actual, capture):
    position = _after_actual(board, actual)
    move = chess.Move.from_uci(capture["uci"])
    return move in position.legal_moves and position.piece_type_at(chess.parse_square(capture["capture_square"])) == capture["captured"]


def _opened_line(board, actual, events, capture):
    if len(events) < 2 or not events[1]["check"] or capture["index"] <= 2:
        return None
    position = _after_actual(board, actual)
    attacker, victim = chess.parse_square(capture["source"]), chess.parse_square(capture["capture_square"])
    if (position.piece_type_at(attacker) != capture["piece"] or position.color_at(attacker) != capture["color"]
            or victim in position.attacks(attacker)):
        return None
    position.push_uci(events[1]["uci"])
    if position.piece_type_at(victim) != capture["captured"] or victim not in position.attacks(attacker):
        return None
    if chess.square_file(attacker) == chess.square_file(victim):
        return capture["source"][0] + "-file"
    return "diagonal" if abs(chess.square_file(attacker) - chess.square_file(victim)) == abs(chess.square_rank(attacker) - chess.square_rank(victim)) else "rank"


def _dialogue(board, kind, actual, played, best, perspective=None):
    if kind == "uncertain":
        return ""
    if actual is None:
        return "I can't verify that move from this position. Reanalyze it before drawing a lesson."
    color, san = actual["color"], actual["san"]
    actor, other = _subject(color, perspective), _subject(not color, perspective)
    own, opposing = _owner(color, perspective), _owner(not color, perspective)
    personal = color == perspective
    if played is None:
        return "The move is legal, but this continuation doesn't check out. Reanalyze the position before relying on the recommendation."
    events, best_event = played["events"], best["events"][0] if best else None
    if kind == "missed_mate" and best:
        mate = next((event for event in best["events"] if event["mate"] and event["color"] == color), None)
        if mate:
            if mate["index"] == 1:
                return f"{actor} had mate in one with {mate['san']}. Before settling on a move, look for checks that leave no legal reply."
            return f"{best_event['san']} starts a mating line in the analysis. Test the other defenses before assuming the finish is forced."
    if kind == "allows_mate":
        mate = next((event for event in events if event["mate"] and event["color"] != color), None)
        if mate and mate["index"] == 2:
            lead = f"{other} can finish immediately with {mate['san']}."
        elif mate:
            lead = f"The shown line after {san} ends in checkmate against {own} king."
        else:
            lead = f"The analysis flags a mating threat against {own} king after {san}."
        return lead + " " + _alternative(board, actual, best_event, perspective)
    if actual["mate"]:
        return "That's checkmate: there isn't a legal reply. Notice that the king can't escape, capture the checking piece, or block the attack."
    if len(events) > 1 and events[1]["mate"]:
        return f"{other} can answer with {events[1]['san']}, an immediate checkmate. Check the threats against {own} king before pursuing this idea."
    if kind in CRITICAL:
        captures = [event for event in events[1:4] if event["color"] != color and event["captured"]]
        if captures:
            capture = max(captures, key=lambda event: (PIECE_POINTS[event["captured"]], -event["index"]))
            victim, capturer = PIECE_NAMES[capture["captured"]], PIECE_NAMES[capture["piece"]]
            opened = _opened_line(board, actual, events, capture)
            if opened:
                lead = f"After {san}, {events[1]['san']} exposes {own} {victim} on the {opened}."
            elif _immediate_capture(board, actual, capture):
                target = max(actual["targets"], key=lambda item: PIECE_POINTS[item["piece"]]) if actual["targets"] else None
                if target and actual["piece"] == chess.PAWN:
                    lead = f"{own.capitalize()} pawn attacks the {PIECE_NAMES[target['piece']]}, but {opposing} {capturer} can still take the {victim}."
                else:
                    lead = f"{own.capitalize()} {victim} on {capture['capture_square']} can be taken by {opposing} {capturer}."
            else:
                lead = f"In the shown line after {san}, {other.lower() if other == 'You' else other} can take {own} {victim}."
            return lead + " " + _alternative(board, actual, best_event, perspective, capture)
        if best_event and best_event["uci"] != actual["uci"]:
            support = _support_advice(actual, best_event, perspective)
            if support:
                return support + " Check how that changes the opponent's captures."
            if actual["target"] == best_event["target"] and actual["piece"] != best_event["piece"]:
                return f"The {PIECE_NAMES[best_event['piece']]} could use {best_event['target']}, but the {PIECE_NAMES[actual['piece']]} takes that square. " + _alternative(board, actual, best_event, perspective)
            return f"Take a closer look at {best_event['san']}. Follow the first reply before deciding what to do with the {PIECE_NAMES[actual['piece']]}."
    if actual["promotion"]:
        piece = PIECE_NAMES[actual["promotion"]]
        lead = f"{actor} {'turn' if personal else 'turns'} the pawn into a {piece}"
        if actual["captured"]:
            lead += f" and {'take' if personal else 'takes'} {opposing} {PIECE_NAMES[actual['captured']]}"
        return lead + ". Check the new position before planning the next move."
    if actual["castle"]:
        return f"{actor} {'have' if personal else 'has'} castled {'kingside' if actual['target'][0] == 'g' else 'queenside'}. Before pushing pawns near the king, check which lines that could open."
    if len(events) > 1 and events[1]["recapture"]:
        reply = events[1]
        return f"{actor} {'take' if personal else 'takes'} a {PIECE_NAMES[actual['captured']]}, and {other.lower() if other == 'You' else other} can recapture the {PIECE_NAMES[reply['captured']]} with {reply['san']}. Count everything that comes off before judging the exchange."
    if actual["captured"]:
        lead = f"{actor} {'take' if personal else 'takes'} {opposing} {PIECE_NAMES[actual['captured']]} on {actual['capture_square']}."
        if len(events) > 1 and events[1]["captured"]:
            reply = events[1]
            return lead + f" {other} can answer {reply['san']}, so include that {PIECE_NAMES[reply['captured']]} in the trade."
        return lead + " Check for a recapture or a stronger threat before calling it a gain."
    threats = [target for target in actual["targets"] if target["piece"] >= chess.KNIGHT]
    if threats and not actual["check"]:
        target = max(threats, key=lambda item: PIECE_POINTS[item["piece"]])
        victim = PIECE_NAMES[target["piece"]]
        verb = "develop" if _develops(actual) else "move"
        lead = f"{actor} {verb if personal else verb + 's'} a {PIECE_NAMES[actual['piece']]} with an attack on {opposing} {victim}."
        if len(events) > 1:
            reply = events[1]
            if reply["captured"] and reply["target"] == actual["target"]:
                lead = f"{other} can take {own} {PIECE_NAMES[actual['piece']]} with {reply['san']}."
                if len(events) > 2 and events[2]["recapture"]:
                    return lead + f" {events[2]['san']} recaptures the {PIECE_NAMES[events[2]['captured']]}; count both sides before judging the exchange."
                return lead + " Check for a recapture before deciding whether the exchange works."
            if reply["source"] == target["square"]:
                after_reply = board.copy(stack=True)
                after_reply.push_uci(actual["uci"])
                after_reply.push_uci(reply["uci"])
                if chess.parse_square(reply["target"]) not in after_reply.attacks(chess.parse_square(actual["target"])):
                    return lead + f" {reply['san']} moves it out of the {PIECE_NAMES[actual['piece']]}'s reach."
            ray = chess.SquareSet(chess.between(chess.parse_square(actual["target"]), chess.parse_square(target["square"])))
            if chess.parse_square(reply["target"]) in ray:
                return lead + f" {reply['san']} blocks the line, so the {PIECE_NAMES[actual['piece']]} needs a new plan."
        return lead + " Keep developing once that threat has been answered."
    if actual["check"]:
        # Moving a blocker can uncover another piece's check; the mover is not
        # necessarily the checking piece.
        lead = f"That move gives check, so {other.lower() if other == 'You' else other} must respond."
        return lead + (f" Look at {events[1]['san']} before deciding how to continue." if len(events) > 1 else " Check the king's escapes and possible blocks before continuing.")
    if actual["was_check"]:
        return f"{actor} {'are' if personal else 'is'} out of check. Now look for a move that improves a piece without allowing another forcing reply."
    if actual["piece"] == chess.PAWN and actual["target"] in ("d4", "e4", "d5", "e5"):
        lead = ("You're taking" if personal else actor + " takes") + " space in the center"
        lead += " and opening a bishop's diagonal." if personal and actual["opens_bishop"] else " and opens a bishop's diagonal." if actual["opens_bishop"] else "."
        if len(events) > 1 and any(target["square"] == actual["target"] for target in events[1]["targets"]):
            reply = events[1]
            return lead + f" {other} can challenge the pawn with {reply['san']}, so plan a response."
        return lead + " Look for a useful square for a knight or bishop next."
    if _develops(actual):
        if len(events) > 1 and any(target["square"] == actual["target"] for target in events[1]["targets"]):
            return f"{own.capitalize()} {PIECE_NAMES[actual['piece']]} develops, but {other.lower() if other == 'You' else other} can attack it with {events[1]['san']}. Have a retreat ready before placing it on that square."
        return f"{own.capitalize()} {PIECE_NAMES[actual['piece']]} is off the back rank and in play. Look for a useful square for another piece before moving this one again."
    if actual["piece"] == chess.PAWN:
        return f"That pawn move changes which squares {actor.lower() if personal else actor} can control. Check the space it leaves behind before committing another pawn."
    return f"The {PIECE_NAMES[actual['piece']]} has a new square. Check the opponent's forcing replies before deciding where it should go next."


def _hint(board, kind, actual, played, best, perspective=None):
    if kind == "uncertain":
        return ""
    own, opposing = _owner(board.turn, perspective), _owner(not board.turn, perspective)
    if kind == "allows_mate":
        return f"Deal with the threats against {own} king before looking for anything else."
    if kind == "missed_mate":
        return f"Look for checks against {opposing} king, and test every escape."
    if board.is_check():
        return f"Get {own} king out of check, then check the next forcing reply."
    events = played["events"] if played else []
    if any(event["color"] != board.turn and event["captured"] for event in events[1:4]):
        return "Check what could be captured after the king's safety has been dealt with."
    best_event = best["events"][0] if best else None
    if any(event and event["captured"] for event in (actual, best_event)):
        return "Count the captures for both sides, including the first recapture."
    if any(event and event["check"] for event in (actual, best_event)):
        return "Start with checks, then look for escapes, captures, and blocks."
    if any(event and event["targets"] for event in (actual, best_event)):
        return "Look for a move that improves a piece while making a threat."
    if any(event and _develops(event) for event in (actual, best_event)):
        return "Find a useful square for an undeveloped piece, then test the reply."
    return "Identify the immediate threat before starting a new plan."


def _has_mate_in_one(board):
    """A local rule check, not a replacement search/evaluation engine."""
    for move in list(board.legal_moves):
        board.push(move)
        mate = board.is_checkmate()
        board.pop()
        if mate:
            return True
    return False


def _teaching(board, kind, actual, played, best):
    """Choose one verified effect of the better move and one transferable habit.

    The classification chooses the alternative; geometry never regrades a move.
    Build facts once, then render both human perspectives without changing them.
    """
    if kind not in CRITICAL or not actual or not played or not best or actual["mate"]:
        return None
    move = best["events"][0]
    if move["uci"] == actual["uci"]:
        return None
    position = _after_actual(board, move)
    common = {"uci": move["uci"], "san": move["san"], "line": "best", "ply_index": 1}

    def lesson(concept, principle, **facts):
        return {**common, "concept": concept, "principle": principle, **facts}

    if move["mate"]:
        return lesson("mate_first", "Look for mate before settling for a smaller gain.")
    if len(played["events"]) > 1 and played["events"][1]["mate"] and not _has_mate_in_one(position):
        return lesson("meet_mating_threat", "Answer mating threats before starting your own attack.")
    if board.is_check():
        response = "king_move"
        if move["piece"] != chess.KING:
            checkers = board.checkers()
            if move["captured"] and chess.parse_square(move["capture_square"]) in checkers:
                response = "capture"
            elif len(checkers) == 1 and chess.parse_square(move["target"]) in chess.SquareSet(chess.between(next(iter(checkers)), board.king(board.turn))):
                response = "block"
        return lesson("answer_check", "When in check, compare king moves, captures, and blocks.", response=response)
    captures = [event for event in played["events"][1:4] if event["color"] != board.turn and event["captured"]]
    for capture in sorted(captures, key=lambda event: -PIECE_POINTS[event["captured"]]):
        source = chess.parse_square(capture["source"])
        original = chess.parse_square(actual["source"])
        if (capture["capture_square"] == actual["target"] and move["source"] != actual["source"]
                and position.piece_type_at(original) == actual["piece"] and position.color_at(original) == actual["color"]
                and position.piece_type_at(source) == capture["piece"] and position.color_at(source) == capture["color"]
                and original not in position.attacks(source)):
            principle = ("Pawns can't retreat; check the destination before pushing." if actual["piece"] == chess.PAWN else
                         "Before moving a piece, check the attacks on its destination.")
            return lesson("avoid_moving_into_attack", principle, attacker=PIECE_NAMES[capture["piece"]])
        if move["source"] != capture["capture_square"] or move["piece"] != capture["captured"]:
            continue
        source, target = chess.parse_square(capture["source"]), chess.parse_square(move["target"])
        if (position.piece_type_at(source) == capture["piece"] and position.color_at(source) == capture["color"]
                and target not in position.attacks(source)):
            return lesson("meet_piece_threat", "Check what your opponent can capture before starting your own attack.",
                          attacker=PIECE_NAMES[capture["piece"]])
    if _support_advice(actual, move, None):
        return lesson("choose_defended_square", "When choosing a square, check who could recapture.")
    if move["captured"]:
        return lesson("count_recaptures", "Count both sides' recaptures before judging a trade.")
    if _develops(move):
        if move["targets"]:
            target = max(move["targets"], key=lambda item: PIECE_POINTS[item["piece"]])
            return lesson("develop_with_threat", "Look for development moves that also create a concrete threat.", target=target)
        return lesson("develop_pieces", "Bring another piece into play when there isn't an urgent threat.")
    if move["promotion"]:
        return lesson("compare_promotions", "Compare promotion choices for checks, captures, and stalemate.")
    if move["castle"]:
        return lesson("castle_with_care", "After castling, check which pawn moves would open lines toward the king.")
    if move["check"]:
        return lesson("calculate_checks", "A check isn't automatically best; calculate the opponent's legal replies.")
    if move["targets"]:
        target = max(move["targets"], key=lambda item: PIECE_POINTS[item["piece"]])
        return lesson("calculate_threats", "Before making a threat, calculate how your opponent can answer it.", target=target)
    # Do not invent positional understanding when no supported explanation fits.
    return lesson("compare_forcing_replies", "Compare checks, captures, and threats for both candidate moves.")


def _teaching_text(teaching, actual, best, perspective):
    move = best["events"][0]
    san, concept = move["san"], teaching["concept"]
    piece = PIECE_NAMES[move["promotion"] or move["piece"]]
    own, opposing = _owner(move["color"], perspective), _owner(not move["color"], perspective)
    if concept == "mate_first":
        reason = f"Try {san}: that's checkmate right now."
    elif concept == "meet_mating_threat":
        reason = f"{san} avoids mate in one."
    elif concept == "meet_piece_threat":
        reason = f"{san} moves {own} {piece} out of that {teaching['attacker']}'s attack."
    elif concept == "avoid_moving_into_attack":
        reason = f"{san} keeps {own} {PIECE_NAMES[actual['piece']]} on {actual['source']}, out of that {teaching['attacker']}'s attack."
    elif concept == "choose_defended_square":
        reason = _support_advice(actual, move, perspective)
    elif concept == "count_recaptures":
        reason = f"{san} captures {opposing} {PIECE_NAMES[move['captured']]}"
        reason += " with check." if move["check"] else "."
    elif concept == "develop_with_threat":
        target = teaching["target"]
        reason = f"{san} develops {own} {piece} and attacks the {PIECE_NAMES[target['piece']]} on {target['square']}."
    elif concept == "develop_pieces":
        reason = f"{san} brings {own} {piece} into play."
    elif concept == "answer_check":
        reason = (f"{san} removes the checking piece." if teaching["response"] == "capture" else
                  f"{san} blocks the check." if teaching["response"] == "block" else
                  f"{san} gets {own} king out of check.")
    elif concept == "compare_promotions":
        reason = f"{san} promotes to a {piece}."
    elif concept == "castle_with_care":
        reason = f"{san} castles {'kingside' if move['target'][0] == 'g' else 'queenside'}, bringing the rook closer to the center."
    elif concept == "calculate_checks":
        reason = f"{san} gives check and makes the king's safety the immediate priority."
    elif concept == "calculate_threats":
        target = teaching["target"]
        reason = f"{san} attacks {opposing} {PIECE_NAMES[target['piece']]} on {target['square']}."
    else:
        reason = f"Regicide prefers {san}, but the reason needs a closer look."
    return {"concept": concept, "alternative": {"uci": teaching["uci"], "san": san, "reason": reason},
            "principle": teaching["principle"], "line": "best", "ply_index": 1,
            "grounding": "search_preference" if concept == "compare_forcing_replies" else "verified_move_effect"}


def _with_teaching(dialogue, teaching):
    if teaching is None:
        return dialogue
    alternative = teaching["alternative"]
    explanation = alternative["reason"] + " " + teaching["principle"]
    # Keep one useful observation about the actual move when it adds information.
    # All explanations stay below the existing 40-word ceiling; never truncate SAN.
    observation = dialogue.split(". ", 1)[0].rstrip(".") + "."
    combined = observation + " " + explanation
    if alternative["san"] not in observation and len(combined.split()) <= 40:
        return combined
    return explanation


def _continuation_facts(board, row, kind, actual, played, best, lesson):
    """Use the existing legal PV and frames, never extend or search a line.

    Material comparisons use the same root and the same number of plies. Full
    history is retained for automatic draw rules; a claimable draw is optional.
    """
    if kind not in CRITICAL or not lesson or not actual or not played or not best or not board.is_valid():
        return None
    for name, replay in (("played", played), ("best", best)):
        branch = row.get(name)
        if (not isinstance(branch, dict) or branch.get("bound") != "exact"
                or branch.get("uci") != replay["events"][0]["uci"]):
            return None
    if row.get("fen_before") is not None and row["fen_before"] != board.fen(en_passant="fen"):
        return None
    frames = row["best"].get("pv_frames")
    if not isinstance(frames, list) or len(frames) < min(6, len(best["events"])) + 1:
        return None
    position, positions = board.copy(stack=True), []
    try:
        for index, event in enumerate([None] + best["events"]):
            if event:
                if position.is_game_over(claim_draw=False):
                    return None
                position.push_uci(event["uci"])
            if index <= 6:
                frame = frames[index]
                if not isinstance(frame, dict) or frame.get("fen") != position.fen(en_passant="fen"):
                    return None
                positions.append(position.copy(stack=True))
    except (ValueError, KeyError, IndexError, TypeError):
        return None
    if positions[0].is_game_over(claim_draw=False):
        return None
    events = best["events"][:6]
    count = len(events)
    endpoint = positions[-1]
    outcome = endpoint.outcome(claim_draw=False)
    comparison, played_outcome, played_terminal_ply = None, None, None
    other = board.copy(stack=True)
    for event in played["events"][:count]:
        other.push_uci(event["uci"])
        played_outcome = other.outcome(claim_draw=False)
        if event["index"] == count:
            comparison = _material(endpoint, board.turn) - _material(other, board.turn)
        if played_outcome:
            played_terminal_ply = event["index"]
            break
    return {"events": events, "positions": positions, "outcome": outcome,
            "comparison": comparison, "played_outcome": played_outcome,
            "played_terminal_ply": played_terminal_ply, "played_endpoint": other,
            "material_change": _material(endpoint, board.turn) - _material(board, board.turn),
            "color": board.turn}


def _draw_reason(outcome):
    return {chess.Termination.STALEMATE: "stalemate", chess.Termination.INSUFFICIENT_MATERIAL: "insufficient mating material",
            chess.Termination.SEVENTYFIVE_MOVES: "the seventy-five-move rule",
            chess.Termination.FIVEFOLD_REPETITION: "fivefold repetition"}.get(outcome.termination, "a draw rule")


def _continuation_step(event, before, after, perspective):
    actor, owner = _subject(event["color"], perspective), _owner(event["color"], perspective)
    opposing = _owner(not event["color"], perspective)
    piece = PIECE_NAMES[event["promotion"] or event["piece"]]
    squares = [event["source"], event["target"]]
    outcome = after.outcome(claim_draw=False)

    def verb(base):
        return base if actor == "You" else base + "s"

    if event["mate"]:
        text = f"{actor} {verb('deliver')} checkmate in this line. {_side(not event['color'])} has no legal reply."
        squares.append(event["king"])
    elif after.is_stalemate():
        text = f"That ends this line in stalemate. {_side(not event['color'])} has no legal move, but the king isn't in check."
    elif outcome:
        text = f"This line ends in a draw by {_draw_reason(outcome)}. There is no next move to play."
    elif event["promotion"]:
        capture = f"{verb('capture')} {opposing} {PIECE_NAMES[event['captured']]} and " if event["captured"] else ""
        text = f"{actor} {capture}{verb('promote')} to a {piece}."
        if event["captured"]:
            squares.append(event["capture_square"])
    elif event["captured"]:
        action = "recapture" if event["recapture"] else "capture"
        text = f"{actor} {verb(action)} {opposing} {PIECE_NAMES[event['captured']]} on {event['capture_square']}."
        squares.append(event["capture_square"])
        if before.is_en_passant(chess.Move.from_uci(event["uci"])):
            text += " It's en passant: the captured pawn leaves a different square."
    elif event["castle"]:
        flank = "kingside" if event["target"][0] == "g" else "queenside"
        rook_to = ("f" if flank == "kingside" else "d") + event["target"][1]
        text = f"{actor} {verb('castle')} {flank}, bringing the rook to {rook_to}."
        squares.append(rook_to)
    elif event["was_check"]:
        if event["piece"] == chess.KING:
            text = f"{actor} {verb('move')} the king out of check."
        else:
            text = f"{actor} {verb('block')} the check with the {piece}."
    elif event["check"]:
        # The moved piece may uncover a check from a different piece.
        text = f"That move gives check. {_side(not event['color'])} must answer the check before pursuing another plan."
        squares.append(event["king"])
    elif _develops(event):
        text = f"{actor} {verb('develop')} the {piece} to {event['target']}."
        if event["targets"]:
            target = max(event["targets"], key=lambda value: PIECE_POINTS[value["piece"]])
            text += f" It also attacks the {PIECE_NAMES[target['piece']]} on {target['square']}."
            squares.append(target["square"])
    elif event["targets"]:
        target = max(event["targets"], key=lambda value: PIECE_POINTS[value["piece"]])
        text = f"{owner.capitalize() if owner == 'your' else owner} {piece} now attacks {opposing} {PIECE_NAMES[target['piece']]} on {target['square']}."
        squares.append(target["square"])
    elif event["opens_bishop"]:
        text = f"That pawn move gives {owner} bishop more room. Check the newly opened diagonal."
    elif event["defenders"]:
        defender = min(event["defenders"], key=lambda value: (value["piece"] != chess.PAWN, value["square"]))
        text = f"{actor} {verb('place')} the {piece} on {event['target']}, protected by the {PIECE_NAMES[defender['piece']]} on {defender['square']}."
        squares.append(defender["square"])
    else:
        text = f"{actor} {verb('move')} the {piece} to {event['target']}."
    if event["check"] and not outcome and (event["captured"] or event["promotion"] or event["was_check"] or event["castle"]):
        text = text.rstrip(".") + "; that also gives check."
        squares.append(event["king"])
    return {"ply_index": event["index"], "uci": event["uci"], "san": event["san"],
            "color": _side(event["color"]).lower(), "text": text,
            "squares": list(dict.fromkeys(square for square in squares if square is not None))}


def _continuation_text(facts, teaching, perspective, insight=None):
    if facts is None or teaching is None:
        return None
    count, outcome = len(facts["events"]), facts["outcome"]
    terminal = outcome is not None
    availability = (f"This {count}-ply line ends the game." if terminal else
                    "Here's one possible six-ply continuation." if count == 6 else
                    f"Only {count} {'ply is' if count == 1 else 'plies are'} available; the continuation stops there.")
    # Introduce the concrete reason now; save the transferable principle for
    # the endpoint, instead of reading the same advice twice.
    reason = insight["summary"] if insight else teaching["alternative"]["reason"]
    intro = reason + " " + availability
    own = _owner(facts["color"], perspective)
    opposing = _owner(not facts["color"], perspective)
    horizon = f"{count} {'ply' if count == 1 else 'plies'}"
    if outcome and outcome.winner is not None:
        result = f"This line ends in checkmate for {_side(outcome.winner)}."
        if count > 1:
            result += " This single line doesn't establish a forced mate."
    elif outcome:
        result = f"This line ends in a draw by {_draw_reason(outcome)}."
    elif facts["played_outcome"] and facts["played_outcome"].winner is not None:
        mate_ply = facts["played_terminal_ply"]
        result = (f"The played line ends in checkmate for {_side(facts['played_outcome'].winner)} after {mate_ply} "
                  f"{'ply' if mate_ply == 1 else 'plies'}; this branch hasn't ended after {horizon}.")
    elif facts["comparison"] is not None:
        difference = facts["comparison"]
        endpoint, played_endpoint = facts["positions"][-1], facts["played_endpoint"]
        color = facts["color"]
        queens = tuple(len(position.pieces(chess.QUEEN, side)) for position in (endpoint, played_endpoint) for side in (color, not color))
        if difference > 0 and queens == (1, 0, 1, 1):
            result = f"After {horizon}, {own} queen survives here while {opposing} queen is gone; the played line still has both queens."
        elif difference:
            actor = "you" if color == perspective else _side(color)
            result = f"At {horizon}, this line is {abs(difference)} points {'better' if difference > 0 else 'worse'} for {actor} on material than the played line."
        else:
            result = f"Both lines have the same material balance after {horizon}; captures alone don't explain the preference."
    else:
        change = facts["material_change"]
        result = (f"With these replies, {own} material balance {'improves' if change > 0 else 'falls'} by the shown endpoint."
                  if change else "The material balance is unchanged at this endpoint.")
        result += " No equal-length material comparison is available."
    steps = [_continuation_step(event, facts["positions"][index], facts["positions"][index + 1], perspective)
             for index, event in enumerate(facts["events"])]
    if insight:
        # Notes are derived from this same legal branch. Keep its actual moves,
        # timing, endpoint and outcome; only enrich the explanation and focus.
        for note in insight.get("step_notes", []):
            index = note.get("ply_index", 0)
            if (note.get("line") == "best" and type(index) is int and 1 <= index <= count
                    and not facts["positions"][index].is_game_over(claim_draw=False)):
                steps[index - 1]["text"] = note["text"]
                steps[index - 1]["squares"] = list(dict.fromkeys(steps[index - 1]["squares"] + note["squares"]))
    principle = insight.get("principle", teaching["principle"]) if insight else teaching["principle"]
    return {"line": "best", "target_plies": 6, "available_plies": count,
            "complete": count == 6 or terminal, "terminal": terminal, "intro": intro,
            "summary": result + " " + principle, "steps": steps}


def describe_move(board, row):
    """Stored SAN/color are not tactical evidence; replay them from the board."""
    kind = row.get("classification", "pending")
    if kind == "pending":
        return None
    actual_line = _replay(board, {"pv_uci": [row.get("uci")]})
    actual = actual_line["events"][0] if actual_line else None
    played = _replay(board, row.get("played"), required=row.get("uci")) if actual else None
    candidate = row.get("best")
    best = _replay(board, candidate, required=candidate.get("uci") if isinstance(candidate, dict) else None)
    if kind in CRITICAL and any(isinstance(branch, dict) and branch.get("bound") not in (None, "exact")
                                for branch in (row.get("played"), candidate)):
        kind = "uncertain"
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
    elif focus_event and focus_event["targets"]:
        focus.append(max(focus_event["targets"], key=lambda target: PIECE_POINTS[target["piece"]])["square"])
    if kind in CRITICAL and played:
        captures = [event for event in played["events"][1:4] if event["color"] != board.turn and event["captured"]]
        if captures:
            focus.append(max(captures, key=lambda event: PIECE_POINTS.get(event["captured"], 0))["capture_square"])
    lesson = _teaching(board, kind, actual, played, best)
    continuation = _continuation_facts(board, row, kind, actual, played, best, lesson)

    def wording(perspective=None):
        teaching = _teaching_text(lesson, actual, best, perspective) if lesson else None
        insight = analyze_strategy(board, row, perspective) if kind != "uncertain" else None
        return {"dialogue": _with_teaching(_dialogue(board, kind, actual, played, best, perspective), teaching),
                "teaching": teaching, "hint": _hint(board, kind, actual, played, best, perspective),
                "insight": insight,
                "continuation": _continuation_text(continuation, teaching, perspective, insight)}

    return {"mood": mood, **wording(), "opening": describe_opening(board, row),
            "perspectives": {name: wording(color) for name, color in (("white", chess.WHITE), ("black", chess.BLACK))},
            "evidence": facts, "focus_squares": list(dict.fromkeys(focus))}


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
        summary = "The analysis is still running. Once a position is ready, we'll look at what mattered and what to try next."
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
            summary += "Start by checking the opponent's immediate reply. "
        if positives and important:
            summary += "There's a good move worth keeping, too."
    else:
        summary = "There isn't a clear lesson yet; the inconclusive positions need a closer look."
    if len(ready) < len(rows) and not lessons:
        summary += " Unanalyzed moves are excluded."
    return {"name": COACH_NAME, "summary": summary, "lesson_plies": lessons, "version": COACH_VERSION}
