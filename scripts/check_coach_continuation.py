"""Check narrated continuations using Regicide and disposable coaching reports.

The two mirrored synthetic games are not personal games or strength tests.
Run with .venv-tests/Scripts/python.exe scripts/check_coach_continuation.py.
"""
from __future__ import annotations

import argparse
import copy
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
    assert row["classification"] in ("blunder", "mistake", "inaccuracy")
    for packet in (row["coach"], *row["coach"]["perspectives"].values()):
        continuation = packet["continuation"]
        assert continuation["available_plies"] == 6
        assert continuation["complete"] is True
        assert len(continuation["steps"]) == 6
        board = chess.Board(row["fen_before"])
        for index, step in enumerate(continuation["steps"], 1):
            move = chess.Move.from_uci(step["uci"])
            assert move in board.legal_moves
            assert step["ply_index"] == index
            assert step["color"] == ("white" if board.turn else "black")
            assert step["san"] == board.san(move)
            assert step["uci"] == row["best"]["pv_uci"][index - 1]
            board.push(move)
            assert board.fen(en_passant="fen") == row["best"]["pv_frames"][index]["fen"]
            assert step["text"].strip()
        assert continuation["intro"].strip() and continuation["summary"].strip()
    return row["coach"]["continuation"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=ROOT / "build/regicide.exe")
    parser.add_argument("--output", type=Path, default=ROOT / "build/coach-continuation/integration")
    args = parser.parse_args()
    binary = args.binary.resolve(strict=True)
    args.output.mkdir(parents=True, exist_ok=True)
    initial = chess.Board("3q2k1/8/8/8/8/4P3/8/3Q2K1 w - - 0 1")
    cases = [("white-exchange", initial, "d1d4"),
             ("black-exchange", initial.mirror(), "d8d5")]
    results = []
    with tempfile.TemporaryDirectory(prefix="regicide-continuation-check-") as temporary:
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
                continuation = validate(report)
                wall_ms = round((time.monotonic() - started) * 1000, 3)
                (args.output / (name + ".json")).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                # Cached commentary is untrusted; reopening must rebuild it from
                # typed scores and legal PVs without a new engine or PGN write.
                cache_files = list((folder / "coaching").glob("*.json"))
                assert len(cache_files) == 1
                cache = json.loads(cache_files[0].read_text(encoding="utf-8"))
                cache["report"]["moves"][0]["coach"]["continuation"] = {"intro": "FORGED FUTURE", "steps": []}
                cache_files[0].write_text(json.dumps(cache), encoding="utf-8")
                before_calls = len(calls)
                cached = session.start(pgn, color, 1500, session.revision)
                assert cached["cached"] and len(calls) == before_calls
                assert validate(cached) == continuation
                assert "FORGED FUTURE" not in json.dumps(cached)
                assert original.read_bytes() == original_bytes
                results.append({"case": name, "fen": board.fen(en_passant="fen"), "actual_uci": actual,
                                "classification": report["moves"][0]["classification"],
                                "best_pv": report["moves"][0]["best"]["pv_san"],
                                "continuation": copy.deepcopy(continuation), "wall_ms": wall_ms,
                                "engine_launches": len(calls), "cache_rebuilt_without_search": True,
                                "original_pgn_unchanged": True})
            finally:
                session.close()
    output = {"engine_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
              "search_ms": 1500, "mode": "actual Regicide; two mirrored synthetic one-ply games; private store",
              "limitations": "Feature integration only, not playing strength or human manual UI verification.",
              "results": results}
    (args.output / "result.json").write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("PASS: two real-engine reports, six legal narrated steps each, both perspectives, rebuilt cache and unchanged PGNs")


if __name__ == "__main__":
    main()
