"""Actual HTTP rating lifecycle with an isolated game and synthetic label.

Uses the local own engine and normal automatic time allocation. The explicit
skip-wait endpoint removes presentation holds; this is not a timing or Elo
measurement. No personal game directory or real rating labels are accessed.
"""
import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import play_human
from game_archive import GameArchive
from runtime_engine import resolve_runtime_engine

SOURCES = ("scripts/play_human.py", "scripts/boss_play.py", "scripts/rating_evidence.py",
           "scripts/sparring_rating.py", "scripts/sparring_play.py", "scripts/engine_pacing.py",
           "scripts/decision_support.py", "scripts/game_archive.py", "tests/sparring_rating_integration.py")


def run(binary, output):
    output.mkdir(parents=True, exist_ok=False)
    def hashes():
        return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCES}
    before = hashes()
    with tempfile.TemporaryDirectory(prefix="regicide-rating-http-") as temporary:
        game = play_human.HumanGame(binary, Path(temporary) / "games", opening_variety_enabled=False)
        server = play_human.HumanServer(game)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def request(path, body=None):
            req = urllib.request.Request(server.origin + path,
                data=json.dumps(body).encode() if body is not None else None,
                headers={"X-Regicide-Token": server.token, "Origin": server.origin,
                         "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=25) as response:
                return json.loads(response.read())
        try:
            state = request("/api/state")
            state = request("/api/new", {"revision": state["revision"], "color": "white",
                                        "sparring_mode": True, "time_control": "10+0"})
            with game._lock:
                # Authored timing fixture with room for the 500ms observation
                # even after v4's one-second reduction. Insufficient allowance
                # must skip observation and is tested in the host boundary suite.
                game._opening_seed = "rating-native-budget-91"
            assert state["engine"]["sparring_rating"]["estimate"] is None
            state = request("/api/move", {"revision": state["revision"], "move": "e2e4"})
            assert request("/api/engine/skip-wait", {"revision": state["revision"]})["released"]
            state = request("/api/engine", {"revision": state["revision"]})
            assert state["error"] is None and state["human_to_move"]
            value = state["engine"]["sparring_rating"]
            decision = copy.deepcopy(game._sparring_decisions[2])
            pacing = copy.deepcopy(game._engine_pacing_decisions[2])
            assert decision["rating_only"] and decision["reason"] == "rating_observation_only"
            assert decision["selected_uci"] == decision["baseline_uci"] == state["last_move"]
            assert decision["baseline_budget_ms"] < 600
            assert decision["quality_budget_ms"] <= pacing["rating_only_budget_ms"] <= 500
            assert decision["baseline_budget_ms"] + decision["quality_budget_ms"] <= pacing["allocation"]["total_search_ms"]
            evidence = decision["opponent_observation"]["rating_evidence"]
            assert evidence["valid"] and evidence["kind"] == "cp_loss", evidence
            assert value["method"] == "regicide-sparring-rating-v2" and value["observations"] == 1
            assert value["updated_human_ply"] == 1 and isinstance(value["estimate"], int)
            assert value["effective_observations"] > 0 and value["evidence_level"] == "low"
            original = copy.deepcopy(value)
            state = request("/api/resign", {"revision": state["revision"]})
            state = request("/api/sparring/rating-label", {"revision": state["revision"],
                            "game_id": state["game_id"], "rating": 1500})
            assert state["engine"]["sparring_rating"]["estimate"] == original["estimate"]
            assert state["engine"]["sparring_rating"]["calibration_games"] == 0
            assert state["engine"]["sparring_rating"]["label"]["rating"] == 1500
            saved = json.loads((game.save_dir / (game.game_id + ".json")).read_text(encoding="utf-8"))
            assert saved["state"]["engine"]["sparring_rating"]["label"] is None
            archive = GameArchive(game.save_dir).get_game(game.game_id)
            assert archive["frames"][0]["engine"]["sparring_rating"]["estimate"] is None
            assert archive["frames"][-1]["engine"]["sparring_rating"]["estimate"] == original["estimate"]
            state = request("/api/new", {"revision": state["revision"], "color": "black",
                                        "sparring_mode": True, "time_control": "10+0"})
            fresh = state["engine"]["sparring_rating"]
            assert fresh["estimate"] is None and fresh["calibration_games"] == 1
            result = {"passed": True, "created_utc": datetime.now(timezone.utc).isoformat(),
                      "scope": __doc__, "synthetic_reported_rating": 1500,
                      "rating_only_decision": decision, "pacing": pacing, "first_rating": original,
                      "next_game_rating": fresh, "own_label_did_not_rewrite_game": True,
                      "archive_prefix_and_private_label_verified": True}
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            game.close()
            assert not thread.is_alive() and game._engine is None
    after = hashes()
    assert before == after, "Source changed during native test"
    result.update(source_sha256=before, source_sha256_after=after,
                  binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(), engine_closed=True)
    (output / "validation.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": True, "output": str(output), "observations": original["observations"]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    run((args.binary or resolve_runtime_engine()).resolve(), args.output.resolve())
