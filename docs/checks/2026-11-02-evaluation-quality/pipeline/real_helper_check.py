"""Bounded backend integration check; never starts a server or opponent search.

Run from the repository root:
  .venv-tests/Scripts/python.exe build/evaluation-refinement/real_helper_check.py

Only evaluation helpers execute the explicitly preserved baseline engine. Game
records live in an isolated temporary directory and are deleted on completion.
This validates the new budget/cancellation pipeline, not strength or GUI output.
"""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import chess.engine
from play_human import HumanGame

OUT = Path(__file__).resolve().parent
BINARY = ROOT / "build/evaluation-quality-v1/baseline/regicide.exe"
DEADLINE = time.monotonic() + 45


class IdleOpponent:
    id = {"name": "Idle integration-test opponent; searches forbidden"}

    def analysis(self, *args, **kwargs):
        raise AssertionError("Opponent search is outside this integration check")

    play = analysis

    def quit(self):
        pass

    close = quit


helpers = []


def real_helper(binary):
    engine = chess.engine.SimpleEngine.popen_uci([str(binary), "uci"], timeout=3)
    row = {"engine": engine, "created_at": time.monotonic()}
    helpers.append(row)
    original = engine.analysis

    def record_analysis(board, limit, **kwargs):
        row.update(started_at=time.monotonic(), limit_seconds=limit.time,
                   fen=board.fen(en_passant="fen"))
        return original(board, limit, **kwargs)

    engine.analysis = record_analysis
    return engine


def until(predicate, timeout=6):
    stop = min(DEADLINE, time.monotonic() + timeout)
    while time.monotonic() < stop:
        value = predicate()
        if value:
            return value
        time.sleep(.02)
    raise AssertionError("Timed out waiting for bounded integration condition")


def has_score(value):
    return value.get("cp") is not None or value.get("mate") is not None


def brief(value):
    return {key: value.get(key) for key in
            ("game_id", "revision", "fen", "source_fen", "status", "cp", "mate", "depth", "budget_ms")}


report = {"started_at_utc": datetime.now(timezone.utc).isoformat(),
          "binary": str(BINARY.relative_to(ROOT)),
          "binary_sha256": hashlib.sha256(BINARY.read_bytes()).hexdigest(),
          "play_human_sha256": hashlib.sha256((ROOT / "scripts/play_human.py").read_bytes()).hexdigest(),
          "source_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
          "python": platform.python_version(), "platform": platform.platform(),
          "scope": "Real baseline evaluation helper only; no GUI, server, AI search, or strength measurement",
          "samples": []}
game = None
try:
    with tempfile.TemporaryDirectory(prefix="regicide-real-helper-") as temporary:
        game = HumanGame(BINARY, Path(temporary) / "games",
                         engine_factory=lambda _: IdleOpponent(),
                         evaluation_engine_factory=real_helper,
                         opening_variety_enabled=False)
        until(lambda: helpers and "started_at" in helpers[0])
        first = helpers[0]
        initial_fen = game.board.fen(en_passant="fen")
        assert first["limit_seconds"] == 3.0
        # This is the first poll after HumanGame construction. Earlier streamed
        # depths may already have been emitted; it is not first-output latency.
        first_score = until(lambda: (value if has_score(value := game.evaluation()) else None))
        report["first_score_elapsed_ms"] = round((time.monotonic() - first["started_at"]) * 1000, 3)
        report["first_score"] = brief(first_score)
        until(lambda: time.monotonic() - first["started_at"] >= 1.1)
        after_one_second = game.evaluation()
        assert after_one_second["status"] == "searching", after_one_second
        assert after_one_second["fen"] == after_one_second["source_fen"] == initial_fen
        assert after_one_second["budget_ms"] == 3000
        report["after_one_second"] = brief(after_one_second)

        def finished():
            value = game.evaluation()
            report["samples"].append({"elapsed_ms": round((time.monotonic() - first["started_at"]) * 1000, 3),
                                      **brief(value)})
            return value if value["status"] == "ready" else None

        final = until(finished)
        report["completed_elapsed_ms"] = round((time.monotonic() - first["started_at"]) * 1000, 3)
        assert final["fen"] == final["source_fen"] == initial_fen
        assert final["budget_ms"] == 3000 and has_score(final)
        assert not first["engine"].returncode.result(timeout=1) is None
        report["completed"] = brief(final)
        assert game.movetime_ms == 1000 and game.hint()["movetime_ms"] == 1000

        game.new_game("white", 1000, game.revision)
        until(lambda: len(helpers) == 2 and "started_at" in helpers[1])
        second = helpers[1]
        old = until(lambda: (value if has_score(value := game.evaluation()) else None))
        until(lambda: time.monotonic() - second["started_at"] >= .4)
        assert game.evaluation()["status"] == "searching"
        move_started = time.monotonic()
        game.human_move("e2e4", game.revision)
        move_returned = time.monotonic()
        moved_fen = game.board.fen(en_passant="fen")
        assert moved_fen != old["fen"]
        assert move_returned - move_started < 1.0
        until(lambda: second["engine"].returncode.done(), timeout=1.5)
        helper_exited = time.monotonic()
        observations = []
        observe_until = time.monotonic() + .4
        while time.monotonic() < observe_until:
            current = game.evaluation()
            observations.append(brief(current))
            assert current["fen"] == moved_fen
            assert current["source_fen"] is None and not has_score(current)
            assert current["status"] == "waiting"
            time.sleep(.02)
        report["cancelled"] = {"old_position": brief(old),
                               "new_position": observations[-1],
                               "human_move_return_ms": round((move_returned - move_started) * 1000, 3),
                               "helper_exit_ms_after_move": round((helper_exited - move_started) * 1000, 3),
                               "post_cancel_observations": len(observations),
                               "old_score_leaks": 0}
        report["opponent_searches"] = 0
        report["helper_searches"] = len(helpers)
        report["status"] = "passed"
        game.close()
        game = None
except BaseException as exc:
    report["status"] = "failed"
    report["error"] = f"{type(exc).__name__}: {exc}"
    raise
finally:
    if game is not None:
        game.close()
    for row in helpers:
        row["engine"].close()
    report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    (OUT / "real-helper-result.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "samples"}, indent=2))
