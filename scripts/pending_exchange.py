"""Recognize one declined recapture of a just-captured equal-value piece.

This does not permit creating a new hanging piece. Only the original capturing
piece, still on its original capture square, can complete that exact exchange.
"""
import copy

import chess

import sparring_play


def naturalness(board, move):
    strict = sparring_play.naturalness(board, move)
    result = copy.deepcopy(strict)
    result["pending_exchange"] = None
    if (strict.get("admitted") or strict.get("reason") != "obvious_material_donation"
            or len(board.move_stack) < 2 or len(board.move_stack) >= 40
            or board.root().fen(en_passant="fen") != chess.STARTING_FEN
            or board.is_check() or board.is_capture(move) or move.promotion or board.gives_check(move)):
        return result
    before = board.copy(stack=True)
    declined = before.pop()
    captured = before.pop()
    if before.turn != board.turn or captured.promotion or not before.is_capture(captured):
        return result
    piece = before.piece_at(captured.from_square)
    victim = before.piece_at(captured.to_square)
    if (piece is None or victim is None or piece.color != board.turn
            or piece.piece_type in (chess.PAWN, chess.KING)
            or sparring_play.VALUES[victim.piece_type] < sparring_play.VALUES[piece.piece_type]):
        return result
    before.push(captured)
    if before.is_capture(declined) or declined.promotion:
        return result
    square = captured.to_square
    original_replies = {reply.uci() for reply in before.legal_moves
                        if reply.to_square == square and before.is_capture(reply)}
    if not original_replies or board.piece_at(square) != piece or move.from_square == square:
        return result
    after = board.copy(stack=True)
    after.push(move)
    if after.piece_at(square) != piece or after.is_game_over(claim_draw=True):
        return result
    # A recapture must already be legal before the candidate. The opponent's
    # intervening quiet move may relocate an attacker or uncover another one;
    # the candidate itself must not create that new attack or lose a pin.
    existing = board.copy(stack=False)
    existing.turn = not board.turn
    existing.ep_square = None
    existing_replies = {reply.uci() for reply in existing.legal_moves
                        if reply.to_square == square and existing.is_capture(reply)}
    credit = sparring_play.VALUES[piece.piece_type]
    rows = strict.get("vulnerable_captures", [])
    if not rows or any(row["reply_uci"] not in existing_replies
                       or chess.Move.from_uci(row["reply_uci"]).to_square != square
                       or row["victim_cp"] != credit or row["net_material_cp"] < -credit
                       or row.get("recapture_exchange", {}).get("complete") is not True
                       for row in rows):
        return result
    proof = {"kind": "pending_equal_exchange", "capture_uci": captured.uci(),
             "declined_reply_uci": declined.uci(), "capture_ply": len(board.move_stack) - 1,
             "piece_square": chess.square_name(square), "piece": piece.symbol(),
             "captured_value_cp": sparring_play.VALUES[victim.piece_type],
             "maximum_exchange_credit_cp": credit,
             "original_legal_recaptures": sorted(original_replies),
             "existing_legal_recaptures": sorted(existing_replies),
             "permitted_replies": sorted(row["reply_uci"] for row in rows),
             "opponent_changed_recapture": sorted(row["reply_uci"] for row in rows
                                                   if row["reply_uci"] not in original_replies),
             "scope": "one immediately delayed equal exchange; attack predates candidate; no new material concession"}
    result.update(admitted=True, reason="pending_equal_exchange", pending_exchange=proof,
                  strict_naturalness=copy.deepcopy(strict))
    return result
