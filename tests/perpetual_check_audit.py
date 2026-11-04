"""Offline external audit of already-declared moves; never a live selector."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform

import chess
import chess.engine


FEN = "r4k2/2p5/1pqp4/5r2/p3R2R/2B3P1/PP3P1P/6K1 b - - 3 31"
LATE = "a4a3 h4h8 f8f7 h8h7 f7f8 h7h8 f8f7 h8h7".split()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = {
        "schema": "regicide.perpetual-check-offline-audit.v1",
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "binary_sha256": sha(args.binary), "script_sha256": sha(__file__),
        "platform": platform.platform(), "python_chess": chess.__version__,
        "scope": "Post-selection external analysis of declared development moves. No live move selection, Elo or solved-position proof.",
        "depth_requested": 18, "time_cap_seconds": 5,
        "options": {"Threads": 1, "Hash": 16}, "queries": []}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:
        with chess.engine.SimpleEngine.popen_uci(str(args.binary.resolve()), timeout=10) as engine:
            report["engine_id"] = engine.id
            engine.configure(report["options"])
            for history, moves in (([], ("a4a3", "c6c5", "d6d5", "f5e5", "c6e4")),
                                   (LATE, ("f7f8", "f7g8", "f7g6"))):
                board = chess.Board(FEN)
                for uci in history:
                    board.push_uci(uci)
                for uci in moves:
                    move = chess.Move.from_uci(uci)
                    assert move in board.legal_moves
                    info = engine.analyse(board, chess.engine.Limit(depth=18, time=5),
                                          root_moves=[move], game=object())
                    score = info["score"].pov(board.turn)
                    replay = board.copy(stack=True)
                    for item in info["pv"]:
                        assert item in replay.legal_moves
                        replay.push(item)
                    report["queries"].append({"initial_fen": FEN, "history_uci": history,
                        "root_fen": board.fen(), "selected_uci": uci,
                        "depth": info["depth"], "cp_root": score.score(), "mate_root": score.mate(),
                        "pv_uci": [m.uci() for m in info["pv"]], "nodes": info.get("nodes"),
                        "time_seconds": info.get("time"),
                        "exact_packet": not info.get("lowerbound") and not info.get("upperbound"),
                        "requested_depth_completed": info["depth"] >= 18})
        report["finished_utc"] = datetime.now(timezone.utc).isoformat()
        json.dump(report, output, indent=2, allow_nan=False)
        output.write("\n")
    print(json.dumps({"queries": len(report["queries"]), "output": str(args.output)}))


if __name__ == "__main__":
    main()
