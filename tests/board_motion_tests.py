"""Validate animated transitions against independent legal chess positions."""

import argparse
import json
from pathlib import Path
import subprocess
import tempfile

import chess

ROOT = Path(__file__).resolve().parents[1]


def frame(board, ply):
    return {"board": {chess.square_name(s): p.symbol() for s, p in board.piece_map().items()},
            "turn": "white" if board.turn else "black", "fen": board.fen(en_passant="fen"),
            "last_move": board.peek().uci() if board.move_stack else None, "game_id": "motion-tests", "ply": ply}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--godot", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "build/board-motion-tests")
    args = parser.parse_args()
    fixtures = []
    for name, fen, uci in [
        ("white pawn", chess.STARTING_FEN, "e2e4"),
        ("knight", chess.STARTING_FEN, "g1f3"),
        ("black pawn", chess.STARTING_FEN.replace(" w ", " b "), "e7e5"),
        ("king capture", "4k3/8/8/8/8/8/4p3/4K3 w - - 0 1", "e1e2"),
        ("white short castle", "r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1", "e1g1"),
        ("white long castle", "r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1", "e1c1"),
        ("black short castle", "r3k2r/8/8/8/8/8/8/R3K2R b KQkq - 0 1", "e8g8"),
        ("black long castle", "r3k2r/8/8/8/8/8/8/R3K2R b KQkq - 0 1", "e8c8"),
        ("white en passant", "4k3/8/8/3pP3/8/8/8/4K3 w - d6 0 1", "e5d6"),
        ("black en passant", "4k3/8/8/8/3pP3/8/8/4K3 b - e3 0 1", "d4e3"),
        ("queen promotion", "4k3/P7/8/8/8/8/8/4K3 w - - 0 1", "a7a8q"),
        ("knight promotion", "4k3/P7/8/8/8/8/8/4K3 w - - 0 1", "a7a8n"),
        ("capture promotion", "1r2k3/P7/8/8/8/8/8/4K3 w - - 0 1", "a7b8q"),
        ("black promotion", "4k3/8/8/8/8/8/7p/4K3 b - - 0 1", "h2h1n"),
    ]:
        board = chess.Board(fen)
        move = chess.Move.from_uci(uci)
        assert board.is_valid() and move in board.legal_moves, name
        before = frame(board, 0)
        movers, capture = 2 if board.is_castling(move) else 1, board.is_capture(move)
        board.push(move)
        fixtures.append({"name": name, "uci": uci, "before": before, "after": frame(board, 1), "movers": movers, "capture": capture})
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "legal-cases.json").write_text(json.dumps(fixtures, indent=2), encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="regicide-motion-") as directory:
        path = Path(directory) / "cases.json"
        path.write_text(json.dumps(fixtures), encoding="utf-8")
        run = subprocess.run([str(args.godot), "--headless", "--path", str(ROOT / "godot"),
                              "--script", "res://tests/board_motion_tests.gd", "--", "--cases-file", str(path)],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    text = run.stdout + run.stderr
    (args.output / "native-motion.txt").write_text(text, encoding="utf-8")
    print(text)
    if run.returncode or "ERROR:" in text or "FAIL:" in text:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
