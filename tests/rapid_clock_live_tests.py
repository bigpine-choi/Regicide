"""Isolated native-engine HTTP integration; no user games or personal labels.

The last timeout is accelerated with an explicitly injected monotonic offset.
It is a functional boundary test, not a real ten-minute match or strength test.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import datetime
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

import chess
import chess.pgn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import play_human
from game_archive import GameArchive

SOURCES = ("scripts/game_clock.py", "scripts/decision_support.py", "scripts/play_human.py",
           "scripts/game_archive.py", "scripts/engine_pacing.py", "scripts/sparring_play.py",
           "scripts/sparring_pacing.py", "scripts/sparring_rating.py", "tests/rapid_clock_live_tests.py")


def hashes():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCES}


def run(binary, output):
    output.mkdir(parents=True, exist_ok=True)
    before = hashes()
    checks, states = [], []
    shifted = [0.0]
    with tempfile.TemporaryDirectory(prefix="regicide-clock-live-") as folder:
        game = play_human.HumanGame(binary, Path(folder) / "games", opening_variety_enabled=False,
                                   monotonic=lambda: time.monotonic() + shifted[0])
        server = play_human.HumanServer(game)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def request(path, body=None):
            req = urllib.request.Request(server.origin + path,
                data=json.dumps(body).encode() if body is not None else None,
                headers={"X-Regicide-Token": server.token, "Origin": server.origin,
                         "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=70) as response:
                return json.loads(response.read())

        def paced_engine(state):
            started = time.monotonic()
            with ThreadPoolExecutor(max_workers=1) as workers:
                future = workers.submit(request, "/api/engine", {"revision": state["revision"]})
                deadline = time.monotonic() + 5
                waiting = None
                while time.monotonic() < deadline and not future.done():
                    observed = request("/api/state")
                    pacing = observed["engine"].get("pacing") or {}
                    if pacing.get("status") == "waiting":
                        waiting = observed
                        break
                    time.sleep(.02)
                assert waiting is not None, "Native engine never entered paced waiting"
                assert waiting["fen"] == state["fen"] and waiting["engine_thinking"]
                color = waiting["clock"]["running_color"]
                time.sleep(.06)
                ticking = request("/api/state")
                assert ticking["clock"][color + "_ms"] < waiting["clock"][color + "_ms"] - 30
                result = future.result(timeout=70)
            record = json.loads((game.save_dir / (game.game_id + ".json")).read_text(encoding="utf-8"))
            elapsed = record["clock"]["history"][-1]["elapsed_ms"]
            target = waiting["engine"]["pacing"]["target_total_ms"]
            assert elapsed >= target - 2, (elapsed, target)
            assert result["result"] == "*" and result["error"] is None
            return result, {"target_total_ms": target, "actual_turn_ms": elapsed,
                            "request_elapsed_ms": round((time.monotonic() - started) * 1000, 3),
                            "clock_polled_while_waiting": True, "move_held_until_target": True}

        try:
            state = request("/api/state")
            for control, initial, increment, color in (("10+0", 600000, 0, "white"),
                                                       ("10+5", 600000, 5000, "black"),
                                                       ("15+10", 900000, 10000, "white")):
                pacing_checks = []
                state = request("/api/new", {"revision": state["revision"], "color": color,
                                            "movetime_ms": 100, "time_control": control})
                assert state["clock"]["control_id"] == control
                assert state["clock"]["running_color"] == "white"
                assert state["clock"]["black_ms"] == initial
                if state["engine_to_move"]:
                    state, pacing_check = paced_engine(state)
                    pacing_checks.append(pacing_check)
                assert state["human_to_move"] and state["decision_support"]["status"] == "ready"
                first_clock = state["clock"][color + "_ms"]
                time.sleep(.06)
                observed = request("/api/state")
                assert observed["clock"][color + "_ms"] < first_clock - 30
                assert observed["clock"]["increment_ms"] == increment
                assert observed["decision_support"]["recommendation"]["remaining_ms"] < first_clock
                state = request("/api/move", {"revision": observed["revision"], "move": observed["legal_moves"][0]})
                state, pacing_check = paced_engine(state)
                pacing_checks.append(pacing_check)
                assert state["human_to_move"] and not state["error"]
                states.append({"control": control, "state": state})
                archive = GameArchive(game.save_dir).get_game(game.game_id)
                parsed = chess.pgn.read_game(io.StringIO(archive["pgn"]))
                assert not parsed.errors and parsed.headers["TimeControl"] == f"{initial // 1000}+{increment // 1000}"
                assert parsed.headers["EnginePacingPolicy"] == play_human.engine_pacing.POLICY
                assert all(n.clock() is not None for n in parsed.mainline())
                if control == "15+10":
                    (output / "native-live-state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
                state = request("/api/resign", {"revision": state["revision"]})
                frozen = (state["clock"]["white_ms"], state["clock"]["black_ms"])
                time.sleep(.03)
                observed = request("/api/state")
                assert frozen == (observed["clock"]["white_ms"], observed["clock"]["black_ms"])
                GameArchive(game.save_dir).get_game(game.game_id)
                checks.append({"control": control, "human_color": color,
                               "plies": len(state["moves"]), "countdown": True,
                               "increment_history_and_pgn": True, "frozen_after_resignation": True,
                               "engine_pacing": pacing_checks})
            state = request("/api/new", {"revision": state["revision"], "color": "black",
                                        "movetime_ms": 5000, "time_control": "10+0"})
            shifted[0] += 599
            urgent_started = time.monotonic()
            state = request("/api/engine", {"revision": state["revision"]})
            assert state["result"] == "*" and state["error"] is None and len(state["moves"]) == 1
            assert state["clock"]["white_ms"] > 0 and state["human_to_move"]
            checks.append({"accelerated_low_engine_clock": True, "injected_offset_seconds": 599,
                           "configured_search_ms": 5000, "engine_remaining_ms": state["clock"]["white_ms"],
                           "reply_elapsed_ms": round((time.monotonic() - urgent_started) * 1000, 3),
                           "legal_move_before_flag": True})
            automatic = []
            for remaining_seconds in (240, 180, 120, 60):
                state = request("/api/new", {"revision": state["revision"], "color": "black",
                                            "sparring_mode": True, "time_control": "10+0"})
                assert state["engine"]["time_management"] == "automatic"
                shifted[0] += 600 - remaining_seconds
                # Charge the accelerated time to a completed legal setup turn.
                # The measured next turn then starts with the reduced balance,
                # rather than confusing a late request with a short-clock turn.
                state = request("/api/engine", {"revision": state["revision"]})
                assert state["human_to_move"] and len(state["moves"]) == 1
                state = request("/api/move", {"revision": state["revision"], "move": state["legal_moves"][0]})
                before_move = request("/api/state")
                started = time.monotonic()
                state = request("/api/engine", {"revision": state["revision"]})
                assert state["result"] == "*" and state["error"] is None and len(state["moves"]) == 3
                assert state["clock"]["white_ms"] > 0 and state["human_to_move"]
                record = json.loads((game.save_dir / (game.game_id + ".json")).read_text(encoding="utf-8"))
                pacing_receipt = next(row for row in record["engine_pacing"]["decisions"] if row["ply"] == 3)
                assert pacing_receipt["pressure_factor"] <= 1 and pacing_receipt["allocation"]["automatic"]
                assert record["engine_movetimes_ms"]["3"] <= pacing_receipt["allocation"]["total_search_ms"]
                automatic.append({"remaining_before_ms": before_move["clock"]["white_ms"],
                                  "actual_search_budget_ms": record["engine_movetimes_ms"]["3"],
                                  "target_total_ms": pacing_receipt["target_total_ms"],
                                  "variation_factor": pacing_receipt["variation_factor"],
                                  "pressure_factor": pacing_receipt["pressure_factor"],
                                  "reply_elapsed_ms": round((time.monotonic() - started) * 1000, 3),
                                  "remaining_after_ms": state["clock"]["white_ms"]})
            checks.append({"automatic_sparring_pressure": automatic, "adaptive_budget_receipts_verified": True,
                           "clock_setup": "injected prior-turn elapsed time to leave 240, 180, 120, 60 seconds; measured next turns use real native replies"})
            state = request("/api/new", {"revision": state["revision"], "color": "white",
                                        "movetime_ms": 100, "time_control": "10+0"})
            shifted[0] += 601
            state = request("/api/state")
            assert state["result"] == "*" and state["human_to_move"] and state["legal_moves"]
            assert state["clock"]["white_ms"] == 0 and state["clock"]["overtime_color"] == "white"
            assert state["decision_support"]["recommendation"]["status"] == "inactive"
            state = request("/api/move", {"revision": state["revision"], "move": state["legal_moves"][0]})
            assert state["result"] == "*" and len(state["moves"]) == 1 and state["engine_to_move"]
            GameArchive(game.save_dir).get_game(game.game_id)
            checks.append({"human_zero_clock_continues": True, "injected_offset_seconds": 601,
                           "legal_move_after_zero": True, "archive_verified": True})
            state = request("/api/new", {"revision": state["revision"], "color": "black",
                                        "movetime_ms": 100, "time_control": "10+0"})
            shifted[0] += 601
            state = request("/api/state")
            assert state["result"] == "0-1" and state["termination"] == "time_forfeit"
            assert state["clock"]["white_ms"] == 0 and not state["legal_moves"]
            revision = state["revision"]
            assert request("/api/state")["revision"] == revision
            archive = GameArchive(game.save_dir).get_game(game.game_id)
            assert archive["summary"]["termination"] == "time_forfeit"
            checks.append({"accelerated_engine_timeout": True, "injected_offset_seconds": 601,
                           "idempotent": True, "archive_verified": True})
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            game.close()
            assert not thread.is_alive() and game._engine is None
    after = hashes()
    assert before == after, "Source changed during live verification; rerun after edits settle"
    result = {"schema": "regicide.rapid-clock-native.v1", "passed": True,
              "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
              "source_dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT)),
              "source_sha256": before, "source_sha256_after": after,
              "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
              "checks": checks, "engine_closed": True, "states": states,
              "scope": "Native timed engine replies and paced holds through production HTTP; short-clock and timeout cases use injected offsets; no complete timed match, human difficulty calibration, or personal rating label"}
    (output / "native-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": True, "checks": checks, "output": str(output)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    run(args.binary.resolve(strict=True), args.output.resolve())
