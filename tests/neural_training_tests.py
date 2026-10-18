"""Learning arithmetic/data-integrity tests; loss reduction is not chess strength."""

import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
# train_neural sets BLAS thread controls before NumPy is imported.
import train_neural as TRAIN
import chess
import numpy as np
import neural_model as NN


def fixture(directory):
    games, positions = [], []
    variations = [
        ("train", ["f2f3", "e7e5", "g2g4", "d8h4"], 2),
        ("validation", ["e2e4", "e7e5", "f1c4", "b8c6", "d1h5", "g8f6", "h5f7"], 4),
        ("test", ["f2f4", "e7e6", "g2g4", "d8h4"], 2)]
    for index, (split, history, ply) in enumerate(variations):
        final = chess.Board()
        for move in history:
            final.push_uci(move)
        outcome = final.outcome(claim_draw=True)
        assert outcome is not None
        game_id, family = f"game-{index}", f"family-{index}"
        games.append({"game_id": game_id, "family": family, "split": split,
                      "initial_fen": chess.STARTING_FEN, "history": history,
                      "completed": True, "result": outcome.result()})
        board = chess.Board()
        for move in history[:ply]:
            board.push_uci(move)
        winner_white = {"1-0": 1, "0-1": -1, "1/2-1/2": 0}[outcome.result()]
        positions.append({"game_id": game_id, "family": family, "split": split,
                          "initial_fen": chess.STARTING_FEN, "history": history[:ply],
                          "fen": board.fen(en_passant="fen"), "ply": ply,
                          "turn": "w" if board.turn else "b", "teacher_cp_stm": 20,
                          "material_cp_stm": NN.material_cp_stm(board),
                          "outcome_stm": winner_white if board.turn else -winner_white,
                          "teacher_depth": 2, "teacher_nodes": 100,
                          "teacher_pv": [next(iter(board.legal_moves)).uci()]})
    manifest = {"schema": TRAIN.SCHEMA, "status": "complete",
                "games_path": "games.jsonl", "positions_path": "positions.jsonl"}
    write_fixture(directory, manifest, games, positions)
    return manifest, games, positions


def write_fixture(directory, manifest, games, positions):
    (directory / "datasetmanifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    for name, rows in (("games", games), ("positions", positions)):
        (directory / f"{name}.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


class ModelTests(unittest.TestCase):
    def test_feature_orientation_own_channels_and_material(self):
        board = chess.Board("4k3/8/8/2r5/8/4P3/8/4K3 w - - 0 1")
        x = NN.encode_features(board)
        self.assertEqual(x.sum(), 4)
        self.assertEqual(x[chess.E3], 1)
        self.assertEqual(x[(6 + chess.ROOK - 1) * 64 + chess.C5], 1)
        self.assertEqual(NN.material_cp_stm(board), -400)
        mirror = board.mirror()
        np.testing.assert_array_equal(x, NN.encode_features(mirror))
        self.assertEqual(NN.material_cp_stm(mirror), -400)

    def test_all_twelve_feature_channels(self):
        board = chess.Board()
        for turn in (True, False):
            board.turn = turn
            x = NN.encode_features(board).reshape(12, 64)
            np.testing.assert_array_equal(x.sum(axis=1), [8, 2, 2, 2, 1, 1] * 2)

    def test_gradient_matches_finite_difference_all_layers(self):
        rng = np.random.default_rng(11)
        model = NN.initialize_model(12)
        model.b1.fill(.2)
        model.b2.fill(.2)
        x = rng.normal(0, .05, (3, 768))
        material, targets = np.array([100., -200., 0.]), np.array([.4, -.2, .1])
        _, gradients = NN.loss_and_gradients(model, x, material, targets)
        epsilon = 1e-6
        for name, parameter in model.parameters().items():
            for index in range(min(4, parameter.size)):
                original = parameter.flat[index]
                parameter.flat[index] = original + epsilon
                plus, _ = NN.loss_and_gradients(model, x, material, targets)
                parameter.flat[index] = original - epsilon
                minus, _ = NN.loss_and_gradients(model, x, material, targets)
                parameter.flat[index] = original
                with self.subTest(name=name, index=index):
                    self.assertAlmostEqual(gradients[name].flat[index], (plus - minus) / (2 * epsilon), places=7)

    def test_rounding_and_cp_clamp(self):
        model = NN.initialize_model(3)
        for parameter in model.parameters().values():
            parameter.fill(0)
        model.b3[0] = 1 / 1200
        self.assertEqual(NN.evaluate_cp(chess.STARTING_FEN, model), 1)
        model.b3[0] = -1 / 1200
        self.assertEqual(NN.evaluate_cp(chess.STARTING_FEN, model), -1)
        model.b3[0] = 100
        self.assertEqual(NN.evaluate_cp(chess.STARTING_FEN, model), 10000)

    def test_export_roundtrip_and_exclusive_create(self):
        model = NN.initialize_model(97)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.nn"
            NN.export_model(model, path)
            restored = NN.load_model(path)
            for name in NN.SHAPES:
                np.testing.assert_array_equal(getattr(model, name), getattr(restored, name))
            self.assertEqual(NN.evaluate_cp(chess.STARTING_FEN, model), NN.evaluate_cp(chess.STARTING_FEN, restored))
            with self.assertRaises(FileExistsError):
                NN.export_model(model, path)

    def test_loader_rejects_bad_header_nonfinite_excess_count_and_size(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.nn"
            NN.export_model(NN.initialize_model(2), path)
            original = path.read_text(encoding="ascii")
            mutations = [original.replace("768", "767", 1),
                         original.replace("768 32", "768\n32", 1),
                         original.replace("16 600\n", "16 600 extra\n", 1), original + " 0",
                         " ".join(original.split()[:-1]),
                         original.rsplit("\n", 2)[0] + "\nnan\n",
                         original.rsplit("\n", 2)[0] + "\n101\n",
                         original.rsplit("\n", 2)[0] + "\n1_0\n",
                         original.rsplit("\n", 2)[0] + "\n1e-9999\n"]
            for malformed in mutations:
                path.write_text(malformed, encoding="ascii")
                with self.subTest(prefix=malformed[:50]), self.assertRaises(ValueError):
                    NN.load_model(path)
            path.write_bytes(b"x" * NN.MAX_FILE_BYTES)
            with self.assertRaises(ValueError):
                NN.load_model(path)

    def test_invalid_training_values_rejected(self):
        model = NN.initialize_model(1)
        for targets in (np.array([np.nan]), np.array([2.])):
            with self.assertRaises(ValueError):
                NN.loss_and_gradients(model, np.zeros((1, 768)), np.zeros(1), targets)
        with self.assertRaises(ValueError):
            NN.loss_and_gradients(model, np.zeros((0, 768)), np.zeros(0), np.zeros(0))
        model.w1[0, 0] = np.inf
        with self.assertRaises(ValueError):
            model.validate()

    def test_clear_synthetic_pattern_is_learned_reproducibly(self):
        x = np.zeros((32, 768))
        x[::2, 0] = 1
        x[1::2, 1] = 1
        targets = np.tile([.5, -.5], 16)
        batch = (x, np.zeros(32), targets)
        batches = {name: batch for name in TRAIN.SPLITS}
        model_a, report_a = TRAIN.train(batches, epochs=80, batch_size=16, seed=23)
        model_b, report_b = TRAIN.train(batches, epochs=80, batch_size=16, seed=23)
        self.assertLess(report_a["metrics"]["test"]["selected_mse"], .01)
        self.assertLess(report_a["metrics"]["train"]["selected_mse"],
                        report_a["metrics"]["train"]["initial_mse"] / 10)
        self.assertEqual(report_a, report_b)
        for name in NN.SHAPES:
            np.testing.assert_array_equal(getattr(model_a, name), getattr(model_b, name))


class DatasetTests(unittest.TestCase):
    def test_full_history_labels_splits_and_provenance_load(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            fixture(directory)
            batches, metadata = TRAIN.load_dataset(directory / "datasetmanifest.json")
            self.assertEqual(metadata["total_games"], 3)
            self.assertEqual(set(batches), set(TRAIN.SPLITS))
            for split in TRAIN.SPLITS:
                self.assertEqual(batches[split][0].shape, (1, 768))
                self.assertEqual(metadata["counts"][split], {"samples": 1, "games": 1, "families": 1})
            self.assertEqual(len(metadata["raw_sha256"]["positions"]), 64)

    def test_wrong_fen_pov_result_material_pv_history_rejected(self):
        changes = [("fen", chess.STARTING_FEN), ("outcome_stm", 1),
                   ("material_cp_stm", 1), ("teacher_pv", ["a1a8"]),
                   ("history", ["a2a5"]), ("teacher_cp_stm", True),
                   ("teacher_depth", 0)]
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            manifest, games, positions = fixture(directory)
            for field, value in changes:
                altered = copy.deepcopy(positions)
                altered[0][field] = value
                write_fixture(directory, manifest, games, altered)
                with self.subTest(field=field), self.assertRaises(ValueError):
                    TRAIN.load_dataset(directory / "datasetmanifest.json")
            wrong_game = copy.deepcopy(games)
            wrong_game[0]["result"] = "1-0"
            write_fixture(directory, manifest, wrong_game, positions)
            with self.assertRaises(ValueError):
                TRAIN.load_dataset(directory / "datasetmanifest.json")

    def test_family_and_position_leakage_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            manifest, games, positions = fixture(directory)
            games[1]["family"] = games[0]["family"]
            positions[1]["family"] = games[0]["family"]
            write_fixture(directory, manifest, games, positions)
            with self.assertRaisesRegex(ValueError, "family"):
                TRAIN.load_dataset(directory / "datasetmanifest.json")
            manifest, games, positions = fixture(directory)
            games[1] = {**games[0], "game_id": "copy", "family": "distinct", "split": "validation"}
            positions[1] = {**positions[0], "game_id": "copy", "family": "distinct", "split": "validation"}
            write_fixture(directory, manifest, games, positions)
            with self.assertRaisesRegex(ValueError, "canonical position"):
                TRAIN.load_dataset(directory / "datasetmanifest.json")

    def test_incomplete_games_excluded_and_empty_split_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            manifest, games, positions = fixture(directory)
            games.append({**games[0], "game_id": "unfinished", "completed": False, "result": "*"})
            positions.append({**positions[0], "game_id": "unfinished"})
            write_fixture(directory, manifest, games, positions)
            _, metadata = TRAIN.load_dataset(directory / "datasetmanifest.json")
            self.assertEqual(metadata["exclusions"], {"incomplete_games": 1, "samples_from_incomplete_games": 1})
            games[1]["completed"] = False
            write_fixture(directory, manifest, games, positions)
            with self.assertRaisesRegex(ValueError, "empty validation"):
                TRAIN.load_dataset(directory / "datasetmanifest.json")

    def test_neural_input_leakage_rejected_despite_different_rights_or_color(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            for mirror in (False, True):
                manifest, games, positions = fixture(directory)
                source = games[0]
                initial = chess.Board(source["initial_fen"])
                if mirror:
                    initial = initial.mirror()
                    history = [chess.Move(chess.square_mirror(chess.Move.from_uci(move).from_square),
                                          chess.square_mirror(chess.Move.from_uci(move).to_square)).uci()
                               for move in source["history"]]
                else:
                    initial.castling_rights = 0
                    history = source["history"]
                games[1] = {**source, "game_id": "feature-copy", "family": "new-family",
                            "split": "validation", "initial_fen": initial.fen(en_passant="fen"),
                            "history": history, "result": "1-0" if mirror else "0-1"}
                board = TRAIN.replay(games[1]["initial_fen"], history[:2])
                positions[1] = {**positions[0], "game_id": "feature-copy", "family": "new-family",
                                "split": "validation", "initial_fen": games[1]["initial_fen"],
                                "history": history[:2], "fen": board.fen(en_passant="fen"),
                                "turn": "w" if board.turn else "b",
                                "teacher_pv": [next(iter(board.legal_moves)).uci()]}
                self.assertNotEqual(TRAIN.position_key(board), TRAIN.position_key(
                    TRAIN.replay(positions[0]["initial_fen"], positions[0]["history"])))
                write_fixture(directory, manifest, games, positions)
                with self.subTest(mirror=mirror), self.assertRaisesRegex(ValueError, "neural feature"):
                    TRAIN.load_dataset(directory / "datasetmanifest.json")

    def test_duplicate_sample_and_path_escape_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            manifest, games, positions = fixture(directory)
            positions.append(positions[0])
            write_fixture(directory, manifest, games, positions)
            with self.assertRaisesRegex(ValueError, "duplicate sample"):
                TRAIN.load_dataset(directory / "datasetmanifest.json")
            manifest["positions_path"] = "../outside.jsonl"
            write_fixture(directory, manifest, games, positions)
            with self.assertRaisesRegex(ValueError, "escapes"):
                TRAIN.load_dataset(directory / "datasetmanifest.json")

    def test_cli_model_report_and_failure_record(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            fixture(directory)
            args = ["--dataset", str(directory / "datasetmanifest.json"),
                    "--output", str(directory / "output"), "--epochs", "2"]
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(TRAIN.main(args), 0)
            report = json.loads((directory / "output/report.json").read_text())
            self.assertEqual(report["status"], "complete")
            self.assertEqual(report["model_sha256"], TRAIN.sha256(directory / "output/small-v1.nn"))
            with self.assertRaises(FileExistsError):
                TRAIN.main(args)
            manifest = json.loads((directory / "datasetmanifest.json").read_text())
            manifest["status"] = "failed"
            (directory / "datasetmanifest.json").write_text(json.dumps(manifest))
            args[3] = str(directory / "failed")
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(TRAIN.main(args), 1)
            report = json.loads((directory / "failed/report.json").read_text())
            self.assertEqual(report["status"], "failed")
            self.assertFalse((directory / "failed/small-v1.nn").exists())


if __name__ == "__main__":
    unittest.main()
