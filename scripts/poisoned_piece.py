"""Bounded tactical evidence for a piece that is dangerous to capture.

This helper proposes legal offers; it cannot choose a game move or start an
engine. The caller records the offer's cost under accurate refusal separately:
an offer can be unsound while its tempting capture is a forced tactical loss.
The capture is hypothetical opponent greed, never an assumed later error: the
proof below quantifies over every legal defense after that capture.
"""

import time

import chess
import chess.engine

VALUES = {chess.PAWN: 100, chess.KNIGHT: 300, chess.BISHOP: 300,
          chess.ROOK: 500, chess.QUEEN: 900, chess.KING: 0}
MIN_OFFER_CP = 100
MIN_NET_GAIN_CP = 150
MIN_DEPTH = 4
MAX_PROOF_NODES = 4000
DEFAULT_PROOF_SECONDS = .15


def _valuable_captures(board, owner):
    result = []
    for move in board.legal_moves:
        victim = board.piece_at(move.to_square)
        if victim and victim.color == owner and VALUES[victim.piece_type] >= MIN_OFFER_CP:
            result.append((move, victim))
    return result


def _opened_slider_rays(before_capture, after_capture, capture, owner):
    """Describe attacks uncovered by the greedy capturer vacating its square."""
    rays = []
    for origin, piece in after_capture.piece_map().items():
        if piece.color != owner or piece.piece_type not in (chess.BISHOP, chess.ROOK, chess.QUEEN):
            continue
        for target in after_capture.attacks(origin):
            victim = after_capture.piece_at(target)
            if (victim is None or victim.color == owner or VALUES[victim.piece_type] < 500
                    or target in before_capture.attacks(origin)
                    or not chess.between(origin, target) & chess.BB_SQUARES[capture.from_square]):
                continue
            rays.append({"slider_square": chess.square_name(origin),
                         "slider_piece": chess.piece_name(piece.piece_type),
                         "vacated_square": chess.square_name(capture.from_square),
                         "target_square": chess.square_name(target),
                         "target_piece": chess.piece_name(victim.piece_type),
                         "target_cp": VALUES[victim.piece_type],
                         "ray_length": chess.square_distance(origin, target)})
    return rays


def enumerate_offers(board, baseline_move=None, max_candidates=2, max_captures=2):
    """Return at most max_candidates offer/capture branches, baseline first.

    The offered piece may differ from the moved piece: moving a pinned knight
    can expose the queen behind it. Quiet equal exchanges are low-priority
    hypotheses and still need native evaluation plus the exact tactical proof.
    """
    if (max_candidates <= 0 or max_captures <= 0 or not board.is_valid()
            or board.is_check() or board.is_game_over(claim_draw=True)):
        return []
    owner = board.turn
    prior = board.copy(stack=False)
    prior.turn, prior.ep_square = not owner, None
    old_captures = {move.uci() for move, _ in _valuable_captures(prior, owner)}
    baseline_uci = baseline_move.uci() if isinstance(baseline_move, chess.Move) else baseline_move
    branches = []
    for move in list(board.legal_moves):
        after = board.copy(stack=True)
        after.push(move)
        captures = sorted(_valuable_captures(after, owner),
                          key=lambda item: (item[0].uci() in old_captures,
                                            -VALUES[item[1].piece_type], item[0].uci()))
        accepted_captures = 0
        for capture, victim in captures:
            attacker = after.piece_at(capture.from_square)
            captured = after.copy(stack=True)
            captured.push(capture)
            recaptures = [reply.uci() for reply in captured.legal_moves
                          if reply.to_square == capture.to_square and captured.is_capture(reply)]
            apparent_gain = VALUES[victim.piece_type] - (VALUES[attacker.piece_type] if recaptures else 0)
            if apparent_gain <= 0:
                continue
            rays = _opened_slider_rays(after, captured, capture, owner)
            newly_exposed = capture.uci() not in old_captures
            moved_piece_is_bait = capture.to_square == move.to_square
            branches.append({"uci": move.uci(), "capture_uci": capture.uci(),
                             "offered_piece": chess.piece_name(victim.piece_type),
                             "offered_cp": VALUES[victim.piece_type],
                             "capturing_piece": chess.piece_name(attacker.piece_type),
                             "newly_exposed": newly_exposed,
                             "moved_piece_is_bait": moved_piece_is_bait,
                             "offer_is_capture": board.is_capture(move),
                             "offer_is_check": after.is_check(),
                             "apparent_capture_gain_cp": apparent_gain,
                             "opened_slider_rays": rays,
                             "attraction_score": apparent_gain + 500 * bool(rays)
                             + 100 * newly_exposed + 100 * (not moved_piece_is_bait),
                             "immediate_recaptures": recaptures,
                             "reason": "legal_capture_of_offered_piece"})
            accepted_captures += 1
            if accepted_captures >= max_captures:
                break
    branches.sort(key=lambda row: (row["uci"] != baseline_uci, -row["attraction_score"],
                                   not row["newly_exposed"], -row["offered_cp"], not row["offer_is_capture"],
                                   not row["offer_is_check"], row["uci"], row["capture_uci"]))
    return branches[:max_candidates]


def _material(board, owner):
    return sum(value * (len(board.pieces(kind, owner)) - len(board.pieces(kind, not owner)))
               for kind, value in VALUES.items())


class _BudgetExpired(Exception):
    pass


def _forced_result(board, owner, target_material, *, mate_only, max_plies,
                   max_nodes, deadline, preferred):
    """Checks/captures and the native PV's quiet moves; ALL defender replies.

    Material success is checked only after the defender got a reply. It is a
    bounded tactical gain, not a claim that no long-term counterplay exists.
    """
    nodes = 0

    def visit(position, remaining, path=()):
        nonlocal nodes
        nodes += 1
        if nodes > max_nodes or time.monotonic() >= deadline:
            raise _BudgetExpired
        if position.is_checkmate():
            return position.turn != owner, [], 0, 0
        if position.is_game_over(claim_draw=True):
            return False, [], 0, 0
        if (not mate_only and position.turn == owner and not position.is_check()
                and _material(position, owner) >= target_material):
            return True, [], 0, 0
        if remaining <= 0:
            return False, [], 0, 0
        legal = list(position.legal_moves)
        if position.turn == owner:
            choices = []
            expected = (preferred[len(path)] if len(path) < len(preferred)
                        and tuple(preferred[:len(path)]) == path else None)
            for move in legal:
                checking = position.gives_check(move)
                if checking or (not mate_only and position.is_capture(move)) or move.uci() == expected:
                    choices.append((move, checking))
            choices.sort(key=lambda item: (item[0].uci() != expected,
                                           not item[1], item[0].uci()))
            for move, _ in choices:
                position.push(move)
                won, witness, earliest, latest = visit(position, remaining - 1, (*path, move.uci()))
                position.pop()
                if won:
                    return True, [move.uci(), *witness], earliest + 1, latest + 1
            return False, [], 0, 0
        witness, earliest, latest = [], None, -1
        for move in legal:
            position.push(move)
            won, continuation, child_min, child_max = visit(position, remaining - 1, (*path, move.uci()))
            position.pop()
            if not won:
                return False, [], 0, 0
            earliest = child_min if earliest is None else min(earliest, child_min)
            if child_max > latest:
                witness = [move.uci(), *continuation]
                latest = child_max
        return bool(legal), witness, earliest or 0, max(0, latest)

    try:
        proven, witness, earliest, latest = visit(board.copy(stack=True), max_plies)
        return {"proven": proven, "nodes": nodes, "budget_exhausted": False,
                "witness_uci": witness, "minimum_payoff_moves": earliest,
                "maximum_payoff_moves": latest}
    except _BudgetExpired:
        return {"proven": False, "nodes": nodes, "budget_exhausted": True,
                "witness_uci": [], "minimum_payoff_moves": None, "maximum_payoff_moves": None}


def validate_capture_branch(before, offer_uci, capture_uci, info, engine_color=None,
                            max_plies=8, *, deadline_monotonic=None, max_nodes=MAX_PROOF_NODES):
    """Check evidence searched AFTER both the offer and the tempting capture.

    A native exact score and legal short PV are necessary, insufficient
    alone: an independent bounded proof must defeat every subsequent defense.
    This proves only the consequence of accepting the bait. It says nothing
    about whether the offer survives accurate refusal or improves best play.
    """
    result = {"verified": False, "kind": None, "reason": "invalid_branch",
              "uci": offer_uci, "capture_uci": capture_uci, "pv_uci": [],
              "proof_nodes": 0, "max_plies": max_plies}
    owner = before.turn if engine_color is None else engine_color
    if owner != before.turn or not before.is_valid() or max_plies < 1 or max_plies > 8:
        return result
    board = before.copy(stack=True)
    try:
        offer, capture = chess.Move.from_uci(offer_uci), chess.Move.from_uci(capture_uci)
        if offer not in board.legal_moves:
            return result
        board.push(offer)
        victim = board.piece_at(capture.to_square)
        if (capture not in board.legal_moves or victim is None or victim.color != owner
                or VALUES[victim.piece_type] < MIN_OFFER_CP):
            return result
        before_capture = board.copy(stack=True)
        board.push(capture)
    except (TypeError, ValueError):
        return result
    result.update(after_capture_fen=board.fen(en_passant="fen"), offered_cp=VALUES[victim.piece_type])
    capturer = board.piece_at(capture.to_square)
    can_recapture = any(move.to_square == capture.to_square and board.is_capture(move)
                       for move in board.legal_moves)
    result["apparent_capture_gain_cp"] = (VALUES[victim.piece_type]
                                          - (VALUES[capturer.piece_type] if can_recapture else 0))
    result["opened_slider_rays"] = _opened_slider_rays(before_capture, board, capture, owner)
    result["reason"] = "unreliable_engine_evidence"
    if (not isinstance(info, dict) or type(info.get("depth")) is not int or info["depth"] < MIN_DEPTH
            or info.get("lowerbound") or info.get("upperbound")
            or not isinstance(info.get("score"), chess.engine.PovScore)
            or info["score"].turn != board.turn):
        return result
    pv = info.get("pv")
    if not isinstance(pv, (tuple, list)) or not pv:
        return result
    replay = board.copy(stack=True)
    for move in pv[:max_plies]:
        if move not in replay.legal_moves:
            result["reason"] = "illegal_pv"
            return result
        replay.push(move)
    result["pv_uci"] = [move.uci() for move in pv[:max_plies]]
    score = info["score"].pov(owner)
    mate, cp = score.mate(), score.score()
    result.update(engine_depth=info["depth"], engine_mate=mate, engine_cp=cp)
    before_material, captured_material = _material(before, owner), _material(board, owner)
    bait_value = VALUES[victim.piece_type]
    # Require profitable bait: a knight (300) opening a rook (500) capture
    # qualifies, while a simple equal trade cannot. A defender must receive a
    # reply before the material threshold is accepted by the proof.
    target_material = max(before_material + MIN_NET_GAIN_CP,
                          captured_material + bait_value + MIN_NET_GAIN_CP)
    result.update(required_final_delta_cp=target_material - before_material,
                  required_countergain_cp=bait_value + MIN_NET_GAIN_CP,
                  pv_final_delta_cp=_material(replay, owner) - before_material,
                  pv_countergain_cp=_material(replay, owner) - captured_material)
    mate_claim = mate is not None and 0 < mate <= 4
    material_claim = (cp is not None
                      and _material(replay, owner) >= target_material)
    if not mate_claim and not material_claim:
        result["reason"] = "no_short_tactical_gain"
        return result
    deadline = (time.monotonic() + DEFAULT_PROOF_SECONDS
                if deadline_monotonic is None else deadline_monotonic)
    proof = _forced_result(board, owner, target_material, mate_only=mate_claim,
                           max_plies=max_plies, max_nodes=max_nodes, deadline=deadline,
                           preferred=[move.uci() for move in pv[:max_plies]])
    result.update(proof_nodes=proof["nodes"], proof_witness_uci=proof["witness_uci"],
                  proof_budget_exhausted=proof["budget_exhausted"])
    if proof["proven"]:
        result.update(verified=True, kind="forced_mate" if mate_claim else "forced_material_gain",
                      reason="all_legal_defenses_verified",
                      minimum_payoff_moves=proof["minimum_payoff_moves"],
                      maximum_payoff_moves=proof["maximum_payoff_moves"],
                      delayed_payoff=proof["minimum_payoff_moves"] >= 2,
                      deep_combination=proof["minimum_payoff_moves"] >= 3,
                      payoff_scope="bounds for the proven strategy, not globally shortest mate")
    else:
        result["reason"] = "proof_budget_exhausted" if proof["budget_exhausted"] else "not_forcing"
    return result
