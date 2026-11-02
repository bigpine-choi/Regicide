"""Actual sparring HTTP evaluation, cancellation and legal move integration.

Uses the selected local Regicide binary and temporary games only. This is a
functional check, not a full game, manual GUI session, or playing-strength test.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import play_human
import sparring_evaluation
from game_archive import GameArchive
from runtime_engine import resolve_runtime_engine

SOURCES = ("scripts/play_human.py", "scripts/move_hints.py", "scripts/sparring_evaluation.py",
           "scripts/sparring_play.py", "tests/sparring_evaluation_integration.py")


def run(binary, output):
    output.mkdir(parents=True, exist_ok=False)
    hashes = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCES}
    checks = []
    with tempfile.TemporaryDirectory(prefix="regicide-precise-live-") as folder:
        game = play_human.HumanGame(binary, Path(folder) / "games", opening_variety_enabled=False)
        server = play_human.HumanServer(game)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def request(path, body=None):
            req = urllib.request.Request(server.origin + path,
                data=json.dumps(body).encode() if body is not None else None,
                headers={"X-Regicide-Token": server.token, "Origin": server.origin,
                         "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as response:
                return json.loads(response.read())

        def evaluation(state, complete):
            started = time.monotonic()
            first_ms, samples = None, []
            while time.monotonic() - started < 14:
                value = request("/api/evaluation")
                assert all(value[key] == state[key] for key in ("game_id", "revision", "fen")), value
                elapsed = (time.monotonic() - started) * 1000
                if value.get("cp") is not None or value.get("mate") is not None:
                    if first_ms is None:
                        first_ms = round(elapsed, 3)
                    assert value["bound"] == "exact" and value["source_fen"] == state["fen"]
                    if not samples or samples[-1]["depth"] != value["depth"]:
                        samples.append({"elapsed_ms": round(elapsed, 3), "depth": value["depth"],
                                        "cp": value["cp"], "mate": value["mate"]})
                    if not complete or value["status"] == "ready":
                        assert value["refinement"]["policy"] == sparring_evaluation.POLICY
                        assert value["budget_ms"] == 10000
                        return {"first_score_ms_from_poll": first_ms, "samples": samples, "final": value}
                assert value["status"] != "unavailable", value
                time.sleep(.02)
            raise AssertionError("Actual precise helper did not finish within its bounded limit")

        def engine_turn(state):
            elapsed_queries = []
            with ThreadPoolExecutor(max_workers=1) as workers:
                future = workers.submit(request, "/api/engine", {"revision": state["revision"]})
                while not future.done():
                    start = time.monotonic()
                    value = request("/api/evaluation")
                    elapsed_queries.append(round((time.monotonic() - start) * 1000, 3))
                    # The response can cross the move boundary, but any score
                    # must identify its own board rather than the previous one.
                    if value.get("cp") is not None or value.get("mate") is not None:
                        assert value["source_fen"] == value["fen"] and value["bound"] == "exact"
                    time.sleep(.02)
                result = future.result(timeout=20)
            assert result["error"] is None and result["result"] == "*" and result["human_to_move"]
            return result, elapsed_queries

        try:
            state = request("/api/state")
            for color in ("white", "black"):
                state = request("/api/new", {"revision": state["revision"], "color": color,
                                            "sparring_mode": True, "time_control": "10+5"})
                initial = None
                if color == "white":
                    initial = evaluation(state, complete=False)
                    old_fen = state["fen"]
                    state = request("/api/move", {"revision": state["revision"], "move": "e2e4"})
                    assert state["fen"] != old_fen
                state, response_times = engine_turn(state)
                refined = evaluation(state, complete=True)
                unchanged = request("/api/state")
                assert unchanged["fen"] == state["fen"] and unchanged["moves"] == state["moves"]
                assert unchanged["clock"][color + "_ms"] <= state["clock"][color + "_ms"]
                archived = GameArchive(game.save_dir).get_game(game.game_id)
                assert '[SparringEvaluationPolicy "' + sparring_evaluation.POLICY + '"]' in archived["pgn"]
                checks.append({"human_color": color, "initial": initial, "refined": refined,
                               "evaluation_poll_ms_during_engine": response_times,
                               "legal_plies": len(state["moves"]), "board_unchanged_by_analysis": True,
                               "archive_policy_preserved": True})
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            game.close()
    final_hashes = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCES}
    assert hashes == final_hashes, "Source changed during integration"
    result = {"scope": __doc__, "binary": str(binary),
              "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
              "source_sha256": hashes, "source_unchanged": True, "checks": checks, "passed": True}
    (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": True, "colors": len(checks), "source_unchanged": True}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run((args.binary or resolve_runtime_engine(ROOT)).resolve(strict=True), args.output)
