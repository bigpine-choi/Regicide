"""QAT arithmetic, surrogate gradients, reproducibility, and data isolation.

These tests use hand-computed gradients of the documented STE surrogate. A
finite difference of integer rounding would be zero almost everywhere and
would not test that deliberately chosen surrogate.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import chess
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("neural_qat_under_test", ROOT / "scripts" / "neural_qat.py")
qat = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = qat
SPEC.loader.exec_module(qat)
import neural_training as training
import calibrate_neural_qat as calibration
import neural_qat_experiment as experiment


def parameters() -> dict:
    return {"input_weights": np.zeros((768, 16), dtype=np.float64),
            "hidden_bias": np.zeros(16, dtype=np.float64),
            "output_weights": np.zeros(16, dtype=np.float64)}


def indices(white: list[int], black: list[int]) -> np.ndarray:
    result = np.full((1, 2, 32), 768, dtype=np.int64)
    result[0, 0, :len(white)] = white
    result[0, 1, :len(black)] = black
    return result


def gradient_fixture() -> tuple[dict, np.ndarray]:
    value = parameters()
    value["hidden_bias"][:2] = [10, -1]
    value["input_weights"][0, :2] = [3, 2]
    value["input_weights"][1, :2] = [-1, 0]
    value["output_weights"][:2] = [2, 1]
    return value, indices([0, 0], [1])


def make_dataset(path: Path, validation_target: int = 500) -> None:
    path.mkdir()
    manifest = {"groups": {"train-game": "train", "validation-game": "validation"},
                "partitions": {}, "engine_sha256": "unit-test-synthetic-targets"}
    fixtures = {"train": (["e2e4", "e7e5", "g1f3", "b8c6"], 100),
                "validation": (["d2d4", "d7d5", "c2c4", "e7e6"], validation_target)}
    for name, (moves, target) in fixtures.items():
        board, rows = chess.Board(), []
        for ply, move in enumerate(moves, 1):
            board.push_uci(move)
            rows.append({"fen": board.fen(), "group_id": name + "-game", "ply": ply,
                         "feature_hash": training.feature_identity(board),
                         "target_white_cp": target,
                         "classical_stm_cp": target if board.turn else -target})
        file = path / (name + ".jsonl")
        file.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        manifest["partitions"][name] = {"file": file.name, "count": len(rows),
                                        "sha256": training.sha256(file)}
    training.write_json(path / "split_manifest.json", manifest)


class IntegerForwardTests(unittest.TestCase):
    def test_round_to_even_all_parameter_kinds(self):
        value = parameters()
        value["input_weights"][0, :4] = [0.5, 1.5, -0.5, -1.5]
        value["hidden_bias"][:4] = [0.5, 1.5, -0.5, -1.5]
        value["output_weights"][:4] = np.array([0.5, 1.5, -0.5, -1.5]) / 8
        model = training.quantize(value)
        expected = np.array([0, 2, 0, -2])
        np.testing.assert_array_equal(model["input_weights"][0, :4], expected)
        np.testing.assert_array_equal(model["hidden_bias"][:4], expected)
        np.testing.assert_array_equal(model["output_weights"][:4], expected)
        prediction, _ = qat.qat_forward(value, indices([0], []))
        # Hidden white [0,4,0,0], black [0,2,0,0], numerator 4, 4/8 -> 0.
        np.testing.assert_array_equal(prediction, [0])

    def test_negative_integer_division_and_activation_clipping(self):
        value = parameters()
        value["input_weights"][0, 0] = 1
        value["output_weights"][0] = -7 / 8
        self.assertEqual(qat.qat_forward(value, indices([0], []))[0][0], 0)
        value["output_weights"][0] = -9 / 8
        self.assertEqual(qat.qat_forward(value, indices([0], []))[0][0], -1)
        self.assertEqual(qat.qat_forward(value, indices([], [0]))[0][0], 1)
        value["input_weights"][0, 0] = 100
        value["output_weights"][0] = 1
        self.assertEqual(qat.qat_forward(value, indices([0, 0], []))[0][0], 127)
        value["input_weights"][0, 0] = -100
        self.assertEqual(qat.qat_forward(value, indices([0, 0], []))[0][0], 0)

    def test_final_score_clamp_is_symmetric(self):
        value = parameters()
        value["input_weights"][0, 0] = 100
        value["output_weights"][0] = 1024
        self.assertEqual(qat.qat_forward(value, indices([0], []))[0][0], 28000)
        self.assertEqual(qat.qat_forward(value, indices([], [0]))[0][0], -28000)

    def test_random_shadow_forward_matches_exported_integer_evaluator(self):
        rng = np.random.default_rng(314159)
        value = {"input_weights": rng.uniform(-5, 5, size=(768, 16)),
                 "hidden_bias": rng.uniform(-15, 20, size=16),
                 "output_weights": rng.uniform(-8, 8, size=16)}
        board, rows = chess.Board(), []
        for _ in range(40):
            legal = sorted(board.legal_moves, key=lambda move: move.uci())
            board.push(legal[int(rng.integers(len(legal)))])
            rows.extend([{"fen": board.fen(), "target_white_cp": 0},
                         {"fen": board.mirror().fen(), "target_white_cp": 0}])
        packed, _ = training.pack_records(rows)
        predictions, _ = qat.qat_forward(value, packed)
        model = training.quantize(value)
        expected = [training.quantized_white(chess.Board(row["fen"]), model) for row in rows]
        np.testing.assert_array_equal(predictions, expected)
        np.testing.assert_array_equal(predictions[::2], -predictions[1::2])


class SurrogateGradientTests(unittest.TestCase):
    def test_backward_uses_quantized_outputs_and_truncation_ste(self):
        value = parameters()
        value["hidden_bias"][0] = 10.4  # fake quantization -> 10
        value["input_weights"][0, 0] = .6  # -> 1
        value["output_weights"][0] = .2  # scale8 round -> 2/8 = .25
        packed = indices([0], [])
        prediction, _ = qat.qat_forward(value, packed)
        self.assertEqual(prediction[0], 0)  # raw .25 is truncated to zero
        gradient = qat.qat_gradients(value, packed, np.array([100.0]))
        # dL/dprediction=-.02 despite the locally constant integer forward.
        self.assertAlmostEqual(gradient["input_weights"][0, 0], -.005)
        self.assertAlmostEqual(gradient["output_weights"][0], -.02)
        self.assertEqual(gradient["hidden_bias"][0], 0)

    def test_hand_computed_sparse_scatter_and_shared_bias(self):
        value, packed = gradient_fixture()
        # white sums [16,3], black [9,-1], differences [7,3], output 17.
        prediction, _ = qat.qat_forward(value, packed)
        np.testing.assert_array_equal(prediction, [17])
        actual = qat.qat_gradients(value, packed, np.array([7.0]))
        expected = parameters()
        # dL/dprediction = 2*(17-7)/10000 = .002.
        expected["input_weights"][0, :2] = [.008, .004]  # feature0 occurs twice
        expected["input_weights"][1, :2] = [-.004, 0]  # black view reverses sign
        expected["hidden_bias"][:2] = [0, .002]  # shared view gradients sum
        expected["output_weights"][:2] = [.014, .006]
        for key in expected:
            np.testing.assert_allclose(actual[key], expected[key], rtol=0, atol=1e-15,
                                       err_msg=key)

    def test_batch_mean_scaling(self):
        value, packed = gradient_fixture()
        single = qat.qat_gradients(value, packed, np.array([7.0]))
        repeated = qat.qat_gradients(value, np.repeat(packed, 3, axis=0), np.full(3, 7.0))
        for key in single:
            np.testing.assert_allclose(single[key], repeated[key], rtol=0, atol=1e-15)

    def test_hidden_derivative_excludes_zero_and_clip_boundaries(self):
        for boundary, other in ((0, 1), (127, 126)):
            value = parameters()
            value["input_weights"][0, 0] = boundary
            value["input_weights"][1, 0] = other
            value["output_weights"][0] = 2
            gradients = qat.qat_gradients(value, indices([0], [1]), np.array([0.0]))
            self.assertEqual(gradients["input_weights"][0, 0], 0)
            self.assertNotEqual(gradients["input_weights"][1, 0], 0)
            self.assertEqual(gradients["hidden_bias"][0], gradients["input_weights"][1, 0])

    def test_output_derivative_excludes_exact_clamp_boundaries(self):
        for sign in (-1, 1):
            value = parameters()
            value["input_weights"][0, 0] = 28
            value["output_weights"][0] = sign * 1000
            packed = indices([0], [])
            gradients = qat.qat_gradients(value, packed, np.array([0.0]))
            self.assertTrue(all(not np.any(gradient) for gradient in gradients.values()))
            value["output_weights"][0] = sign * 999
            gradients = qat.qat_gradients(value, packed, np.array([0.0]))
            self.assertNotEqual(gradients["output_weights"][0], 0)

    def test_parameter_clip_ste_keeps_boundary_but_blocks_outside(self):
        for shadow, expected_nonzero in ((127, True), (127.5, False)):
            value = parameters()
            value["hidden_bias"][0] = -126
            value["input_weights"][0, 0] = shadow
            value["output_weights"][0] = 2
            gradient = qat.qat_gradients(value, indices([0], []), np.array([0.0]))
            self.assertEqual(gradient["input_weights"][0, 0] != 0, expected_nonzero)
        for shadow, expected_nonzero in ((1024, True), (1024.5, False)):
            value = parameters()
            value["input_weights"][0, 0] = 1
            value["output_weights"][0] = shadow
            gradient = qat.qat_gradients(value, indices([0], []), np.array([0.0]))
            self.assertEqual(gradient["output_weights"][0] != 0, expected_nonzero)


class TrainingTests(unittest.TestCase):
    CONFIG = {"candidate_id": "unit", "epochs": 5, "input_bias_lr": .2,
              "output_lr": .1, "seed": 42, "batch_size": 1}

    def test_shadow_updates_export_changes_and_fixed_seed(self):
        initial, packed = gradient_fixture()
        original = {key: item.copy() for key, item in initial.items()}
        first, history = qat.train_candidate(initial, packed, np.array([100.0]), dict(self.CONFIG))
        second, repeated = qat.train_candidate(initial, packed, np.array([100.0]), dict(self.CONFIG))
        self.assertEqual(history, repeated)
        self.assertEqual(len(history), self.CONFIG["epochs"])
        old_model, new_model = training.quantize(initial), training.quantize(first)
        for key in initial:
            np.testing.assert_array_equal(initial[key], original[key], err_msg="caller model mutated")
            np.testing.assert_array_equal(first[key], second[key])
            self.assertGreater(np.count_nonzero(first[key] != initial[key]), 0, key)
            self.assertTrue(np.all(np.isfinite(first[key])))
        self.assertGreater(sum(np.count_nonzero(old_model[key] != new_model[key]) for key in initial), 0)

    def test_invalid_training_configuration_rejected(self):
        initial, packed = gradient_fixture()
        for change in ({"epochs": 0}, {"batch_size": 0}, {"input_bias_lr": 0},
                       {"output_lr": float("nan")}, {"seed": True}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                qat.train_candidate(initial, packed, np.array([1.0]), {**self.CONFIG, **change})
        with self.assertRaises(ValueError):
            qat.train_candidate(initial, packed[:0], np.zeros(0), dict(self.CONFIG))

    def test_invalid_shapes_features_and_nonfinite_values_rejected(self):
        initial, packed = gradient_fixture()
        for bad in (packed.astype(np.float64), packed[:, :, :31], packed - 769, packed + 1):
            with self.subTest(shape=bad.shape, dtype=bad.dtype), self.assertRaises(ValueError):
                qat.qat_forward(initial, bad)
        for invalid in (np.array([float("nan")]), np.array([float("inf")]), np.zeros(2)):
            with self.assertRaises(ValueError):
                qat.qat_gradients(initial, packed, invalid)
        initial["output_weights"][0] = float("nan")
        with self.assertRaises(ValueError):
            qat.qat_forward(initial, packed)


class TrainingPartitionTests(unittest.TestCase):
    def test_training_loader_never_reads_validation_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "data"
            make_dataset(path)
            original_read_text = Path.read_text
            def guarded_read_text(file, *args, **kwargs):
                if file.name == "validation.jsonl":
                    raise AssertionError("validation file was accessed while selecting training inputs")
                return original_read_text(file, *args, **kwargs)
            with mock.patch.object(Path, "read_text", guarded_read_text):
                _, rows = qat.load_training_partition(path)
            self.assertEqual(len(rows), 4)
            # Absence or corrupt contents of an unused validation file cannot
            # make training depend on those held-out targets.
            (path / "validation.jsonl").unlink()
            _, again = qat.load_training_partition(path)
            self.assertEqual(rows, again)

    def test_train_hash_and_mirrored_duplicate_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "data"
            make_dataset(path)
            file = path / "train.jsonl"
            original = file.read_text(encoding="utf-8")
            file.write_text(original + "{}\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "checksum|hash"):
                qat.load_training_partition(path)
            rows = [json.loads(line) for line in original.splitlines()]
            mirrored = dict(rows[0])
            board = chess.Board(mirrored["fen"]).mirror()
            mirrored.update({"fen": board.fen(), "target_white_cp": -mirrored["target_white_cp"]})
            rows.append(mirrored)
            file.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            manifest_path = path / "split_manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["partitions"]["train"].update({"count": len(rows), "sha256": training.sha256(file)})
            training.write_json(manifest_path, manifest)
            with self.assertRaisesRegex(ValueError, "duplicate|identity"):
                qat.load_training_partition(path)


class SelectionAndFreezeTests(unittest.TestCase):
    GRID = [{"candidate_id": "unit-a", "epochs": 2, "input_bias_lr": .02,
             "output_lr": .01, "seed": 71, "batch_size": 2},
            {"candidate_id": "unit-b", "epochs": 3, "input_bias_lr": .03,
             "output_lr": .01, "seed": 71, "batch_size": 2}]

    @staticmethod
    def initial_files(path: Path) -> tuple[Path, Path]:
        floats, model = path / "initial.npz", path / "initial.nnue"
        initial = training.bootstrap_float()
        np.savez(floats, **initial)
        training.save_model(training.quantize(initial), model)
        return model, floats

    def test_selection_does_not_access_validation_and_ignores_its_labels(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_dataset(root / "a", 500)
            make_dataset(root / "b", -1800)
            baseline, floats = self.initial_files(root)
            original_open = Path.open
            def guarded_open(file, *args, **kwargs):
                if file.name == "validation.jsonl":
                    raise AssertionError("validation content was accessed during QAT selection")
                return original_open(file, *args, **kwargs)
            with mock.patch.object(qat, "FIXED_GRID", self.GRID), \
                    mock.patch.object(Path, "open", guarded_open):
                first = qat.select_model(root / "a", baseline, floats, root / "out-a")
                second = qat.select_model(root / "b", baseline, floats, root / "out-b")
            self.assertEqual(first, second)
            self.assertEqual((root / "out-a/model.nnue").read_bytes(),
                             (root / "out-b/model.nnue").read_bytes())
            for config in self.GRID:
                candidate = config["candidate_id"]
                self.assertEqual((root / "out-a" / candidate / "metrics.json").read_bytes(),
                                 (root / "out-b" / candidate / "metrics.json").read_bytes())
            freeze = qat.verify_freeze(root / "out-a/freeze.json")
            self.assertEqual(freeze["selected_config"], first["selected_config"])
            self.assertEqual((root / "out-a/model.nnue").read_text(encoding="ascii").splitlines()[0],
                             "REGICIDE_NNUE 1 768 16 127 8")

    def test_freeze_detects_model_config_data_edits_and_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_dataset(root / "data")
            baseline, floats = self.initial_files(root)
            output = root / "selected"
            with mock.patch.object(qat, "FIXED_GRID", self.GRID):
                qat.select_model(root / "data", baseline, floats, output)
                original = {file.name: file.read_bytes() for file in output.iterdir() if file.is_file()}
                with self.assertRaisesRegex(ValueError, "overwrite"):
                    qat.select_model(root / "data", baseline, floats, output)
            self.assertEqual(original, {file.name: file.read_bytes() for file in output.iterdir() if file.is_file()})
            freeze_path = output / "freeze.json"
            for file in (output / "model.nnue", output / "selection.json",
                         output / "grid_plan.json", root / "data/train.jsonl",
                         root / "data/split_manifest.json"):
                saved = file.read_bytes()
                # Valid trailing whitespace still changes frozen artifact bytes.
                file.write_bytes(saved + b"\n")
                with self.subTest(file=file.name), self.assertRaisesRegex(ValueError, "checksum"):
                    qat.verify_freeze(freeze_path)
                file.write_bytes(saved)
                qat.verify_freeze(freeze_path)

    def test_baseline_float_checkpoint_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_dataset(root / "data")
            baseline, floats = self.initial_files(root)
            changed = training.bootstrap_float()
            changed["input_weights"][0, 0] += 2
            np.savez(floats, **changed)
            with self.assertRaisesRegex(ValueError, "baseline"):
                qat.select_model(root / "data", baseline, floats, root / "selected")
            self.assertFalse((root / "selected").exists())


class CalibrationPlanTests(unittest.TestCase):
    def test_grid_rejects_count_duplicate_directory_ids_and_bad_config(self):
        valid = SelectionAndFreezeTests.GRID[0]
        invalid = [[], [valid] * 17, {}, [valid, valid],
                   [{**valid, "candidate_id": "../escape"}],
                   [{**valid, "candidate_id": "nested/path"}],
                   [{**valid, "epochs": 0}], [{**valid, "seed": True}]]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = root / "plan.json"
            for grid in invalid:
                training.write_json(plan, {"candidate_grid": grid})
                with self.subTest(grid=grid), mock.patch.object(calibration.qat, "select_model") as select:
                    with self.assertRaises(ValueError):
                        calibration.select_from_plan(plan, root / "unused-data", root / "unused-model",
                                                     root / "unused-floats", root / "unused-output")
                    select.assert_not_called()
            self.assertFalse((root / "unused-output").exists())

    def test_grid_object_restored_after_training_exception(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = root / "plan.json"
            training.write_json(plan, {"candidate_grid": SelectionAndFreezeTests.GRID})
            original_grid = calibration.qat.FIXED_GRID
            def failing_selection(*args):
                self.assertEqual(calibration.qat.FIXED_GRID, SelectionAndFreezeTests.GRID)
                raise RuntimeError("forced training failure")
            with mock.patch.object(calibration.qat, "select_model", side_effect=failing_selection):
                with self.assertRaisesRegex(RuntimeError, "forced training failure"):
                    calibration.select_from_plan(plan, root / "unused-data", root / "unused-model",
                                                 root / "unused-floats", root / "unused-output")
            self.assertIs(calibration.qat.FIXED_GRID, original_grid)

    def test_real_plan_metadata_restoration_and_postfreeze_guards(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_dataset(root / "data")
            baseline, floats = SelectionAndFreezeTests.initial_files(root)
            plan = root / "plan.json"
            training.write_json(plan, {"candidate_grid": SelectionAndFreezeTests.GRID,
                                       "basis": "tiny synthetic training-only test"})
            original_grid = calibration.qat.FIXED_GRID
            result = calibration.select_from_plan(plan, root / "data", baseline, floats, root / "selected")
            self.assertIs(calibration.qat.FIXED_GRID, original_grid)
            output = root / "selected"
            record_path = output / "calibration_driver.json"
            record = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertEqual(record["plan"]["sha256"], training.sha256(plan))
            self.assertEqual(record["driver"]["sha256"], training.sha256(Path(calibration.__file__)))
            self.assertEqual(record["freeze_sha256"], training.sha256(output / "freeze.json"))
            frozen = experiment.frozen_models(output)
            self.assertEqual(frozen["selected_candidate_id"], result["selected_candidate_id"])
            model_bytes = (output / "model.nnue").read_bytes()
            original_plan = plan.read_bytes()
            plan.write_bytes(original_plan + b"\n")
            with self.assertRaisesRegex(ValueError, "calibration.*plan"):
                experiment.frozen_models(output)
            plan.write_bytes(original_plan)
            original_record = record_path.read_bytes()
            for role, pattern in (("driver", "calibration.*driver"), ("freeze", "calibration freeze")):
                changed = json.loads(original_record)
                if role == "freeze":
                    changed["freeze_sha256"] = "0" * 64
                else:
                    changed[role]["sha256"] = "0" * 64
                training.write_json(record_path, changed)
                with self.subTest(role=role), self.assertRaisesRegex(ValueError, pattern):
                    experiment.frozen_models(output)
                record_path.write_bytes(original_record)
            experiment.frozen_models(output)
            self.assertEqual(model_bytes, (output / "model.nnue").read_bytes())


if __name__ == "__main__":
    unittest.main()
