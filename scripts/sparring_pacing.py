"""Replay-derived delays after major losses and graduated resource protection.

These are explicit practice heuristics, never a dead-position adjudicator.
Capture/promotion runs are settled as a unit so equal exchanges do not create
fake compensation debt. No opponent identity, external score or future move
is used.
"""
import math

import chess

VALUES = {chess.PAWN: 100, chess.KNIGHT: 300, chess.BISHOP: 300,
          chess.ROOK: 500, chess.QUEEN: 900, chess.KING: 0}
CLOSE_PLIES = 30
FINISH_PLIES = 98
MAJOR_LOSS_CP = 400


def _positions(board):
    current = board.root()
    positions = [current.copy(stack=False)]
    for move in board.move_stack:
        if move not in current.legal_moves:
            return []
        current.push(move)
        positions.append(current.copy(stack=False))
    return positions if current.fen(en_passant="fen") == board.fen(en_passant="fen") else []


def _balance(board, color):
    return sum(VALUES[p.piece_type] * (1 if p.color == color else -1)
               for p in board.piece_map().values())


def _own_promotion_gain(board, positions, start, end, color):
    return sum(VALUES[move.promotion] - VALUES[chess.PAWN]
               for index, move in enumerate(board.move_stack[start:end], start)
               if move.promotion and positions[index].turn == color)


def _resources(board, color):
    pieces = [p for p in board.piece_map().values() if p.piece_type not in (chess.PAWN, chess.KING)]
    own = [p for p in pieces if p.color == color]
    pawns = chess.popcount(board.pawns)
    queens = sum(p.piece_type == chess.QUEEN for p in pieces)
    total = sum(VALUES[p.piece_type] for p in pieces)
    # A warning blocks material compensation, but still permits quiet positional
    # concessions. Full suppression is reserved for much simpler endings.
    caution = total <= 2000 or len(own) <= 2
    fragile = False
    if pawns == 0:
        kinds = [p.piece_type for p in own]
        fragile = not kinds or (len(kinds) == 1 and kinds[0] in (chess.BISHOP, chess.KNIGHT))
        fragile |= kinds == [chess.KNIGHT, chess.KNIGHT]
        if kinds and all(kind == chess.BISHOP for kind in kinds):
            colors = {(chess.square_rank(square) + chess.square_file(square)) % 2
                      for square in board.pieces(chess.BISHOP, color)}
            fragile |= len(colors) == 1
    reason = ("pawn_endgame" if not pieces else
              "simple_low_pawn_endgame" if len(pieces) <= 2 and pawns <= 4 else
              "fragile_pawnless_mating_resources" if fragile else None)
    return {"active": reason is not None, "reason": reason,
            "material_concessions_blocked": caution or reason is not None,
            "nonpawn_cp_total": total, "nonpawn_pieces_total": len(pieces),
            "own_nonpawn_pieces": len(own), "pawns_total": pawns, "queens": queens}


def resource_guard(board):
    """Only a critical pre-16 ending latches; the caution tier is reversible."""
    current = _resources(board, board.turn)
    positions = _positions(board)
    trigger = next((ply for ply, position in enumerate(positions[:CLOSE_PLIES + 1])
                    if _resources(position, board.turn)["active"]), None)
    if trigger is not None:
        current.update(active=True, reason=_resources(positions[trigger], board.turn)["reason"],
                       material_concessions_blocked=True)
    else:
        # After 15, the fixed ramp remains the normal policy. No new early
        # stop is inferred retroactively; material caution still applies.
        current["active"] = False
        current["reason"] = None
    current.update(trigger_ply=trigger,
                   scope="practice resource heuristic; not a proof of mate, draw or forced victory")
    return current


def current_resources(board, color):
    """Inspect a candidate reply without latching unrelated historical state."""
    return _resources(board, color)


def _episodes(board, positions):
    color, blocks, block = board.turn, [], None
    for index, move in enumerate(board.move_stack):
        before, after = positions[index], positions[index + 1]
        material_move = before.is_capture(move) or bool(move.promotion)
        if material_move:
            if block is None:
                block = {"start_ply": index, "major_captures": []}
            victim = before.piece_at(move.to_square)
            if before.turn == color and victim and victim.piece_type in (chess.ROOK, chess.QUEEN):
                block["major_captures"].append(index + 1)
        elif block is not None:
            block["settled_ply"] = index + 1
            block["gain_cp"] = (_balance(after, color) - _balance(positions[block["start_ply"]], color)
                                - _own_promotion_gain(board, positions, block["start_ply"], index + 1, color))
            blocks.append(block)
            block = None
    if block is not None:
        block["settled_ply"] = None
        block["gain_cp"] = (_balance(board, color) - _balance(positions[block["start_ply"]], color)
                            - _own_promotion_gain(board, positions, block["start_ply"], len(board.move_stack), color))
        blocks.append(block)
    episodes = []
    for item in blocks:
        if not item["major_captures"] or item["gain_cp"] < MAJOR_LOSS_CP:
            continue
        event = item["major_captures"][0]
        if event > CLOSE_PLIES:
            continue
        settled = item["settled_ply"]
        start = None if settled is None else settled + int(positions[settled].turn != color)
        if episodes and episodes[-1]["start_root_ply"] is not None:
            earlier = episodes[-1]
            candidate_root = len(board.move_stack) if start is None else start
            if candidate_root <= earlier["start_root_ply"] + 6:
                earlier["major_captures"].extend(item["major_captures"])
                continue
        episodes.append({**item, "event_ply": event, "start_root_ply": start})
    return episodes


def derive(board, decisions, baseline_cp, *, ramp_fraction=0.0, persist_recovery=True):
    """Keep the concealment delay; carry unfinished recovery through move 15.

    New and unfinished material returns both stop before either side's move 16.
    Before then,
    failure to find a safe return does not consume the final recovery stage.
    ``persist_recovery=False`` uses a four-turn window within this close phase.
    """
    inactive = {"active": False, "reason": "no_recent_major_loss", "event_ply": None,
                "stage": None, "debt_cp": 0, "anchor_cp": None, "target_cp": None,
                "upper_cp": None, "allow_material_return": False, "return_cap_cp": 0,
                "scope": "delayed practice recovery; no promise of invisibility or band restoration"}
    if type(baseline_cp) not in (int, float) or not math.isfinite(baseline_cp):
        return {**inactive, "reason": "no_finite_baseline"}
    positions = _positions(board)
    if not positions or len(board.move_stack) >= FINISH_PLIES:
        return {**inactive, "reason": "invalid_history_or_full_strength"}
    if len(board.move_stack) >= CLOSE_PLIES:
        return {**inactive, "reason": "close_phase_finished"}
    episodes = _episodes(board, positions)
    if not episodes:
        return inactive
    episode = episodes[-1]
    root_ply, start = len(board.move_stack), episode["start_root_ply"]
    elapsed_stage = 0 if start is None else (root_ply - start) // 2 + 1
    persistent = persist_recovery and root_ply < CLOSE_PLIES and elapsed_stage > 4
    stage = min(4, elapsed_stage) if persistent else elapsed_stage
    if stage < 0 or stage > 4:
        return {**inactive, "reason": "recovery_window_finished"}
    if persist_recovery and -100 <= baseline_cp <= 100:
        return {**inactive, "reason": "strict_band_recovered"}
    debt = (_balance(board, board.turn) - _balance(positions[episode["start_ply"]], board.turn)
            - _own_promotion_gain(board, positions, episode["start_ply"], root_ply, board.turn))
    if debt <= 0:
        return {**inactive, "reason": "material_compensation_already_received"}
    anchor = max(100, float(baseline_cp))
    history = [move.uci() for move in board.move_stack]
    rows = decisions.values() if isinstance(decisions, dict) else decisions if isinstance(decisions, (list, tuple)) else []
    receipts = []
    for row in rows:
        if not isinstance(row, dict) or type(row.get("ply")) is not int:
            continue
        ply, pacing = row["ply"], row.get("pacing") or {}
        if (not isinstance(pacing, dict) or pacing.get("event_ply") != episode["event_ply"]
                or not 1 <= ply <= root_ply or row.get("history_uci") != history[:ply - 1]
                or row.get("selected_uci") != history[ply - 1]
                or row.get("before_fen") != positions[ply - 1].fen(en_passant="fen")
                or type(pacing.get("anchor_cp")) not in (int, float)
                or not math.isfinite(pacing["anchor_cp"])):
            continue
        receipts.append((ply, pacing["anchor_cp"]))
    if receipts:
        anchor = max(100, min(receipts)[1])
    progress = max(0., (stage - 1) / 3)
    upper = 100 + (anchor - 100) * (1 - progress)
    target = max(30, anchor - 40) if stage <= 1 else 30 + (anchor - 30) * (1 - progress)
    fade = min(1., max(0., ramp_fraction))
    target = 30 + (target - 30) * (1 - fade)
    upper = 100 + (upper - 100) * (1 - fade)
    guard = resource_guard(board)
    return {**inactive, "active": True,
            "reason": ("persistent_major_loss_recovery" if persistent else
                       "settle_exchange" if stage == 0 else "staged_major_loss_recovery"),
            "event_ply": episode["event_ply"], "exchange_start_ply": episode["start_ply"],
            "settled_ply": episode["settled_ply"], "start_root_ply": start,
            "stage": stage, "elapsed_stage": elapsed_stage, "persistent": persistent,
            "debt_cp": debt, "anchor_cp": round(anchor, 3),
            "target_cp": round(target, 3), "upper_cp": round(upper, 3),
            "allow_material_return": stage in (3, 4) and not guard["material_concessions_blocked"],
            "return_cap_cp": min(400, debt), "ramp_fraction": round(fade, 4),
            "material_concessions_blocked": guard["material_concessions_blocked"]}
