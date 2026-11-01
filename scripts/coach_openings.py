"""Opening plans for the actual game, separate from search and move grading.

Names come from the pinned CC0 catalogue, including its exact display-name
overrides. Family descriptions are original summaries of the official sources
listed below. They describe connected plans, not a fixed opening matchup score.
"""
import unicodedata

import chess

from opening_guide import CatalogError, _catalog, position_key


MAX_PLIES = 24
SOURCE_ROOT = "https://www.chess.com/openings/"

# family: White setup, Black response, source slug, short shared idea,
# White's plan, Black's reply. Plans are conditional where the family permits
# different pawn structures; the name is never proof that a future move happened.
PROFILES = {
    "sicilian": (
        "King's Pawn Opening", "Sicilian Defense", "Sicilian-Defense",
        "The Sicilian's ...c5 challenged d4 without copying ...e5; subsequent exchanges determine how each side uses the resulting space and files.",
        "White's e-pawn takes central space while Black's c-pawn challenges d4. In an Open Sicilian, playing d4 exchanges a central pawn for Black's c-pawn and opens lines for active pieces. Delaying d4 keeps different structures available. The useful question is how White can finish development while making that central space count.",
        "Black creates an asymmetric center rather than copying White's e-pawn move. If the c-pawn is exchanged on d4, the c-file can become a route for the rooks, provided other pieces don't block it. Black's counterplay has to accompany development and king safety; the opening's reputation doesn't make an unprepared attack sound."),
    "french": (
        "King's Pawn Opening", "French Defense", "French-Defense",
        "The French started with ...e6 preparing ...d5; White's choice of advance or exchange determines the later structure.",
        "Against ...e6 and ...d5, White chooses the kind of center to play. Advancing e5 can gain space but creates a pawn chain that needs support; exchanging on d5 leads to a different, more open structure. Don't borrow a kingside attack from a closed French variation unless the actual pawn structure supports it.",
        "Black supports the d5 challenge with the e-pawn, which can limit the c8-bishop until the structure changes. In a closed center, pressure against White's d4 base is a recurring plan, often involving a prepared ...c5. After central exchanges, development and open lines take priority over a plan designed for a locked pawn chain."),
    "caro": (
        "King's Pawn Opening", "Caro-Kann Defense", "Caro-Kann-Defense",
        "The Caro-Kann started with ...c6 supporting ...d5, initially leaving the c8-bishop's diagonal available.",
        "White meets a prepared ...d5 challenge and must decide how much central space to keep. An advance, an exchange, and a maintained pawn tension lead to different plans. Develop pieces that support the chosen center rather than pushing pawns automatically; a space advantage is useful only if the pieces can use it.",
        "Black's ...c6 supports ...d5 without initially blocking the c8-bishop with an e-pawn. That is an option to develop the bishop, not proof it has already come out. Black aims to complete development and challenge White's center at the right moment; extra defensive pawn moves can leave the pieces short of activity."),
    "italian": (
        "Italian Game", "Open Game response", "Italian-Game",
        "The Italian began with Bc4 toward f7; the useful plan depends on how the central pawns and pieces have changed.",
        "The Italian develops the bishop toward f7 while preparing the rest of White's pieces and king. That diagonal passes through d5, so central pawn changes affect the bishop's work. White often chooses between a direct d4 challenge and a slower buildup. An attack on f7 still needs calculation; the bishop alone doesn't prove a tactic.",
        "Black's ...e5 contests the center and gives the pieces room to develop. Against White's bishop, Black can develop actively while watching the e5 pawn and the timing of d4. The concrete variation matters: ...Bc5 and ...Nf6 create different threats. Copying a familiar Italian plan without checking White's next central move can lose time."),
    "ruy": (
        "Ruy Lopez", "Open Game response", "Ruy-Lopez-Opening",
        "The Ruy Lopez began by pressuring the c6-knight, connecting bishop development with the struggle over e5.",
        "Bb5 puts pressure on the knight that often supports Black's e5 pawn. White can develop, castle, and prepare a central expansion rather than treating the opening as an immediate pawn-winning trick. Whether exchanging on c6 helps depends on the resulting pawn structure and the available recaptures, not just on removing a defender.",
        "Black must coordinate the defense of e5 with development and the bishop question. Challenging the bishop can gain useful space, but it also spends time and changes the queenside pawn structure. Black's central response remains essential: White's pressure is a long-term problem to manage, not proof that the e5 pawn is already lost."),
    "scotch": (
        "Scotch Game", "Open Game response", "Scotch-Game",
        "White's early d4 challenge opens central play before a slow maneuvering battle develops.",
        "White challenges the e5 center early with d4. After exchanges, open lines make piece development and concrete threats especially important. A knight on d4 can be active but may also become a target. White should connect the central initiative with developing the remaining pieces instead of assuming that extra space alone settles the opening.",
        "Black answers the central challenge through exchanges and active development. Depending on the variation, pressure against e4 or the knight on d4 can create useful tempi. The point is to contest White's activity with concrete threats, not simply to hold a passive position. Each exchange changes which central squares and pieces need attention."),
    "petrov": (
        "King's Knight Opening", "Petrov's Defense", "Petrovs-Defense",
        "Black's ...Nf6 counterattacks e4 instead of first defending e5 with ...Nc6.",
        "White faces a counterattack against e4 while attacking e5 with the knight. The order of captures and recaptures matters, because a familiar-looking pawn exchange can expose a tactical mistake. After the central tension is resolved, active development remains the goal. A symmetrical appearance doesn't remove the need to calculate concrete threats.",
        "Black's knight challenges White's e4 pawn rather than accepting a purely defensive role. Correctly timed central exchanges can lead to a sound structure, but Black still has to finish development and watch forcing moves. The Petrov's solid reputation is not a guarantee of a draw, nor permission to copy moves without checking their order."),
    "qg": (
        "Queen's Gambit", "Central ...d5 setup", "Queens-Gambit",
        "White's c-pawn challenges Black's d5 center; Black's answer determines how that tension develops.",
        "White supports a central claim with d4 and challenges Black's d5 pawn using c4. The c-pawn offer asks Black a structural question, rather than promising an immediate attack. White's next development should fit the response: accepting the pawn, supporting d5, and changing the center produce different positions and different useful squares.",
        "Black chooses how to handle the pressure on d5. Capturing on c4, supporting d5 with ...e6, and supporting it with ...c6 are distinct plans with different effects on the bishops and central pawns. This is one shared fight over the center; neither side's opening name determines who has played the better moves."),
    "qgd": (
        "Queen's Gambit", "Queen's Gambit Declined", "Queens-Gambit-Declined",
        "Black supports d5 with ...e6; White tries to make central pressure count before Black fully frees the pieces.",
        "White uses c4 to challenge a d5 pawn supported by ...e6. Keeping the tension, exchanging, and expanding the center each change the available plans. White can use the extra room to develop actively, but should watch for Black's central counterplay. A structural target matters only when pieces can actually put pressure on it.",
        "Black keeps a strong central foothold while accepting that the c8-bishop may need a route into play. A prepared ...c5 is a recurring way to challenge White's d4 center. The timing depends on development and tactics: Black wants to free the position without leaving a central pawn or the king exposed."),
    "qga": (
        "Queen's Gambit", "Queen's Gambit Accepted", "Queens-Gambit-Accepted",
        "Black captures on c4; White can use the central space while arranging to recover the pawn.",
        "White has offered the c-pawn and can use the central space to develop while arranging its recovery. Spending too many moves chasing the pawn can defeat that purpose. The useful comparison is between development, the central pawn structure, and the time needed to recapture, rather than treating the temporary pawn count as the whole story.",
        "Black takes on c4 to change the central tension and give the pieces freer play. Keeping the extra pawn at all costs is not the only plan; returning it can help finish development or challenge the center. Black should judge any attempt to hold c4 by the time and structural concessions it requires."),
    "slav": (
        "Queen's Gambit", "Slav Defense", "Slav-Defense",
        "The Slav began with ...c6 supporting d5, initially keeping different bishop options from an ...e6 setup.",
        "White's c4 challenge meets a d5 pawn supported by ...c6. The tension can be maintained or resolved, and those choices affect the center and bishop development. White should keep development connected to pressure on the center. A plan from a different Queen's Gambit structure may not fit after the pawn exchanges change.",
        "The Slav's ...c6 supports d5 while initially leaving the c8-bishop's diagonal unblocked by the e-pawn. That doesn't guarantee the bishop develops outside the chain. If Black later adds ...e6, the bishop and pawn breaks need a different treatment. Black's aim is sound development with a timely challenge to White's central space."),
    "london": (
        "London System", "Response to the London", "London-System",
        "White develops the queen's bishop early; Black still has several ways to challenge the d4 center.",
        "The London brings the queen's bishop out early, before an e3 setup would shut it in. Many lines support d4 with e3 and c3, but that is not a universal recipe: an early Nc3 changes the pawn options. White should build around the actual piece placement rather than replaying a fixed setup regardless of Black's response.",
        "Black can challenge the d4 center, including a prepared ...c5, or choose a fianchetto structure with different pressure. The response should address White's actual setup, not a stereotype of the London. If White has committed the knight to c3 or changed the center, the useful pawn breaks and exchanges can change as well."),
    "kid": (
        "Queen's Pawn center", "King's Indian Defense", "Kings-Indian-Defense",
        "White can occupy the center while Black develops around a kingside fianchetto and prepares a pawn challenge.",
        "White can build a broad center while Black develops around a kingside fianchetto. That space creates possibilities, but also pawns that Black can attack. A closed-center plan belongs only to a position that actually has the relevant pawn chains. White should decide how to use the center before treating a flank attack as automatic.",
        "Black develops the bishop on the long diagonal and prepares a central challenge. The early ...Nf6 and ...g6 move order can still lead to other families, including a Grünfeld with ...d5. A kingside attack is a conditional plan in some closed structures, not a promise attached to every King's Indian position."),
    "nimzo": (
        "Queen's Pawn / Nc3 setup", "Nimzo-Indian Defense", "Nimzo-Indian-Defense",
        "The defining ...Bb4 put pressure on the c3-knight, linking control of e4 with a possible structural trade.",
        "White develops toward a strong center while dealing with pressure on the c3-knight. The question is how to support the center and answer the bishop without wasting development time. If ...Bxc3 occurs, the recapture matters: a queen recapture does not create doubled pawns. The bishop pair is an option to evaluate after the actual exchange.",
        "Black uses ...Bb4 to restrain White's central ambitions and introduce a possible bishop-for-knight exchange. Giving up the bishop can change the pawn structure or gain time, but it isn't automatically favorable. Black should connect that decision with pressure on the center; a pin or threatened exchange alone doesn't establish a lasting advantage."),
    "grunfeld": (
        "Queen's Pawn center", "Grünfeld Defense", "Grunfeld-Defense",
        "Black challenges the center with ...d5; White's central space and Black's pressure must be evaluated together.",
        "White can gain a large center after exchanges, but those pawns also become targets. The task is to support or advance the center while developing the pieces behind it. A large pawn center is not automatically secure: each pawn move changes the lines available to Black's bishop and the timing of the counterplay.",
        "Black contests White's center with ...d5 rather than allowing an uncontested buildup. In Exchange structures, pressure against d4 often combines the fianchetto bishop with a prepared ...c5. That plan depends on the actual exchanges and piece placement. Black needs active pressure, not just the opening name, to compensate for White's central space."),
    "reti": (
        "Réti Opening", "Response to the Réti", "Reti-Opening",
        "White develops Nf3 before committing the central pawns, keeping several central structures available.",
        "The Réti starts with Nf3 and delays a full central pawn commitment. White can pressure the center with pieces and a later c4, but can also transpose by playing d4 or e4. The original move order is useful context, not a permanent identity: once the pawn structure changes, the plan should follow the position reached.",
        "Black's response helps decide which center will appear. A ...d5 setup, a ...c5 response, and a kingside fianchetto ask White different questions. Black should use the available central space without assuming White must remain in a Réti structure. Later pawn moves can turn the same game into a Queen's Gambit, Sicilian, or another family."),
    "english": (
        "English Opening", "Response to the English", "English-Opening",
        "White's c4 controls d5; Black's response determines whether the center is symmetrical or asymmetric.",
        "The English begins with c4, influencing d5 while keeping the central pawns flexible. White can choose a kingside fianchetto or a different piece setup, then decide when to commit d4 or e4. A later d4 can transpose into a Queen's Gambit or Indian structure, so the current position matters more than the first move alone.",
        "Black can answer with ...e5 for a reversed-Sicilian structure, with ...c5 for a symmetrical one, or with another setup. Those responses create different central questions, not a fixed winner. Black's development and pawn breaks should address White's actual arrangement; treating every English position as the same system can miss a transposition."),
    "open_game": (
        "King's Pawn Opening", "Open Game (...e5)", "Kings-Pawn-Opening",
        "Both sides stake a claim in the center with their e-pawns; development and the timing of d4 now matter.",
        "White's e4 meets a matching central claim from ...e5. Developing the pieces and deciding when to challenge with d4 are connected tasks: opening the center can help active pieces and expose an unready king. The game may become an Italian, Ruy Lopez, Scotch, Petrov, or another line, depending on the moves actually chosen.",
        "Black contests the center directly with ...e5 and must coordinate its defense with development. A knight move, a bishop move, or a central counterattack can change the character of the game. Black's objective is to answer White's concrete threats while bringing the pieces into play, rather than simply mirroring White's moves."),
}


def _family(name):
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    if "london system" in text or text.startswith(("jobava london", "rapport-jobava system")):
        return "london"
    for prefix, family in (("sicilian defense", "sicilian"), ("french defense", "french"),
                           ("caro-kann defense", "caro"), ("italian game", "italian"),
                           ("ruy lopez", "ruy"), ("scotch game", "scotch"),
                           ("petrov's defense", "petrov"), ("queen's gambit accepted", "qga"),
                           ("queen's gambit declined", "qgd"), ("semi-slav defense", "slav"),
                           ("slav defense", "slav"), ("queen's gambit", "qg"),
                           ("king's indian defense", "kid"), ("nimzo-indian defense", "nimzo"),
                           ("grunfeld defense", "grunfeld"), ("reti opening", "reti"),
                           ("zukertort opening", "reti"), ("english opening", "english")):
        if text.startswith(prefix):
            return family
    return None


def _actual_position(board, row):
    if (not isinstance(board, chess.Board) or not isinstance(row, dict) or board.chess960
            or not board.is_valid() or len(board.move_stack) >= MAX_PLIES
            or board.root().fen(en_passant="fen") != chess.STARTING_FEN):
        return None
    if row.get("fen_before") is not None and row["fen_before"] != board.fen(en_passant="fen"):
        return None
    if row.get("ply") is not None and row["ply"] != len(board.move_stack) + 1:
        return None
    replay = chess.Board()
    for move in board.move_stack:
        if replay.is_game_over(claim_draw=False) or move not in replay.legal_moves:
            return None
        replay.push(move)
    if replay.fen(en_passant="fen") != board.fen(en_passant="fen") or replay.is_game_over(claim_draw=False):
        return None
    try:
        move = chess.Move.from_uci(row.get("uci"))
    except (ValueError, TypeError, AttributeError):
        return None
    if move not in replay.legal_moves:
        return None
    replay.push(move)
    if row.get("fen_after") is not None and row["fen_after"] != replay.fen(en_passant="fen"):
        return None
    return replay


def _recognition(board, catalog):
    probe = board.copy(stack=True)
    while probe.move_stack:
        history = tuple(move.uci() for move in probe.move_stack)
        matches = [catalog.openings[index] for index in catalog.named.get(position_key(probe), ())]
        if matches:
            opening = min(matches, key=lambda item: (item.moves != history, len(item.moves), item.eco, item.name))
            return opening, {"name": opening.name, "eco": opening.eco,
                             "matched_ply": len(history), "match_type": "exact" if opening.moves == history else "transposition",
                             "scope": "current" if len(history) == len(board.move_stack) else "last_known"}
        probe.pop()
    return None, None


def _played_score(board, row):
    branch = row.get("played")
    if not isinstance(branch, dict) or branch.get("bound") != "exact":
        return None, "missing_or_bounded_score"
    pv = branch.get("pv_uci")
    if (branch.get("uci") != row.get("uci") or not isinstance(pv, (list, tuple))
            or not pv or len(pv) > 64 or pv[0] != row.get("uci")):
        return None, "wrong_score_root"
    frames = branch.get("pv_frames")
    if frames is not None and (not isinstance(frames, list) or len(frames) != len(pv) + 1):
        return None, "invalid_score_frames"
    replay = board.copy(stack=True)
    if frames is not None and (not isinstance(frames[0], dict) or frames[0].get("fen") != replay.fen(en_passant="fen")):
        return None, "invalid_score_frames"
    for index, uci in enumerate(pv, 1):
        try:
            move = chess.Move.from_uci(uci)
        except (TypeError, ValueError, AttributeError):
            return None, "invalid_score_pv"
        if replay.is_game_over(claim_draw=False) or move not in replay.legal_moves:
            return None, "invalid_score_pv"
        replay.push(move)
        if frames is not None and (not isinstance(frames[index], dict) or frames[index].get("fen") != replay.fen(en_passant="fen")):
            return None, "invalid_score_frames"
    if branch.get("mate_white") is not None:
        return None, "mate_score"
    cp, depth = branch.get("score_cp_white"), branch.get("depth")
    if type(cp) is not int or type(depth) is not int or depth < 0:
        return None, "missing_score_or_depth"
    return {"score_cp_white": cp, "depth": depth}, None


def _assessment(before, after, row):
    base = {"status": "unavailable", "side": None, "score_cp_white": None, "depth": None,
            "source": "played", "reason": None, "provisional": False}
    outcome = after.outcome(claim_draw=False)
    if outcome:
        if outcome.winner is not None:
            side = "white" if outcome.winner else "black"
            return {**base, "status": "checkmate", "side": side, "reason": "actual_checkmate"}, f"The actual move ends the game in checkmate for {side.title()}. There is no remaining opening contest to assess."
        reason = {chess.Termination.STALEMATE: "stalemate", chess.Termination.INSUFFICIENT_MATERIAL: "insufficient mating material",
                  chess.Termination.SEVENTYFIVE_MOVES: "the seventy-five-move rule",
                  chess.Termination.FIVEFOLD_REPETITION: "fivefold repetition"}.get(outcome.termination, "a draw rule")
        return {**base, "status": "draw", "reason": reason}, f"The actual position ends in a draw by {reason}. A numerical opening advantage no longer decides this game's result."
    score, reason = _played_score(before, row)
    if len(after.move_stack) < 12:
        return {**base, **(score or {}), "status": "too_early", "reason": "fewer_than_12_plies", "provisional": score is not None}, "It's too early to award either side the better start. The opening name explains the central choices, not who is winning. Development, king safety, and the next central decisions still have to be judged from the moves actually played."
    if row.get("classification") not in {"best", "good", "inaccuracy", "mistake", "blunder", "missed_mate", "allows_mate"}:
        return {**base, "reason": "uncertain_move_analysis"}, "The opening is recognizable, but this move's analysis is inconclusive. There isn't a reliable basis here to award either side the better start. Any later advantage must come from the position and moves played, not from a fixed ranking of these opening names."
    if score is None:
        return {**base, "reason": reason}, "The available analysis isn't reliable enough for an opening verdict. The named structure still helps explain each side's plans, but it doesn't establish an advantage. An exact evaluation of the actual move is needed before deciding who got the better start."
    if score["depth"] < 4:
        near_level = ", close to level" if abs(score["score_cp_white"]) < 50 else ""
        text = (f"The provisional estimate is {score['score_cp_white'] / 100:+.2f} on White's scale at depth {score['depth']}{near_level}. "
                "That search is too shallow to award either side the better start. The number describes the moves played, "
                "not a fixed advantage of one opening over another.")
        return {**base, **score, "reason": "shallow_score", "provisional": True}, text
    cp = score["score_cp_white"]
    if abs(cp) < 50:
        status, side = "balanced", None
        verdict = f"At depth {score['depth']}, Regicide sees a roughly balanced position: {cp / 100:+.2f} on White's scale."
    else:
        side = "white" if cp > 0 else "black"
        status = "slight_edge" if abs(cp) < 100 else "better_start"
        verdict = f"At depth {score['depth']}, Regicide {'slightly prefers' if status == 'slight_edge' else 'prefers'} {side.title()}'s position: {cp / 100:+.2f} on White's scale."
    text = verdict + " This is a provisional estimate from the moves played, not a fixed opening matchup result. It doesn't mean the game is won; a different central decision or tactical mistake can change the picture."
    return {**base, **score, "status": status, "side": side, "provisional": True}, text


def _center_observation(board):
    parts = []
    for color, name in ((chess.WHITE, "White"), (chess.BLACK, "Black")):
        squares = [chess.square_name(square) for square in (chess.D4, chess.E4, chess.D5, chess.E5)
                   if board.piece_at(square) == chess.Piece(chess.PAWN, color)]
        if squares:
            parts.append(name + " has " + ("a pawn" if len(squares) == 1 else "pawns") + " on " + ", ".join(squares))
    return " On the board, " + "; ".join(parts) + "." if parts else ""


def _pawn_history(board):
    """Trace the actual original pawns, not just familiar-looking squares."""
    replay = chess.Board()
    origins = {square: square for square in chess.SquareSet(replay.pawns)}
    facts = {"sicilian_trade": False, "open_sicilian": False, "french_trade": False, "french_exchange": False}
    for move in board.move_stack:
        color, piece = replay.turn, replay.piece_type_at(move.from_square)
        capture_square = move.to_square + ((-8 if color else 8) if replay.is_en_passant(move) else 0)
        captured_piece = replay.piece_type_at(capture_square)
        captured = origins.pop(capture_square, None)
        origin = origins.pop(move.from_square, None)
        if piece == chess.PAWN and not color and origin == chess.C7 and captured == chess.D2 and move.to_square == chess.D4:
            facts["sicilian_trade"] = True
        if (color and piece != chess.PAWN and captured_piece == chess.PAWN and captured == chess.C7
                and move.to_square == chess.D4 and facts["sicilian_trade"]):
            facts["open_sicilian"] = True
        if piece == chess.PAWN and color and origin == chess.E2 and captured == chess.D7 and move.to_square == chess.D5:
            facts["french_trade"] = True
        if (piece == chess.PAWN and not color and origin == chess.E7 and captured == chess.E2
                and move.to_square == chess.D5 and facts["french_trade"]):
            facts["french_exchange"] = True
        replay.push(move)
        if origin is not None and not move.promotion:
            origins[move.to_square] = origin
    facts["origins"] = origins
    return facts


def _structure_plan(family, opening, board):
    if family not in ("french", "sicilian"):
        return None
    facts = _pawn_history(board)
    if family == "sicilian" and facts["open_sicilian"]:
        return ("Open Sicilian", "White's d-pawn was exchanged for Black's c-pawn. The resulting activity and files matter more than the original first move.",
                "This game exchanged White's original d-pawn for Black's c-pawn, reaching an Open Sicilian structure. White can use the resulting lines for active development, but the central pieces can also become targets. The practical task is to connect that activity with king safety and support for the remaining center, rather than assuming an attack follows automatically.",
                "The central trade removed Black's original c-pawn and White's d-pawn. That can give Black a semi-open c-file if no friendly pawn now blocks it. Black needs to coordinate pressure on the center with development; the open structure gives both sides tactical chances, so a flank operation still depends on the actual position.")
    if (family == "sicilian" and "Closed" in opening.name
            and any(facts["origins"].get(square) == chess.D2 for square in (chess.D2, chess.D3))):
        return ("Closed Sicilian setup", "White has delayed d4 in this Closed Sicilian move order. That preserves options; it doesn't guarantee the center will stay closed.",
                "White has kept the original d-pawn behind d4 in this Closed Sicilian move order. Delaying d4 allows a buildup before deciding where to open play, but a later d4 can still change the structure. White should coordinate the pieces and judge the central break from the actual position, rather than treating the setup as an automatic kingside attack.",
                "Black's ...c5 challenge has not yet been met by the Open Sicilian d-pawn trade. Black can develop and choose an appropriate central response while watching White's buildup. The word 'Closed' describes the move order here, not a permanent promise: a later d4 or another pawn break can change which files and targets matter.")
    if family == "french" and facts["french_exchange"]:
        return ("French Exchange setup", "White exchanged on d5, and Black recaptured with the e-pawn. That changes the development problem from an Advance-style pawn chain.",
                "This game exchanged White's original e-pawn for Black's d-pawn, with Black's e-pawn recapturing on d5. The game entered through the French Exchange rather than the Advance move order. White's plans should follow the available files and piece development; copying an attack from another French variation without checking the current structure misses the point.",
                "Black's e-pawn recapture on d5 changed the original French center and removed the pawn from e6. The bishop's development problem and the available lines therefore differed from the Advance variation. Black should use the actual piece routes and central opportunities in the current position, rather than assume an Advance-style pawn chain.")
    origins = facts["origins"]
    if family == "french" and all(origins.get(square) == origin for square, origin in
                                  ((chess.D4, chess.D2), (chess.E5, chess.E2), (chess.D5, chess.D7), (chess.E6, chess.E7))):
        pressure = ("Black's pawn on c5 already attacks the base on d4. " if board.piece_at(chess.C5) == chess.Piece(chess.PAWN, chess.BLACK) else
                    "A prepared ...c5 is a thematic way to attack the base on d4. ")
        return ("French Advance center", "White's d4-e5 chain is present here. Black's counterplay targets its d4 base rather than accepting White's space without a challenge.",
                "White currently has the d4-e5 pawn chain that defines this Advance French structure. The advanced e-pawn gains space, while the d4 base needs support. White should finish development around that chain and watch how exchanges at its base would change the center; a kingside operation should follow the actual piece placement, not just the opening name.",
                pressure + "Black's e6-d5 chain meets White's central space, so pressure on the base is a concrete way to challenge it. The timing still depends on development and tactics. Opening the center while the king or pieces are unready can turn a thematic pawn break into a practical problem.")
    return None


def describe_opening(board_before, row):
    """Return actual-position opening context, or fail closed without a guess."""
    after = _actual_position(board_before, row)
    if after is None:
        return None
    try:
        catalog = _catalog()
        opening, recognition = _recognition(after, catalog)
    except (CatalogError, OSError, ValueError, TypeError, AttributeError, IndexError, KeyError):
        return None
    if opening is None:
        return None
    family = _family(opening.name)
    history = tuple(move.uci() for move in after.move_stack)
    # The base e4/e5 node is useful, but do not overwrite separately named
    # gambits or other families merely because their first two moves match.
    if family is None and opening.name == "King's Pawn Game" and history[:2] == ("e2e4", "e7e5"):
        family = "open_game"
    profile = PROFILES.get(family)
    assessment, balance = _assessment(board_before, after, row)
    sources = [catalog.source["repository_url"] + "/tree/" + catalog.source["commit"]]
    if opening.name_source_url:
        sources.append(opening.name_source_url)
    if profile:
        white_name, black_name, slug, idea, white_text, black_text = profile
        sources.append(SOURCE_ROOT + slug)
        if family == "qgd":
            sources.append(SOURCE_ROOT + "Queens-Gambit")
        elif family == "open_game":
            sources.append(SOURCE_ROOT + "Kings-Pawn-Opening-1...e5")
        if family == "london" and opening.name.startswith("Rapport-Jobava System"):
            white_name = "Rapport-Jobava System"
        if family == "slav" and opening.name.startswith("Semi-Slav Defense"):
            black_name = "Semi-Slav Defense"
            black_text = ("The Semi-Slav move order added ...e6 to a ...c6/...d5 setup, changing the c8-bishop's development problem. "
                          "Black's central breaks depend on the actual exchanges and piece activity. Keeping the structure solid is useful "
                          "only if Black can also bring the pieces into play; the early bishop option from a Slav cannot simply be assumed.")
        structure = _structure_plan(family, opening, after)
        if structure:
            white_name, idea, white_text, black_text = structure
        summary = idea
    else:
        white_name, black_name = "White setup", "Black response"
        summary = "This position has a recorded opening name, but no verified family-specific plan is attached to it here."
        white_text = f"The catalogue identifies {opening.name}. This is a name for the recorded move sequence or position, not evidence of an advantage. No detailed White plan is inferred from the name alone."
        black_text = "Black's position belongs to the same actual game. This opening is outside the detailed plan profiles, so a specific counter-plan isn't invented. The move analysis can still explain concrete threats and mistakes."
    if history[0] == "g1f3" and family not in (None, "reti"):
        destination = profile[1] if family in ('sicilian', 'french', 'caro', 'kid', 'nimzo', 'grunfeld') else profile[0]
        summary = f"The Réti move order led into {destination}. " + idea
        white_text = "The game began with Nf3, but the current family follows the later central choices. " + white_text
    elif recognition["match_type"] == "transposition":
        summary = "A different move order reached this opening position. " + summary
    if recognition["scope"] == "last_known":
        summary = f"Last recognized at ply {recognition['matched_ply']}: " + summary
    if len(summary.split()) > 40:
        # Keep the verified transition and provenance instead of truncating an
        # opening name or half of a sentence. Full plans remain below the card.
        summary = (("White began with Nf3; the opening family changed with the later central moves. "
                    f"The last named position was at ply {recognition['matched_ply']}.")
                   if history[0] == "g1f3" and profile and family != "reti" else
                   (f"The last recognized opening position was at ply {recognition['matched_ply']}. "
                    "Its plans are context for this game, not a verdict on the current position."))
    if assessment["status"] in ("checkmate", "draw"):
        summary = balance
    else:
        balance += _center_observation(after)
    move = chess.Move.from_uci(row["uci"])
    return {"title": "Opening: " + opening.name, "summary": summary,
            "white": {"name": white_name}, "black": {"name": black_name},
            "recognition": recognition, "sections": [
                {"id": "white", "title": "White's plan", "text": white_text},
                {"id": "black", "title": "Black's reply", "text": black_text},
                {"id": "balance", "title": "Who got the better start?", "text": balance}],
            "ply_index": len(after.move_stack), "fen": after.fen(en_passant="fen"),
            "focus_squares": [chess.square_name(move.from_square), chess.square_name(move.to_square)],
            "sources": list(dict.fromkeys(sources)), "assessment": assessment}
