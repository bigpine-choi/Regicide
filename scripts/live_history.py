"""Read-only frames of a private active-game board copy for live browsing."""
import chess


def live_history(board, *, game_id, revision, human_color):
    """Use the real complete history; never infer special moves in a client."""
    replay = board.root()
    moves, frames = [], []

    def frame():
        return {"game_id": game_id, "revision": revision, "ply": len(moves),
                "fen": replay.fen(en_passant="fen"),
                "board": {chess.square_name(square): piece.symbol()
                          for square, piece in replay.piece_map().items()},
                "turn": "white" if replay.turn else "black",
                "human_color": "white" if human_color else "black",
                "in_check": replay.is_check(),
                "last_move": moves[-1]["uci"] if moves else None,
                "human_to_move": False, "engine_to_move": False,
                "legal_moves": [], "hint_legal_moves": []}

    frames.append(frame())
    for ply, move in enumerate(board.move_stack, 1):
        moves.append({"ply": ply, "uci": move.uci(), "san": replay.san(move),
                      "actor": "human" if replay.turn == human_color else "engine"})
        replay.push(move)
        frames.append(frame())
    return {"game_id": game_id, "revision": revision, "moves": moves, "frames": frames}
