"""Check opening review with the real Regicide engine and private game stores.

The two short games are synthetic examples, not playing-strength evidence.
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

from coaching_session import CoachingSession

ROOT = Path(__file__).resolve().parents[1]
CASES = [
    ("sicilian", "1. e4 c5 2. Nf3 d6 3. d4 cxd4 4. Nxd4 Nf6 5. Nc3 a6 6. Be3 e5 *"),
    ("reti-to-queens-gambit", "1. Nf3 d5 2. d4 Nf6 3. c4 e6 4. Nc3 Be7 5. Bg5 O-O 6. e3 h6 *"),
]


def validate(report):
    assert report["status"] == "completed", report.get("error")
    assert len(report["moves"]) == 12
    board = chess.Board()
    packets = []
    for row in report["moves"]:
        move = chess.Move.from_uci(row["uci"])
        assert move in board.legal_moves
        board.push(move)
        packet = row["coach"]["opening"]
        if packet is None:
            continue
        assert packet["ply_index"] == row["ply"]
        assert packet["fen"] == row["fen_after"] == board.fen(en_passant="fen")
        assert [section["id"] for section in packet["sections"]] == ["white", "black", "balance"]
        assert packet["white"]["name"] and packet["black"]["name"]
        assert all(section["text"] for section in packet["sections"])
        if row["ply"] < 12:
            assert packet["assessment"]["status"] in ("too_early", "unavailable", "checkmate", "draw")
        if packet["assessment"]["status"] in ("balanced", "slight_edge", "better_start"):
            assert packet["assessment"]["score_cp_white"] == row["played"]["score_cp_white"]
            assert row["played"]["bound"] == "exact" and row["played"]["depth"] >= 4
            assert packet["assessment"]["provisional"] is True
        packets.append(packet)
    assert packets and packets[-1]["ply_index"] == 12
    return packets


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=ROOT / "build/regicide.exe")
    parser.add_argument("--output", type=Path, default=ROOT / "build/opening-coach/integration")
    args = parser.parse_args()
    binary = args.binary.resolve(strict=True)
    args.output.mkdir(parents=True, exist_ok=True)
    results = []
    with tempfile.TemporaryDirectory(prefix="regicide-opening-coach-") as temporary:
        for name, moves in CASES:
            folder = Path(temporary) / name
            folder.mkdir()
            pgn = '[Result "*"]\n\n' + moves + '\n'
            original = folder / "original.pgn"
            original.write_text(pgn, encoding="utf-8")
            original_bytes, calls = original.read_bytes(), []

            def factory(path):
                calls.append(True)
                options = {"creationflags": subprocess.CREATE_NO_WINDOW} if hasattr(subprocess, "CREATE_NO_WINDOW") else {}
                return chess.engine.SimpleEngine.popen_uci([str(path), "uci"], timeout=4, **options)

            session = CoachingSession(binary, save_dir=folder, engine_factory=factory)
            try:
                started = time.monotonic()
                session.start(pgn, "white", 500, session.revision)
                deadline = time.monotonic() + 50
                while session.snapshot()["status"] == "running" and time.monotonic() < deadline:
                    time.sleep(.05)
                report = session.snapshot()
                (args.output / (name + ".json")).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                packets = validate(report)
                wall_ms = round((time.monotonic() - started) * 1000, 3)
                files = list((folder / "coaching").glob("*.json"))
                assert len(files) == 1
                cache = json.loads(files[0].read_text(encoding="utf-8"))
                for row in cache["report"]["moves"]:
                    row["coach"]["opening"] = {"summary": "FORGED MATCHUP WINNER"}
                files[0].write_text(json.dumps(cache), encoding="utf-8")
                before = len(calls)
                cached = session.start(pgn, "white", 500, session.revision)
                assert cached["cached"] and len(calls) == before
                assert validate(cached) == packets and "FORGED MATCHUP" not in json.dumps(cached)
                assert original.read_bytes() == original_bytes
                results.append({"case": name, "plies": 12, "opening_packets": len(packets),
                                "last_recognition": packets[-1]["recognition"],
                                "last_assessment": packets[-1]["assessment"], "wall_ms": wall_ms,
                                "engine_launches": len(calls), "cache_rebuilt_without_search": True,
                                "original_pgn_unchanged": True})
            finally:
                session.close()
    result = {"engine_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(), "search_ms": 500,
              "mode": "Actual Regicide; two synthetic 12-ply standard-start games; private stores",
              "limitations": "Feature integration only; no opening matchup win rates or playing-strength claims.",
              "results": results}
    (args.output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("PASS: two real-engine opening reviews, 24 actual positions, rebuilt caches and unchanged PGNs")


if __name__ == "__main__":
    main()
