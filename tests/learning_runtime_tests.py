"""Adopted-model runtime selection and real native client identity regressions.

Temporary champion manifests below are test fixtures only. They do not assert
that the shipped experimental model passed a strength arena or publish it into
any user state directory.
"""

import argparse
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import chess
import chess.engine
import chess.pgn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from learning_registry import SCHEMA, sha256
from play_human import HumanGame, runtime_evaluation

BINARY = None
MODEL = ROOT / "models/regicide-small-v1.nn"


class RuntimeSelectionTests(unittest.TestCase):
    def setUp(self):
        if BINARY is None:
            raise RuntimeError("run learning_runtime_tests.py --binary <native engine>")
        self.temporary = tempfile.TemporaryDirectory(prefix="regicide-runtime-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = self.root / "synthetic-registry"
        self.state.mkdir()

    def fixture_champion(self, *, model_bytes=None):
        model = self.state / "models" / "unit-test-only.nn"
        model.parent.mkdir(exist_ok=True)
        model.write_bytes(MODEL.read_bytes() if model_bytes is None else model_bytes)
        document = {"schema": SCHEMA, "generation": 7,
                    "relative_model_path": "models/unit-test-only.nn",
                    "model_sha256": sha256(model), "engine_sha256": sha256(BINARY),
                    "scope": "Synthetic runtime test fixture only; not promotion evidence"}
        self.save_manifest(document)
        return model, document

    def save_manifest(self, value):
        (self.state / "champion.json").write_text(json.dumps(value), encoding="utf-8")

    def test_missing_registry_uses_classical_without_warning(self):
        selected = runtime_evaluation(BINARY, learning_state=self.state)
        self.assertEqual(selected, {"model": None, "generation": None, "warning": None})

    def test_explicit_experimental_model_takes_precedence(self):
        path = self.root / "explicit experiment.nn"
        with patch("learning_registry.load_champion", side_effect=AssertionError("must not inspect registry")):
            selected = runtime_evaluation(BINARY, explicit_model=path, learning_state=self.state)
        self.assertEqual(selected, {"model": path, "generation": None, "warning": None})

    def test_classical_override_does_not_open_corrupt_registry(self):
        (self.state / "champion.json").write_text("broken JSON", encoding="utf-8")
        selected = runtime_evaluation(BINARY, learning_state=self.state, classical=True)
        self.assertEqual(selected, {"model": None, "generation": None, "warning": None})

    def test_classical_and_explicit_model_are_incompatible(self):
        with self.assertRaisesRegex(ValueError, "cannot be combined"):
            runtime_evaluation(BINARY, explicit_model=MODEL, classical=True)

    def test_corrupt_json_and_nonobject_manifests_fail_closed_with_warning(self):
        for text in ("{broken", "[]", "null", '{"schema":"unknown"}'):
            (self.state / "champion.json").write_text(text, encoding="utf-8")
            selected = runtime_evaluation(BINARY, learning_state=self.state)
            self.assertIsNone(selected["model"])
            self.assertIsNone(selected["generation"])
            self.assertIn("기존 평가", selected["warning"])

    def test_changed_model_bytes_fail_closed_with_warning(self):
        model, _ = self.fixture_champion()
        model.write_bytes(model.read_bytes() + b" changed")
        selected = runtime_evaluation(BINARY, learning_state=self.state)
        self.assertIsNone(selected["model"])
        self.assertIn("SHA256", selected["warning"])

    def test_different_engine_binary_identity_fails_closed(self):
        _, manifest = self.fixture_champion()
        manifest["engine_sha256"] = "0" * 64
        self.save_manifest(manifest)
        selected = runtime_evaluation(BINARY, learning_state=self.state)
        self.assertIsNone(selected["model"])
        self.assertIn("different engine", selected["warning"])

    def test_path_traversal_fails_closed(self):
        _, manifest = self.fixture_champion()
        manifest["relative_model_path"] = "../outside.nn"
        self.save_manifest(manifest)
        selected = runtime_evaluation(BINARY, learning_state=self.state)
        self.assertIsNone(selected["model"])
        self.assertTrue(selected["warning"])

    def test_hash_correct_but_native_invalid_model_fails_closed(self):
        self.fixture_champion(model_bytes=b"invalid neural model\n")
        selected = runtime_evaluation(BINARY, learning_state=self.state)
        self.assertIsNone(selected["model"])
        self.assertTrue(selected["warning"])

    def test_real_native_valid_registry_resolves_generation_and_model_identity(self):
        model, manifest = self.fixture_champion()
        selected = runtime_evaluation(BINARY, learning_state=self.state)
        self.assertEqual(selected, {"model": model.resolve(), "generation": 7, "warning": None})
        self.assertEqual(sha256(selected["model"]), manifest["model_sha256"])

    def test_experimental_session_remains_distinct_from_adopted_generation(self):
        game = HumanGame(BINARY, self.root / "experimental-game", neural_model=MODEL)
        try:
            state = game.state()
            self.assertEqual(state["engine"]["evaluation"], "neural_experimental")
            self.assertNotIn("model_generation", state["engine"])
            self.assertIn("학습 평가 실험", state["engine"]["name"])
            pgn = chess.pgn.read_game(io.StringIO(game.pgn()))
            self.assertEqual(pgn.headers["Evaluation"], "neural_experimental")
            self.assertNotIn("ModelGeneration", pgn.headers)
        finally:
            game.close()

    def test_invalid_adopted_generation_rejected_before_launch(self):
        def must_not_launch(_):
            self.fail("invalid adopted identity must not start an engine")
        for generation in (0, -1, True, 1.5):
            with self.assertRaises(ValueError):
                HumanGame(BINARY, self.root / "invalid-generation", engine_factory=must_not_launch,
                          neural_model=MODEL, neural_adopted_generation=generation)
        with self.assertRaises(ValueError):
            HumanGame(BINARY, self.root / "generation-without-model", engine_factory=must_not_launch,
                      neural_adopted_generation=7)

    def test_adopted_main_and_hint_share_immutable_snapshot_and_saved_generation(self):
        model, manifest = self.fixture_champion()
        selected = runtime_evaluation(BINARY, learning_state=self.state)
        configured, peers = [], []

        class RecordingEngine:
            def __init__(self, peer):
                self.peer = peer

            def configure(self, options):
                configured.append(dict(options))
                return self.peer.configure(options)

            def __getattr__(self, name):
                return getattr(self.peer, name)

        def factory(binary):
            peer = chess.engine.SimpleEngine.popen_uci([str(binary), "uci"], timeout=10)
            peers.append(peer)
            return RecordingEngine(peer)

        save_dir = self.root / "adopted-game"
        game = HumanGame(BINARY, save_dir, factory, factory, neural_model=selected["model"],
                         neural_adopted_generation=selected["generation"])
        snapshot = None
        try:
            state = game.new_game("white", 100, game.state()["revision"])
            identity = state["engine"]
            self.assertEqual(identity["evaluation"], "neural_champion")
            self.assertEqual(identity["model_generation"], 7)
            self.assertEqual(identity["model_sha256"], manifest["model_sha256"])
            self.assertIn("검증 통과 학습 평가", identity["name"])
            self.assertEqual(len(configured), 1)
            snapshot = Path(configured[0]["EvalFile"])
            self.assertNotEqual(snapshot.resolve(), model.resolve())
            self.assertEqual(sha256(snapshot), manifest["model_sha256"])

            # Hints are lazy. Replace the registry source *after* opponent load;
            # the subsequently launched advisor must still use the same snapshot.
            model.write_bytes(b"source replaced during active session\n")
            game.set_hint_enabled(True)
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline and game.hint()["status"] == "searching":
                time.sleep(.01)
            hint = game.hint()
            self.assertEqual(hint["status"], "ready", hint)
            self.assertIn(chess.Move.from_uci(hint["move"]), chess.Board(state["fen"]).legal_moves)
            self.assertEqual(len(configured), 2)
            self.assertTrue(all(options["UseNeuralEvaluation"] is True for options in configured))
            self.assertEqual({options["EvalFile"] for options in configured}, {str(snapshot)})
            self.assertEqual(sha256(snapshot), manifest["model_sha256"])

            game.set_hint_enabled(False)
            moved = game.human_move("e2e4", game.state()["revision"])
            moved = game.engine_move(moved["revision"])
            self.assertIsNone(moved["error"])
            self.assertEqual(len(moved["moves"]), 2)
            self.assertEqual(moved["engine"]["model_generation"], 7)
            saved = json.loads((save_dir / f"{moved['game_id']}.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["state"]["engine"]["evaluation"], "neural_champion")
            self.assertEqual(saved["state"]["engine"]["model_generation"], 7)
            self.assertEqual(saved["state"]["engine"]["model_sha256"], manifest["model_sha256"])
            pgn = chess.pgn.read_game(io.StringIO(game.pgn()))
            self.assertFalse(pgn.errors)
            self.assertEqual(pgn.headers["Evaluation"], "neural_champion")
            self.assertEqual(pgn.headers["ModelGeneration"], "7")
            self.assertEqual(pgn.headers["ModelSHA256"], manifest["model_sha256"])
            self.assertEqual(pgn.headers["ModelFile"], model.name)
        finally:
            game.close()
            for peer in peers:
                peer.close()
                peer.returncode.result(timeout=5)
        self.assertIsNotNone(snapshot)
        self.assertFalse(snapshot.exists())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--model", type=Path, default=MODEL)
    arguments, unittest_arguments = parser.parse_known_args()
    BINARY = arguments.binary.resolve(strict=True)
    MODEL = arguments.model.resolve(strict=True)
    unittest.main(argv=[sys.argv[0], *unittest_arguments])
