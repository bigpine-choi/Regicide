"""Exploratory depth scaling; baseline scores are reports, not independent proofs."""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import chess
from mate_performance import TimedUci, query, save, distance

baseline, prototype, output = sys.argv[1:4]
output = Path(output)
engines = [TimedUci(baseline), TimedUci(prototype)]
records = []
try:
    for engine in engines:
        engine.send("setoption name Move Overhead value 0")
        engine.send("isready")
        engine.until("readyok")
    for rank in (5, 4, 3, 1):
        board = chess.Board.empty()
        board.set_piece_at(chess.H7, chess.Piece(chess.KING, chess.BLACK))
        board.set_piece_at(chess.A2, chess.Piece(chess.QUEEN, chess.WHITE))
        board.set_piece_at(chess.parse_square("f%d" % rank), chess.Piece(chess.KING, chess.WHITE))
        assert board.is_valid()
        assert chess.Move.from_uci("a2g2") in board.legal_moves
        row = {"label": "Kf%d_Qa2_Kh7" % rank, "fen": board.fen(), "root_moves": ["a2g2"], "budgets": {}}
        for budget, suffix in (("nodes50000", "nodes 50000"), ("time100ms", "movetime 100 nodes 1000000000")):
            pair = {}
            for i in (0, 1):
                command = ("go " if i == 0 else "go mate 16 ") + suffix + " searchmoves a2g2"
                pair["normal_baseline" if i == 0 else "mate_prototype"] = query(engines[i], board, command)
            row["budgets"][budget] = pair
        if rank in (5, 4):
            row["independent_oracle_mate"] = distance(board, 4, ["a2g2"])
            for pair in row["budgets"].values():
                assert pair["mate_prototype"]["result"]["score"] == row["independent_oracle_mate"]
        else:
            row["independent_oracle_mate"] = "not evaluated beyond M4; no long mate claim"
        records.append(row)
        save(output / "scaling.json", records)
        print("SCALE", row["label"], {b:{k:v["result"] for k,v in p.items()} for b,p in row["budgets"].items()}, flush=True)
finally:
    for engine in engines:
        engine.close()
