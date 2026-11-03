"""Bounded optional legal capture exchanges on one square.

This is a local material screen, not a tactical search or an evaluation score.
An incomplete tree supplies no compensation credit at all.
"""
import time

import chess

VALUES = {chess.PAWN: 100, chess.KNIGHT: 300, chess.BISHOP: 300,
          chess.ROOK: 500, chess.QUEEN: 900, chess.KING: 0}
MAX_NODES = 128
HARD_MAX_NODES = 1024
MAX_TIME_MS = 25
HARD_MAX_TIME_MS = 100


class _Exhausted(Exception):
    pass


def exchange_gain(board, square, *, max_nodes=MAX_NODES, max_time_ms=MAX_TIME_MS):
    """Best optional material gain from legal captures, including re-recaptures.

    A cutoff supplies zero credit, never optimistic compensation. Each recursive
    capture removes a piece, so the complete tree also has a finite natural end.
    """
    if not isinstance(board, chess.Board) or not board.is_valid():
        raise ValueError("A valid chess position is required")
    if type(square) is not int or not 0 <= square < 64:
        raise ValueError("Exchange square must be between 0 and 63")
    if type(max_nodes) is not int or not 1 <= max_nodes <= HARD_MAX_NODES:
        raise ValueError("Exchange node budget is outside its hard limit")
    if type(max_time_ms) is not int or not 1 <= max_time_ms <= HARD_MAX_TIME_MS:
        raise ValueError("Exchange time budget is outside its hard limit")
    started = time.monotonic()
    deadline = started + max_time_ms / 1000
    nodes, cache_hits = 0, 0
    cache, root_captures = {}, []

    def budget():
        if time.monotonic() >= deadline:
            raise _Exhausted("exchange_time_cap")

    def read(position, depth=0):
        nonlocal nodes, cache_hits
        budget()
        if nodes >= max_nodes:
            raise _Exhausted("exchange_node_cap")
        nodes += 1
        key = position.fen(en_passant="fen")
        if key in cache:
            cache_hits += 1
            return cache[key]
        best = 0
        # legal_moves enforces pins, check evasion and king safety. Every
        # branch captures a piece; promotion changes value, not piece count.
        for move in position.legal_moves:
            budget()
            if move.to_square != square or not position.is_capture(move):
                continue
            victim = position.piece_at(square)
            captured = 100 if position.is_en_passant(move) else VALUES[victim.piece_type] if victim else 0
            promotion = VALUES[move.promotion] - 100 if move.promotion else 0
            if depth == 0:
                root_captures.append(move.uci())
            position.push(move)
            try:
                gain = captured + promotion - read(position, depth + 1)
            finally:
                position.pop()
            best = max(best, gain)
        cache[key] = best
        return best

    try:
        # Exchange legality depends on the present position. The caller's
        # historical stack is neither copied nor changed by this local screen.
        gain = read(board.copy(stack=False))
        budget()
        reason = None
    except _Exhausted as exc:
        # Propagate failure to the top: returning zero only at a leaf would
        # create an optimistic captured_value - 0 in its parent.
        gain, reason = 0, str(exc)
    complete = reason is None
    result = {"gain_cp": gain, "complete": complete,
              "status": "complete" if complete else "exhausted", "exhausted": not complete,
              "nodes": nodes, "node_cap": max_nodes, "time_cap_ms": max_time_ms,
              "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
              "cache_entries": len(cache), "cache_hits": cache_hits,
              "root_captures_uci": root_captures}
    if reason is not None:
        result["reason"] = reason
    return result
