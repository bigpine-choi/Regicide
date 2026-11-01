"""Verify real opponent candidates arrive before its normal 1s move completes."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time

ROOT = next(parent for parent in Path(__file__).resolve().parents
            if (parent / "scripts/play_human.py").is_file())
sys.path.insert(0, str(ROOT / "scripts"))
import chess
import chess.engine
import subprocess
from play_human import HumanGame

BINARY = ROOT / "build/regicide.exe"
def factory(binary):
    return chess.engine.SimpleEngine.popen_uci([str(binary), "uci"], timeout=3,
        **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))

board = chess.Board()
for san in "e4 e5 Nf3 Nc6 Bc4 Bc5 O-O Nf6 d3 d6 c3 O-O Re1 a6 Bb3 Ba7".split():
    board.push_san(san)
positions = [board.copy(stack=True)]
board.push_san("h3")
positions.append(board)
rows = []
for position in positions:
    with tempfile.TemporaryDirectory(prefix="regicide-both-hints-default-") as folder:
        game = HumanGame(BINARY, folder, engine_factory=factory, hint_engine_factory=factory)
        try:
            with game._lock:
                game.board = position.copy(stack=True)
                game.human_color = not position.turn
                game.revision += 1
                game._sync_evaluation()
            before = game.state()
            assert before["engine_to_move"] and not before["legal_moves"]
            game.set_hint_enabled(True)
            started = time.monotonic()
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(game.engine_move, before["revision"])
                observed = []
                while not future.done():
                    hint = game.hint()
                    if hint["status"] == "ready" and hint["fen"] == before["fen"]:
                        assert hint["turn"] == before["turn"]
                        assert len(hint["candidates"]) == 3
                        assert all(item["move"] in before["hint_legal_moves"] for item in hint["candidates"])
                        observed.append({"at_ms": round((time.monotonic() - started) * 1000, 2),
                                         "depth": hint["depth"], "moves": [item["move"] for item in hint["candidates"]]})
                    time.sleep(.3)
                after = future.result()
            assert observed, "No real candidate packet observed before normal opponent reply"
            assert not after["error"] and len(after["moves"]) == len(before["moves"]) + 1
            assert game._opening_decisions.get(len(after["moves"])) is None, "Fixture must avoid extra opening time"
            assert game.hint()["fen"] == after["fen"]
            rows.append({"opponent_color": before["turn"], "before_ply": len(before["moves"]),
                         "budget_ms": 1000, "extra_opening_ms": 0, "poll_interval_ms": 300,
                         "engine_move": after["last_move"], "observed_before_reply": observed,
                         "old_position_cleared_after_reply": True})
        finally:
            game.close()
report = {"binary_sha256": hashlib.sha256(BINARY.read_bytes()).hexdigest(), "cases": rows,
          "note": "Fresh private real engines, classical evaluation, both opponent colors outside opening variety. No added move delay, personal games or UI interaction. Shared computer timing observations, not latency guarantees."}
output = ROOT / "build/both-side-hints/default-time-result.json"
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
print(json.dumps(report))
