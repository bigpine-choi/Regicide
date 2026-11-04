"""Local legal exchange evidence for a new non-pawn investment.

The historical module name is not a soundness claim. This screen observes
material offered after a move and optional legal recaptures on that square.
It proves neither compensation, best play, novelty nor any site's move grade.
"""
import math
import time

import chess

import sparring_material

POLICY = "regicide-sound-sacrifice-geometry-v1"
VALUES = sparring_material.VALUES
MIN_INVESTMENT_CP = 100
MAX_EVIDENCE_MS = 75


def evidence(board, move, *, deadline=None):
    started = time.monotonic()
    result = {"policy": POLICY, "eligible": False, "complete": True,
              "reason": "no_new_nonpawn_sacrifice", "uci": None, "offers": [],
              "investment_cp": 0, "before_fen": None, "history_uci": [],
              "basis": "local legal exchange geometry only; not soundness, compensation or a move grade"}
    if not isinstance(board, chess.Board) or not board.is_valid() or board.chess960:
        result["reason"] = "invalid_position"
        return result
    result.update(before_fen=board.fen(en_passant="fen"), history_uci=[m.uci() for m in board.move_stack])
    if board.is_check():
        result["reason"] = "in_check"
        return result
    if board.is_game_over(claim_draw=True) or board.legal_moves.count() <= 1:
        result["reason"] = "forced_or_terminal"
        return result
    try:
        move = chess.Move.from_uci(move) if isinstance(move, str) else move
        if not isinstance(move, chess.Move) or move not in board.legal_moves:
            raise ValueError("Illegal move")
        result["uci"] = move.uci()
    except (ValueError, TypeError):
        result["reason"] = "invalid_move"
        return result
    if deadline is not None and (type(deadline) not in (int, float) or not math.isfinite(deadline)):
        result.update(reason="invalid_deadline", complete=False, investment_cp=None)
        return result
    stop = min(started + MAX_EVIDENCE_MS / 1000, deadline) if deadline is not None else started + MAX_EVIDENCE_MS / 1000

    def expired():
        if time.monotonic() >= stop:
            result.update(eligible=False, complete=False, reason="geometry_deadline", investment_cp=None)
            return True
        return False

    if expired():
        return result
    owner = board.turn
    # Counterfactual opponent turn at the unchanged position establishes
    # whether exactly this legal capture was already available before the move.
    prior = board.copy(stack=False)
    prior.turn, prior.ep_square = not owner, None
    old_captures = {capture.uci() for capture in prior.legal_moves if prior.is_capture(capture)
                    and prior.piece_at(capture.to_square) is not None
                    and prior.piece_at(capture.to_square).color == owner
                    and VALUES[prior.piece_type_at(capture.to_square)] >= 300}
    immediate_gain = (100 if board.is_en_passant(move) else
                      VALUES.get(board.piece_type_at(move.to_square), 0) if board.is_capture(move) else 0)
    immediate_gain += VALUES[move.promotion] - 100 if move.promotion else 0
    after = board.copy(stack=True)
    after.push(move)
    prior_hanging = None
    for capture in list(after.legal_moves):
        if expired():
            return result
        victim = after.piece_at(capture.to_square)
        if (not after.is_capture(capture) or victim is None or victim.color != owner
                or VALUES[victim.piece_type] < 300):
            continue
        moved_bait = capture.to_square == move.to_square
        newly_exposed = capture.uci() not in old_captures
        if not moved_bait and not newly_exposed:
            continue
        if moved_bait and prior_hanging is None:
            # Relocating a piece that was already offered for a real net loss
            # does not create a new investment. Mere attack is insufficient:
            # every credit uses the same complete legal recapture tree.
            prior_hanging = False
            result["prior_moved_piece_exchanges"] = []
            for old_capture in list(prior.legal_moves):
                old_victim = prior.piece_at(move.from_square)
                if (old_capture.to_square != move.from_square or not prior.is_capture(old_capture)
                        or old_victim is None or VALUES[old_victim.piece_type] < 300):
                    continue
                remaining = int((stop - time.monotonic()) * 1000)
                if remaining < 1:
                    result.update(eligible=False, complete=False, reason="geometry_deadline", investment_cp=None)
                    return result
                old_after = prior.copy(stack=False)
                old_after.push(old_capture)
                try:
                    old_exchange = sparring_material.exchange_gain(old_after, move.from_square,
                        max_time_ms=min(remaining, sparring_material.MAX_TIME_MS))
                except Exception as error:
                    result.update(eligible=False, complete=False, reason="exchange_failed", investment_cp=None,
                                  error=type(error).__name__)
                    return result
                old_promotion = VALUES[old_capture.promotion] - 100 if old_capture.promotion else 0
                old_investment = VALUES[old_victim.piece_type] + old_promotion - old_exchange["gain_cp"]
                result["prior_moved_piece_exchanges"].append({"capture_uci": old_capture.uci(),
                    "net_investment_cp": old_investment, "recapture_exchange": old_exchange})
                if not old_exchange["complete"]:
                    result.update(eligible=False, complete=False, reason="exchange_incomplete", investment_cp=None)
                    return result
                prior_hanging = prior_hanging or old_investment >= MIN_INVESTMENT_CP
            result["moved_piece_already_hanging"] = prior_hanging
        if moved_bait and prior_hanging:
            continue
        remaining = int((stop - time.monotonic()) * 1000)
        if remaining < 1:
            result.update(eligible=False, complete=False, reason="geometry_deadline", investment_cp=None)
            return result
        reply = after.copy(stack=True)
        reply.push(capture)
        try:
            exchange = sparring_material.exchange_gain(reply, capture.to_square,
                max_time_ms=min(remaining, sparring_material.MAX_TIME_MS))
        except Exception as error:
            result.update(eligible=False, complete=False, reason="exchange_failed", investment_cp=None,
                          error=type(error).__name__)
            return result
        promotion_gain = VALUES[capture.promotion] - 100 if capture.promotion else 0
        investment = VALUES[victim.piece_type] + promotion_gain - immediate_gain - exchange["gain_cp"]
        eligible = exchange["complete"] and investment >= MIN_INVESTMENT_CP
        result["offers"].append({"capture_uci": capture.uci(), "victim_square": chess.square_name(capture.to_square),
            "victim_piece": chess.piece_name(victim.piece_type), "victim_cp": VALUES[victim.piece_type],
            "moved_piece_is_bait": moved_bait, "newly_exposed": newly_exposed,
            "candidate_immediate_gain_cp": immediate_gain, "opponent_promotion_gain_cp": promotion_gain,
            "recapture_exchange": exchange, "net_investment_cp": investment,
            "eligible": eligible, "reason": "new_nonpawn_investment" if eligible else "no_net_investment"})
        if not exchange["complete"]:
            result.update(eligible=False, complete=False, reason="exchange_incomplete", investment_cp=None)
            return result
    if expired():
        return result
    qualified = [offer for offer in result["offers"] if offer["eligible"]]
    if qualified:
        result.update(eligible=True, reason="new_nonpawn_investment",
                      investment_cp=max(offer["net_investment_cp"] for offer in qualified))
    result["elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
    return result
