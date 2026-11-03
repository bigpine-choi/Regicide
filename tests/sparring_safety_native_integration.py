"""Actual host/native comparison for the recorded poisoned a7 pawn.

The root baseline is reproducibly capped at depth 5 to recreate the recorded
Nxa7 +17cp mistake. All packets and moves are from the native engine. Later
searches use the production time limits. No personal archives are opened.
"""
import argparse
import contextlib
import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

import chess
import chess.engine

from recovery_native_integration import FixtureClock, RecordedEngine

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import play_human
import sparring_safety

HISTORY = "b1c3 e7e5 g1f3 b8c6 d2d4 e5d4 f3d4 c6d4 d1d4 g8f6 c3b5 f8e7 c1f4 d7d6 e1c1 e8g8"
SOURCES = ("scripts/play_human.py", "scripts/engine_pacing.py", "scripts/sparring_safety.py",
           "scripts/sparring_play.py", "scripts/sparring_evaluation.py",
           "tests/sparring_safety_native_integration.py", "tests/recovery_native_integration.py")


def source_hashes():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCES}


def fixture():
    board = chess.Board()
    for move in HISTORY.split():
        board.push_uci(move)
    return board


class ReplayEngine(RecordedEngine):
    def __init__(self, binary, searches):
        super().__init__(binary, searches)
        self.baseline_replayed = False

    def analysis(self, board, limit, **options):
        if (not self.baseline_replayed and len(board.move_stack) == 16
                and board.fen() == fixture().fen() and not options.get("root_moves")):
            limit = chess.engine.Limit(time=max(3.0, limit.time or 0), depth=5)
            self.baseline_replayed = True
        return super().analysis(board, limit, **options)


def audit(binary, board, selected):
    child = board.copy(stack=True)
    child.push_uci(selected)
    engine = RecordedEngine(binary, [])
    try:
        engine.configure({"UsePieceActivity": True})
        info = engine.analyse(child, chess.engine.Limit(time=3))
        return {"depth": info.get("depth"), "cp_white": info["score"].white().score(),
                "mate_white": info["score"].white().mate(), "budget_ms": 3000,
                "pv_uci": [m.uci() for m in info.get("pv", [])]}
    finally:
        engine.quit()


def external_audit(binary, selected):
    child = fixture()
    child.push_uci(selected)
    engine = chess.engine.SimpleEngine.popen_uci(str(binary), timeout=5)
    try:
        settings = {"Threads": 1, "Hash": 32, "Skill Level": 20, "UCI_LimitStrength": False}
        engine.configure(settings)
        info = engine.analyse(child, chess.engine.Limit(time=2))
        return {"engine": engine.id, "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                "options": settings, "depth": info.get("depth"), "budget_ms": 2000,
                "cp_white": info["score"].white().score(), "mate_white": info["score"].white().mate(),
                "pv_uci": [m.uci() for m in info.get("pv", [])], "selection_influence": False}
    finally:
        engine.quit()


def run_case(binary, enabled, referee=None):
    board, clock = fixture(), FixtureClock()
    searches, waits = [], []
    def wait(seconds, event):
        started = time.monotonic()
        event.wait(seconds)
        waits.append((time.monotonic() - started) * 1000)
    with tempfile.TemporaryDirectory(prefix="regicide-safety-native-") as folder:
        game = play_human.HumanGame(binary, Path(folder) / "games",
            engine_factory=lambda executable: ReplayEngine(executable, searches),
            opening_variety_enabled=False, monotonic=clock, pacing_wait=wait)
        try:
            game.new_game("black", 1000, game.revision, sparring_mode=True, time_control="10+0")
            game._opening_seed = "diagnostic-readonly-no-engine"
            for ply in range(16):
                game._clock.complete_move("white" if ply % 2 == 0 else "black", ply + 1)
            game.board = board.copy(stack=True)
            game._sync_evaluation()
            clock.started = time.monotonic()
            context = contextlib.nullcontext() if enabled else patch.object(
                sparring_safety, "inspect", return_value={"risky": False, "reason": "pre_gate_comparison"})
            started = time.monotonic()
            with context:
                state = game.engine_move(game.revision)
            elapsed = time.monotonic() - started
            assert state["error"] is None, state["error"]
            assert len(game.board.move_stack) == 17
            assert searches[0]["native_bestmove"] == "b5a7", searches[0]
            assert searches[0]["depth"] == 5 and searches[0]["cp_own"] == 17, searches[0]
            selected = state["last_move"]
            assert chess.Move.from_uci(selected) in board.legal_moves
            decision = game._sparring_decisions[17]
            safety = decision.get("tactical_safety")
            if enabled:
                assert safety and safety["status"] == "replaced", safety
                assert selected != "b5a7" and selected == safety["selected_move"]
                assert game.board.peek().uci() == selected, "Final gate selection must survive later processing"
                assert abs(safety["alternative_child"]["cp_own"]) <= 100, safety
            else:
                assert selected == "b5a7" and safety is None
            result = {"variant": "safety_enabled" if enabled else "gate_disabled_control",
                      "selected_uci": selected, "elapsed_ms": round(elapsed * 1000, 3),
                      "cosmetic_wait_ms": round(sum(waits), 3), "decision": decision,
                      "native_search_trace": searches, "clock": state["clock"],
                      "legal_move": True, "final_selected_matches_gate": enabled,
                      "referee_enabled": False}
        finally:
            game.close()
            assert game._engine is None
    result["fresh_child_audit"] = audit(binary, board, selected)
    cp = result["fresh_child_audit"]["cp_white"]
    assert cp is not None and (abs(cp) <= 100 if enabled else cp <= -100), result["fresh_child_audit"]
    if referee:
        result["external_child_audit"] = external_audit(referee, selected)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--repetitions", type=int, default=2)
    parser.add_argument("--referee", type=Path, help="Optional external audit only; never used by the player")
    args = parser.parse_args()
    before = source_hashes()
    results = []
    document = {"schema": "regicide.sparring-safety-native.v1", "passed": False,
                "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "binary": str(args.binary.resolve()),
                "binary_sha256": hashlib.sha256(args.binary.read_bytes()).hexdigest(),
                "source_sha256": before, "history_uci": HISTORY.split(), "fen": fixture().fen(),
                "conditions": {"Threads": 1, "Hash": 16, "UsePieceActivity": True,
                    "time_control": "10+0", "baseline_depth_cap": 5, "referee": "disabled",
                    "platform": platform.platform(), "processor": platform.processor(),
                    "logical_cpu_count": os.cpu_count(), "python": platform.python_version()},
                "cases": results,
                "limitations": "Same-position single-turn regression, not a match or strength estimate. Baseline depth is capped to reproduce the exact recorded finite search. Additional search is the treatment. Audit is fresh same-engine analysis, not an external oracle. Time-limited depths vary with load."}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    for enabled in [False] + [True] * args.repetitions:
        case = run_case(args.binary, enabled, args.referee)
        results.append(case)
        print(json.dumps({"variant": case["variant"], "selected": case["selected_uci"],
              "audit": case["fresh_child_audit"], "external": case.get("external_child_audit"),
              "elapsed_ms": case["elapsed_ms"]}), flush=True)
        args.output.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    after = source_hashes()
    document.update(source_sha256_after=after, source_unchanged=before == after, passed=before == after)
    args.output.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert before == after, "Sources changed during native verification; rerun required"


if __name__ == "__main__":
    main()
