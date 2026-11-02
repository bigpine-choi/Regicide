"""Isolated native-engine HTTP integration; no user games or personal labels.

The last timeout is accelerated with an explicitly injected monotonic offset.
It is a functional boundary test, not a real ten-minute match or strength test.
"""
import argparse
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
           "scripts/game_archive.py", "tests/rapid_clock_live_tests.py")


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
            with urllib.request.urlopen(req, timeout=10) as response:
                return json.loads(response.read())

        try:
            state = request("/api/state")
            for control, initial, increment, color in (("10+0", 600000, 0, "white"),
                                                       ("10+5", 600000, 5000, "black"),
                                                       ("15+10", 900000, 10000, "white")):
                state = request("/api/new", {"revision": state["revision"], "color": color,
                                            "movetime_ms": 100, "time_control": control})
                assert state["clock"]["control_id"] == control
                assert state["clock"]["running_color"] == "white"
                assert state["clock"]["black_ms"] == initial
                if state["engine_to_move"]:
                    state = request("/api/engine", {"revision": state["revision"]})
                assert state["human_to_move"] and state["decision_support"]["status"] == "ready"
                first_clock = state["clock"][color + "_ms"]
                time.sleep(.06)
                observed = request("/api/state")
                assert observed["clock"][color + "_ms"] < first_clock - 30
                assert observed["clock"]["increment_ms"] == increment
                assert observed["decision_support"]["recommendation"]["remaining_ms"] < first_clock
                state = request("/api/move", {"revision": observed["revision"], "move": observed["legal_moves"][0]})
                state = request("/api/engine", {"revision": state["revision"]})
                assert state["human_to_move"] and not state["error"]
                states.append({"control": control, "state": state})
                archive = GameArchive(game.save_dir).get_game(game.game_id)
                parsed = chess.pgn.read_game(io.StringIO(archive["pgn"]))
                assert not parsed.errors and parsed.headers["TimeControl"] == f"{initial // 1000}+{increment // 1000}"
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
                               "increment_history_and_pgn": True, "frozen_after_resignation": True})
            state = request("/api/new", {"revision": state["revision"], "color": "white",
                                        "movetime_ms": 100, "time_control": "10+0"})
            shifted[0] += 601
            state = request("/api/state")
            assert state["result"] == "0-1" and state["termination"] == "time_forfeit"
            assert state["clock"]["white_ms"] == 0 and not state["legal_moves"]
            revision = state["revision"]
            assert request("/api/state")["revision"] == revision
            archive = GameArchive(game.save_dir).get_game(game.game_id)
            assert archive["summary"]["termination"] == "time_forfeit"
            checks.append({"accelerated_timeout": True, "injected_offset_seconds": 601,
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
              "scope": "Native 100ms engine replies through production HTTP; final timeout uses injected offset; no complete timed match, human difficulty calibration, or personal rating label"}
    (output / "native-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": True, "checks": checks, "output": str(output)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    run(args.binary.resolve(strict=True), args.output.resolve())
