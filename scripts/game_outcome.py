"""Automatic results for local play, based on positions actually reached.

FIDE also permits a player to claim a draw by declaring an intended move.
That entitlement is useful when assessing candidate moves, but must not end
an interactive game before the move is chosen and played.  Online practice
automatically draws on the completed third repetition or 50-move condition.
"""
import chess


DRAW_POLICY = "automatic draw on completed third repetition or 50-move condition"
LEGACY_DRAW_POLICY = "always claim available threefold/50-move draws"


def automatic_outcome(board):
    """Return a result only for terminal conditions on the current board.

    Delegate compulsory endings first, preserving checkmate priority and
    fivefold/75-move boundaries.  Keep full move history when passing a board:
    FEN alone cannot establish repetition.  The board is never modified.
    """
    outcome = board.outcome(claim_draw=False)
    if outcome is not None:
        return outcome
    if board.is_fifty_moves():
        return chess.Outcome(chess.Termination.FIFTY_MOVES, None)
    if board.is_repetition(3):
        return chess.Outcome(chess.Termination.THREEFOLD_REPETITION, None)
    return None
