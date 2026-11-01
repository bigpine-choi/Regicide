"""Verify strategic coaching with Regicide and private, synthetic game stores.

This checks feature integration and cache reconstruction, not playing strength.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import time

import chess
import chess.engine
import chess.pgn

from coaching_session import CoachingSession

ROOT = Path(__file__).resolve().parents[1]


def validate(report):
    assert report["status"] == "completed", report.get("error")
    row = report["moves"][0]
    assert row["classification"] in ("blunder", "mistake", "inaccuracy", "missed_mate", "allows_mate")
    for packet in (row["coach"], *row["coach"]["perspectives"].values()):
        insight = packet["insight"]
        assert insight and insight["theme"] == "fork", row
        assert [part["id"] for part in insight["sections"]] == ["idea", "plan", "counterplay"]
        board = chess.Board(row["fen_before"])
        branch = row["best"]
        for index, uci in enumerate(branch["pv_uci"], 1):
            move = chess.Move.from_uci(uci)
            assert move in board.legal_moves
            board.push(move)
            assert board.fen(en_passant="fen") == branch["pv_frames"][index]["fen"]
        for part in insight["sections"] + insight["step_notes"]:
            assert part["line"] == "best" and 1 <= part["ply_index"] <= len(branch["pv_uci"])
            assert part["text"] and all(square in chess.SQUARE_NAMES for square in part["squares"])
        continuation = packet["continuation"]
        assert continuation and continuation["intro"].startswith(insight["summary"])
        for note in insight["step_notes"]:
            if note["ply_index"] <= len(continuation["steps"]):
                assert note["text"] == continuation["steps"][note["ply_index"] - 1]["text"]
    return row["coach"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=ROOT / "build/regicide.exe")
    parser.add_argument("--output", type=Path, default=ROOT / "build/coach-strategy/integration")
    args = parser.parse_args()
    binary = args.binary.resolve(strict=True)
    args.output.mkdir(parents=True, exist_ok=True)
    initial = chess.Board("8/3q1k2/8/8/8/5N2/7P/6K1 w - - 0 1")
    cases = [("white-fork", initial, "g1h1"), ("black-fork", initial.mirror(), "g8h8")]
    results = []
    with tempfile.TemporaryDirectory(prefix="regicide-strategy-check-") as temporary:
        for name, board, actual in cases:
            game = chess.pgn.Game.from_board(board)
            game.add_main_variation(chess.Move.from_uci(actual))
            pgn, color = str(game), "white" if board.turn else "black"
            folder = Path(temporary) / name
            folder.mkdir()
            original = folder / "original.pgn"
            original.write_text(pgn, encoding="utf-8")
            original_bytes = original.read_bytes()
            calls = []

            def factory(path):
                calls.append(True)
                options = {"creationflags": subprocess.CREATE_NO_WINDOW} if hasattr(subprocess, "CREATE_NO_WINDOW") else {}
                return chess.engine.SimpleEngine.popen_uci([str(path), "uci"], timeout=4, **options)

            session = CoachingSession(binary, save_dir=folder, engine_factory=factory)
            try:
                started = time.monotonic()
                session.start(pgn, color, 1500, session.revision)
                deadline = time.monotonic() + 25
                while session.snapshot()["status"] == "running" and time.monotonic() < deadline:
                    time.sleep(.05)
                report = session.snapshot()
                (args.output / (name + ".json")).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                coach = validate(report)
                wall_ms = round((time.monotonic() - started) * 1000, 3)
                files = list((folder / "coaching").glob("*.json"))
                assert len(files) == 1
                cache = json.loads(files[0].read_text(encoding="utf-8"))
                cache["report"]["moves"][0]["coach"] = {"insight": {"summary": "FORGED PLAN"}}
                files[0].write_text(json.dumps(cache), encoding="utf-8")
                before_calls = len(calls)
                cached = session.start(pgn, color, 1500, session.revision)
                assert cached["cached"] and len(calls) == before_calls
                assert validate(cached) == coach and "FORGED PLAN" not in json.dumps(cached)
                assert original.read_bytes() == original_bytes
                results.append({"case": name, "fen": board.fen(en_passant="fen"), "actual_uci": actual,
                                "classification": report["moves"][0]["classification"],
                                "best_pv": report["moves"][0]["best"]["pv_san"],
                                "theme": coach["insight"]["theme"], "wall_ms": wall_ms,
                                "engine_launches": len(calls), "cache_rebuilt_without_search": True,
                                "original_pgn_unchanged": True})
            finally:
                session.close()
    output = {"engine_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(), "search_ms": 1500,
              "mode": "actual Regicide; two mirrored synthetic one-ply games; private store",
              "limitations": "Feature integration only, not playing strength or human manual UI verification.",
              "results": results}
    (args.output / "result.json").write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("PASS: two real-engine strategy reports, three perspectives, legal frames, rebuilt cache and unchanged PGNs")


if __name__ == "__main__":
    main()
