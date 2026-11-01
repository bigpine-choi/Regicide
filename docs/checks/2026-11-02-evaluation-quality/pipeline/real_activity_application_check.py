"""Verify owned HumanGame configuration against real binaries, without GUI/server."""
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import chess
import chess.engine
import chess.pgn
from classical_evaluation import classical_cp_stm
from play_human import HumanGame

OUT = Path(__file__).resolve().parent
report = {"started_utc": datetime.now(timezone.utc).isoformat(),
          "scope": "Owned temporary backend sessions only; no server, GUI or playing-strength claim", "cases": []}


def wait_for(predicate):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.02)
    raise AssertionError("Timed out waiting for owned helper")


def check(label, binary, model=None):
    peers, configurations = [], []
    def factory(path):
        extra = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        engine = chess.engine.SimpleEngine.popen_uci([str(path), "uci"], timeout=3, **extra)
        peers.append(engine)
        row = {"peer": len(peers), "options": []}
        configurations.append(row)
        original = engine.configure
        def configure(options):
            row["options"].append(dict(options))
            return original(options)
        engine.configure = configure
        return engine
    game = None
    with tempfile.TemporaryDirectory(prefix="regicide-real-activity-app-") as directory:
        try:
            game = HumanGame(binary, Path(directory) / "games", factory, factory,
                             neural_model=model, opening_variety_enabled=False)
            wait_for(lambda: game.evaluation().get("depth") is not None)
            first = game.evaluation()
            assert first["fen"] == first["source_fen"] == chess.STARTING_FEN
            assert first["budget_ms"] == 3000
            initial = game.state()["engine"]
            pgn_mode = chess.pgn.read_game(io.StringIO(game.pgn())).headers["Evaluation"]
            coaching_id = game.coaching.snapshot()["engine"]["evaluation_id"]
            game.human_move("e2e4", game.revision)
            wait_for(lambda: peers[1].returncode.done())
            for helper_factory in (game._hints._factory, game.analysis._hints._factory, game.coaching._factory):
                helper = helper_factory(binary)
                helper.quit()
            assert len(peers) == 5
            assert game.movetime_ms == game.hint()["movetime_ms"] == 1000
            assert "UsePieceActivity" not in game.engine_name
            expected_mode = "neural_experimental" if model else "classical" if label == "legacy" else "classical_activity_v1"
            assert initial["evaluation"] == pgn_mode == expected_mode
            if model:
                expected_id = hashlib.sha256(model.read_bytes()).hexdigest()
                assert coaching_id == expected_id
                for row in configurations:
                    assert len(row["options"]) == 1
                    assert set(row["options"][0]) == {"EvalFile", "UseNeuralEvaluation"}
                    assert row["options"][0]["UseNeuralEvaluation"] is True
            elif label == "legacy":
                assert coaching_id == "classical"
                assert all(row["options"] == [] for row in configurations)
            else:
                assert coaching_id == "classical_activity_v1"
                assert peers[0].options["UsePieceActivity"].default is False
                assert all(row["options"] == [{"UsePieceActivity": True}] for row in configurations)
            # A forced single legal root at depth 1 makes the selected static
            # evaluator observable, separately from the cancelled live search.
            board = chess.Board("6k1/3p4/8/8/8/8/4P3/3R2K1 w - - 0 1")
            move = chess.Move.from_uci("d1d3")
            peers[0].configure({"UseQuiescence": False})
            info = peers[0].analyse(board, chess.engine.Limit(depth=1), root_moves=[move])
            assert info["pv"][0] == move
            observed = info["score"].white().score()
            if not model:
                board.push(move)
                assert observed == -classical_cp_stm(board, piece_activity=label != "legacy")
            report["cases"].append({"case": label, "binary": str(binary.relative_to(ROOT)),
                "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                "evaluation": initial["evaluation"], "pgn_evaluation": pgn_mode, "coaching_id": coaching_id,
                "model_sha256": hashlib.sha256(model.read_bytes()).hexdigest() if model else None,
                "first_observed_depth": first["depth"], "evaluation_budget_ms": first["budget_ms"],
                "opponent_budget_ms": game.movetime_ms, "factory_configurations": configurations,
                "depth1_leaf_cp_white": observed, "status": "passed"})
        finally:
            if game is not None:
                game.close()
            for engine in peers:
                engine.close()
                engine.returncode.result(timeout=3)


try:
    candidate = ROOT / "build-eval-activity/regicide.exe"
    check("activity", candidate)
    check("legacy", ROOT / "build/evaluation-quality-v1/baseline/regicide.exe")
    check("explicit_neural", candidate, ROOT / "models/regicide-small-v1.nn")
    report["status"] = "passed"
finally:
    report["finished_utc"] = datetime.now(timezone.utc).isoformat()
    report["play_human_sha256"] = hashlib.sha256((ROOT / "scripts/play_human.py").read_bytes()).hexdigest()
    (OUT / "real-activity-application-result.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
