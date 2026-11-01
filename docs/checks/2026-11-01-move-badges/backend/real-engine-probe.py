"""Reproduce a one-position annotation check without loading personal games."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time

import chess
import chess.engine

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "scripts"))
from coaching_session import CoachingSession

OUT = Path(__file__).resolve().parent
BINARY = ROOT / "build" / "regicide.exe"
PGN = '[SetUp "1"]\n[FEN "4k3/8/8/8/8/8/q7/R3K3 w Q - 0 1"]\n[Result "*"]\n\n1. Rxa2 *\n'
factory_calls = 0

def factory(binary):
    global factory_calls
    factory_calls += 1
    return chess.engine.SimpleEngine.popen_uci([str(binary), "uci"], timeout=3)

with tempfile.TemporaryDirectory(prefix="regicide-move-badges-") as folder:
    session = CoachingSession(BINARY, save_dir=folder, engine_factory=factory)
    try:
        begin = time.monotonic()
        session.start(PGN, "white", 500, 0)
        deadline = begin + 15
        while session.snapshot()["status"] == "running" and time.monotonic() < deadline:
            time.sleep(.02)
        report = session.snapshot()
        elapsed = time.monotonic() - begin
        assert report["status"] == "completed", report["status"]
        row = report["moves"][0]
        assert row["classification"] == "best"
        for name in ("best", "played", "alternative"):
            line = row[name]
            assert line and line["bound"] == "exact"
            board = chess.Board(row["fen_before"])
            assert line["pv_frames"][0]["fen"] == board.fen(en_passant="fen")
            for index, uci in enumerate(line["pv_uci"], 1):
                move = chess.Move.from_uci(uci)
                assert move in board.legal_moves
                board.push(move)
                assert line["pv_frames"][index]["fen"] == board.fen(en_passant="fen")
        assert row["alternative"]["uci"] != row["uci"]
        assert row["annotation"]["square"] == "a2"
        assert row["annotation"]["fen_after"] == row["fen_after"]
        cached = session.start(PGN, "white", 500, session.revision)
        assert cached["cached"] and cached["moves"] == report["moves"]
        assert factory_calls == 1
        assert set(Path(folder).iterdir()) == {Path(folder) / "coaching"}
        evidence = {"fixture": "Constructed Rxa2 position; no personal games", "movetime_ms": 500,
                    "elapsed_seconds": round(elapsed, 4), "engine_factory_calls_after_cache_reuse": factory_calls,
                    "binary_sha256": hashlib.sha256(BINARY.read_bytes()).hexdigest(), "evaluation_id": "classical",
                    "actual": row["uci"], "symbol": row["annotation"]["symbol"],
                    "best_cp_white": row["best"]["score_cp_white"], "alternative_uci": row["alternative"]["uci"],
                    "alternative_cp_white": row["alternative"]["score_cp_white"],
                    "checks": ["Both complete PVs legally replayed", "Actual move excluded from alternative root",
                               "Exact search scores", "Actual destination and full FEN bound to badge",
                               "Cache regenerates identical complete rows without another engine", "Only temporary coaching cache written"],
                    "limits": ["One constructed position, not a strength test", "Finite classical Regicide search, not objective brilliance"]}
        (OUT / "real-engine-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        (OUT / "real-engine-evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(evidence, ensure_ascii=False, indent=2))
    finally:
        session.close()
