"""Independent arithmetic, STE, provenance, and training tests for format v2.

Rounding has zero mathematical derivative almost everywhere. Gradient checks
below independently differentiate the documented straight-through surrogate,
including cancellation of the input precision scale, rather than finite
differences of the deployed integer function.
"""
from __future__ import annotations

import copy
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
import neural_training as base
import neural_precision as precision


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


def checksum_independent(model: dict) -> int:
    """Canonical byte stream assembled without calling either implementation."""
    version = int(model.get("format_version", 1))
    header = [version, 768, 16, int(model.get("clip", 127)), int(model["output_scale"])]
    if version == 2:
        header.append(int(model["input_scale"]))
    values = header + [int(item) for key in ("hidden_bias", "output_weights", "input_weights")
                       for item in np.asarray(model[key]).flat]
    result = 14695981039346656037
    for value in values:
        for byte in (value & 0xFFFFFFFF).to_bytes(4, "little"):
            result = ((result ^ byte) * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return result


def sparse_surrogate_independent(value: dict, packed: np.ndarray, targets: np.ndarray,
                                 scale: int) -> dict:
    """Scalar loops deliberately avoid forward/backward implementation helpers."""
    model = precision.quantize(value, scale)
    expected = parameters()
    clip = 127 * scale
    for batch, target in enumerate(targets):
        sums, activations = [], []
        for view in range(2):
            sums.append([int(model["hidden_bias"][hidden])
                         + sum(int(model["input_weights"][feature, hidden])
                               for feature in packed[batch, view] if feature != 768)
                         for hidden in range(16)])
            activations.append([max(0, min(clip, number)) for number in sums[-1]])
        numerator = sum((activations[0][h] - activations[1][h])
                        * int(model["output_weights"][h]) for h in range(16))
        raw = numerator / (8 * scale)
        prediction = max(-28000, min(28000, int(raw)))
        derivative = 2 * (prediction - target) / (len(targets) * 10000)
        if not -28000 < raw < 28000:
            continue
        for hidden in range(16):
            expected["output_weights"][hidden] += derivative * (
                activations[0][hidden] - activations[1][hidden]) / scale
            for view, sign in ((0, 1), (1, -1)):
                if not 0 < sums[view][hidden] < clip:
                    continue
                # d round(S*x)/dx=S, cancelled by the final 8*S denominator.
                gradient = derivative * sign * int(model["output_weights"][hidden]) / 8
                expected["hidden_bias"][hidden] += gradient
                for feature in packed[batch, view]:
                    if feature != 768:
                        expected["input_weights"][feature, hidden] += gradient
    bounds = {"input_weights": 32767 / scale, "hidden_bias": 1048576 / scale,
              "output_weights": 1024}
    for key in expected:
        expected[key] *= np.abs(value[key]) <= bounds[key]
    return expected


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
                         "feature_hash": base.feature_identity(board), "target_white_cp": target,
                         "classical_stm_cp": target if board.turn else -target})
        file = path / (name + ".jsonl")
        file.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        manifest["partitions"][name] = {"file": file.name, "count": len(rows),
                                        "sha256": base.sha256(file)}
    base.write_json(path / "split_manifest.json", manifest)


class IntegerModelTests(unittest.TestCase):
    def test_v2_ties_to_even_and_physical_units(self):
        value = parameters()
        value["input_weights"][0, :4] = np.array([.5, 1.5, -.5, -1.5]) / 16
        value["hidden_bias"][:4] = np.array([.5, 1.5, -.5, -1.5]) / 16
        value["output_weights"][:4] = np.array([.5, 1.5, -.5, -1.5]) / 8
        model = precision.quantize(value, 16)
        for key in ("hidden_bias", "output_weights"):
            np.testing.assert_array_equal(model[key][:4], [0, 2, 0, -2])
        np.testing.assert_array_equal(model["input_weights"][0, :4], [0, 2, 0, -2])
        self.assertEqual((model["format_version"], model["input_scale"], model["clip"],
                          model["output_scale"]), (2, 16, 2032, 128))

    def test_signed_truncation_is_once_after_both_views(self):
        value = parameters()
        value["input_weights"][0, 0] = 15 / 16
        value["input_weights"][1, 0] = 9 / 16
        value["output_weights"][0] = 1.5
        # White 1.40625, black .84375: subtract before division -> zero.
        self.assertEqual(precision.qat_forward(value, indices([0], [1]), 16)[0][0], 0)
        value["input_weights"][0, 0] = 17 / 16
        value["output_weights"][0] = -1
        self.assertEqual(precision.qat_forward(value, indices([0], []), 16)[0][0], -1)
        self.assertEqual(precision.qat_forward(value, indices([], [0]), 16)[0][0], 1)

    def test_activation_clip_and_64bit_numerator(self):
        value = parameters()
        value["input_weights"][0] = 127
        value["output_weights"][:] = 1024
        prediction, cache = precision.qat_forward(value, indices([0, 0], []), 256)
        self.assertEqual(prediction[0], 28000)
        self.assertEqual(precision.qat_forward(value, indices([], [0, 0]), 256)[0][0], -28000)
        model = precision.quantize(value, 256)
        # This legal format-v2 numerator exceeds signed 32 bit.
        self.assertGreater(127 * 256 * 8192 * 16, 2**31 - 1)
        self.assertEqual(int(precision.predict(model, indices([0, 0], []))[0]), 28000)
        value["input_weights"][0] = -127
        self.assertEqual(precision.qat_forward(value, indices([0], []), 256)[0][0], 0)

    def test_invalid_precision_clip_denominator_and_bounds_rejected(self):
        for invalid in (0, 1, 3, 257, 512, True, 16.0):
            with self.subTest(scale=invalid), self.assertRaises(ValueError):
                precision.quantize(parameters(), invalid)
        original = precision.quantize(parameters(), 16)
        for key, bad in (("format_version", 3), ("input_scale", 3), ("clip", 127),
                         ("output_scale", 127), ("output_scale", 0), ("output_scale", 65552)):
            model = copy.deepcopy(original)
            model[key] = bad
            with self.subTest(key=key, bad=bad), self.assertRaises(ValueError):
                precision.validate_model(model)
        for key, bound in (("input_weights", 32767), ("hidden_bias", 1048576),
                           ("output_weights", 8192)):
            model = copy.deepcopy(original)
            model[key].flat[0] = bound + 1
            with self.subTest(key=key), self.assertRaises(ValueError):
                precision.validate_model(model)
        accepted = copy.deepcopy(original)
        accepted["output_scale"] = 16  # Loader accepts other divisible denominators.
        precision.validate_model(accepted)

    def test_v2_checksum_bounds_roundtrip_and_corruption(self):
        model = precision.quantize(parameters(), 256)
        for key, bound in (("input_weights", 32767), ("hidden_bias", 1048576),
                           ("output_weights", 8192)):
            model[key].flat[:2] = [-bound, bound]
        self.assertEqual(precision.model_checksum(model), checksum_independent(model))
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "model.nnue"
            precision.save_model(model, path)
            self.assertEqual(path.read_text(encoding="ascii").splitlines()[0],
                             "REGICIDE_NNUE 2 768 16 32512 2048 256")
            loaded = precision.load_model(path)
            for key in model:
                np.testing.assert_array_equal(model[key], loaded[key])
            repeated = path.with_name("roundtrip.nnue")
            precision.save_model(loaded, repeated)
            self.assertEqual(path.read_bytes(), repeated.read_bytes())
            with self.assertRaisesRegex(ValueError, "overwrite"):
                precision.save_model(model, path)
            tokens = path.read_text(encoding="ascii").split()
            tokens[-1] = str(int(tokens[-1]) ^ 1)
            path.write_text(" ".join(tokens), encoding="ascii")
            with self.assertRaisesRegex(ValueError, "checksum"):
                precision.load_model(path)

    def test_v1_bytes_checksum_and_scores_remain_identical(self):
        model = base.quantize(base.bootstrap_float())
        with tempfile.TemporaryDirectory() as temporary:
            old, new = Path(temporary) / "v1.nnue", Path(temporary) / "v1-again.nnue"
            base.save_model(model, old)
            loaded = precision.load_model(old)
            precision.save_model(loaded, new)
            self.assertEqual(old.read_bytes(), new.read_bytes())
            self.assertEqual(precision.model_checksum(loaded), base.model_checksum(model))
            self.assertEqual(precision.model_checksum(loaded), checksum_independent(loaded))
            board = chess.Board()
            for move in ("e2e4", "d7d5", "e4d5", "d8d5"):
                board.push_uci(move)
                self.assertEqual(base.quantized_white(board, model),
                                 precision.quantized_white(board, loaded))

    def test_exact_rescaling_is_a_control_without_learning(self):
        legacy = base.quantize(base.bootstrap_float())
        board, rows = chess.Board(), []
        for text in ("e2e4", "d7d5", "e4d5", "d8d5", "b1c3", "d5e5"):
            board.push_uci(text)
            rows.append({"fen": board.fen(), "target_white_cp": 0})
        packed, _ = base.pack_records(rows)
        expected = [base.quantized_white(chess.Board(row["fen"]), legacy) for row in rows]
        for scale in (2, 16, 256):
            converted = precision.rescale_model(legacy, scale)
            for key in ("input_weights", "hidden_bias"):
                np.testing.assert_array_equal(converted[key], legacy[key] * scale)
            np.testing.assert_array_equal(converted["output_weights"], legacy["output_weights"])
            np.testing.assert_array_equal(precision.predict(converted, packed), expected)
        with self.assertRaises(ValueError):
            precision.rescale_model(precision.rescale_model(legacy, 16), 8)

    def test_loader_rejects_oversized_integers_and_malformed_headers(self):
        model = precision.quantize(parameters(), 16)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "model.nnue"
            precision.save_model(model, path)
            original = path.read_text(encoding="ascii").split()
            malformed = []
            for offset, text in ((1, "3"), (2, "769"), (4, "127"), (5, "127"),
                                 (6, "3"), (7, str(2**63)), (-1, "-1"),
                                 (-1, str(2**64)), (7, "1.0")):
                changed = original.copy()
                changed[offset] = text
                malformed.append(changed)
            malformed.extend([original[:-1], original + ["trailing"]])
            for tokens in malformed:
                path.write_text(" ".join(tokens) + "\n", encoding="ascii")
                with self.subTest(header=tokens[:8]), self.assertRaises(ValueError):
                    precision.load_model(path)
            path.write_bytes(b" " * (base.MODEL_LIMIT + 1))
            with self.assertRaisesRegex(ValueError, "size"):
                precision.load_model(path)

    def test_random_shadow_export_board_and_mirror_agree(self):
        rng = np.random.default_rng(41031)
        value = {"input_weights": rng.uniform(-4, 4, (768, 16)),
                 "hidden_bias": rng.uniform(-5, 20, 16),
                 "output_weights": rng.uniform(-8, 8, 16)}
        board, rows = chess.Board(), []
        for _ in range(32):
            legal = sorted(board.legal_moves, key=lambda move: move.uci())
            board.push(legal[int(rng.integers(len(legal)))])
            rows.extend([{"fen": board.fen(), "target_white_cp": 0},
                         {"fen": board.mirror().fen(), "target_white_cp": 0}])
        packed, _ = base.pack_records(rows)
        for scale in (2, 16, 256):
            model = precision.quantize(value, scale)
            actual, _ = precision.qat_forward(value, packed, scale)
            expected = [precision.quantized_white(chess.Board(row["fen"]), model) for row in rows]
            np.testing.assert_array_equal(actual, expected)
            np.testing.assert_array_equal(actual[::2], -actual[1::2])


class SurrogateTests(unittest.TestCase):
    def test_hand_computed_gradient_scale_cancels(self):
        value, packed = gradient_fixture()
        expected = parameters()
        expected["input_weights"][0, :2] = [.008, .004]
        expected["input_weights"][1, :2] = [-.004, 0]
        expected["hidden_bias"][:2] = [0, .002]
        expected["output_weights"][:2] = [.014, .006]
        for scale in (2, 8, 16, 256):
            np.testing.assert_array_equal(precision.qat_forward(value, packed, scale)[0], [17])
            actual = precision.qat_gradients(value, packed, np.array([7.0]), scale)
            for key in expected:
                np.testing.assert_allclose(actual[key], expected[key], rtol=0, atol=1e-15)

    def test_fractional_shadow_uses_quantized_outputs_in_backward(self):
        value = parameters()
        value["hidden_bias"][0] = 10.4
        value["input_weights"][0, 0] = .6
        value["output_weights"][0] = .2
        actual = precision.qat_gradients(value, indices([0], []), np.array([100.0]), 16)
        self.assertEqual(precision.qat_forward(value, indices([0], []), 16)[0][0], 0)
        self.assertAlmostEqual(actual["input_weights"][0, 0], -.005)
        self.assertAlmostEqual(actual["output_weights"][0], -.0125)
        self.assertEqual(actual["hidden_bias"][0], 0)

    def test_independent_sparse_loop_repeated_features_batch_and_scales(self):
        rng = np.random.default_rng(61041)
        value = {"input_weights": rng.uniform(-2, 2, (768, 16)),
                 "hidden_bias": rng.uniform(-2, 4, 16),
                 "output_weights": rng.uniform(-3, 3, 16)}
        packed = np.full((3, 2, 32), 768, dtype=np.int64)
        packed[:, :, :7] = rng.integers(0, 9, size=(3, 2, 7))
        targets = np.array([17.0, -23.0, 100.0])
        for scale in (2, 16, 256):
            actual = precision.qat_gradients(value, packed, targets, scale)
            expected = sparse_surrogate_independent(value, packed, targets, scale)
            for key in expected:
                np.testing.assert_allclose(actual[key], expected[key], rtol=0, atol=1e-14,
                                           err_msg=key)

    def test_relu_and_score_endpoint_derivatives_are_zero(self):
        for boundary, other in ((0, 1), (127, 126)):
            value = parameters()
            value["input_weights"][0, 0] = boundary
            value["input_weights"][1, 0] = other
            value["output_weights"][0] = 2
            actual = precision.qat_gradients(value, indices([0], [1]), np.array([0.0]), 16)
            self.assertEqual(actual["input_weights"][0, 0], 0)
            self.assertNotEqual(actual["input_weights"][1, 0], 0)
        for sign in (-1, 1):
            value = parameters()
            value["input_weights"][0, 0] = 28
            value["output_weights"][0] = sign * 1000
            actual = precision.qat_gradients(value, indices([0], []), np.array([0.0]), 16)
            self.assertTrue(all(not np.any(item) for item in actual.values()))

    def test_parameter_clip_blocks_only_outside_physical_bounds(self):
        scale = 256
        for shadow, active in ((32767 / scale, True), (32767 / scale + 1 / scale, False)):
            value = parameters()
            value["hidden_bias"][0] = -126
            value["input_weights"][0, 0] = shadow
            value["output_weights"][0] = 2
            actual = precision.qat_gradients(value, indices([0], []), np.array([0.0]), scale)
            self.assertEqual(actual["input_weights"][0, 0] != 0, active)


class TrainingTests(unittest.TestCase):
    CONFIG = {"candidate_id": "unit", "input_scale": 16, "epochs": 5,
              "input_bias_lr": .2, "output_lr": .1, "seed": 42, "batch_size": 1}

    def test_seed_repeat_and_input_changes_against_same_scale_reference(self):
        initial, packed = gradient_fixture()
        original = copy.deepcopy(initial)
        first, history = precision.train_candidate(initial, packed, np.array([100.0]), self.CONFIG)
        second, repeated = precision.train_candidate(initial, packed, np.array([100.0]), self.CONFIG)
        self.assertEqual(history, repeated)
        self.assertEqual(len(history), self.CONFIG["epochs"])
        old_model = precision.quantize(initial, 16)
        new_model = precision.quantize(first, 16)
        self.assertGreater(np.count_nonzero(old_model["input_weights"] != new_model["input_weights"]), 0)
        for key in first:
            np.testing.assert_array_equal(initial[key], original[key])
            np.testing.assert_array_equal(first[key], second[key])
            self.assertGreater(np.count_nonzero(first[key] != initial[key]), 0)

    def test_invalid_config_batch_and_nonfinite_parameters(self):
        value, packed = gradient_fixture()
        for edit in ({"epochs": 0}, {"input_scale": 3}, {"seed": True},
                     {"output_lr": float("nan")}, {"batch_size": 0}):
            with self.subTest(edit=edit), self.assertRaises(ValueError):
                precision.train_candidate(value, packed, np.array([1.0]), {**self.CONFIG, **edit})
        for bad in (packed.astype(float), packed[:, :, :31], packed - 769, packed + 1):
            with self.assertRaises(ValueError):
                precision.qat_forward(value, bad, 16)
        for target in (np.array([float("nan")]), np.array([float("inf")]), np.zeros(2)):
            with self.assertRaises(ValueError):
                precision.qat_gradients(value, packed, target, 16)
        value["hidden_bias"][0] = float("nan")
        with self.assertRaises(ValueError):
            precision.quantize(value, 16)


class SelectionAndFreezeTests(unittest.TestCase):
    GRID = [{"candidate_id": "unit-a", "input_scale": 8, "epochs": 4,
             "input_bias_lr": .03, "output_lr": .01, "seed": 71, "batch_size": 2},
            {"candidate_id": "unit-b", "input_scale": 16, "epochs": 6,
             "input_bias_lr": .05, "output_lr": .01, "seed": 71, "batch_size": 2}]

    @staticmethod
    def initial_files(path: Path) -> tuple[Path, Path]:
        floats, model = path / "initial.npz", path / "initial.nnue"
        initial = base.bootstrap_float()
        np.savez(floats, **initial)
        base.save_model(base.quantize(initial), model)
        return model, floats

    def select(self, root: Path, dataset: str = "data", output: str = "selected") -> dict:
        with mock.patch.object(precision, "FIXED_GRID", self.GRID):
            return precision.select_model(root / dataset, root / "initial.nnue",
                                          root / "initial.npz", root / output)

    def test_selection_never_opens_validation_or_previous_holdout_labels(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_dataset(root / "a", 500)
            make_dataset(root / "b", -1800)
            self.initial_files(root)
            original_open = Path.open
            def guarded_open(file, *args, **kwargs):
                if file.name in {"validation.jsonl", "holdout.jsonl", "holdout-comparison.json"}:
                    raise AssertionError("held-out contents were accessed during selection")
                return original_open(file, *args, **kwargs)
            with mock.patch.object(Path, "open", guarded_open):
                first = self.select(root, "a", "out-a")
                second = self.select(root, "b", "out-b")
            self.assertEqual(first, second)
            self.assertEqual((root / "out-a/model.nnue").read_bytes(),
                             (root / "out-b/model.nnue").read_bytes())
            frozen = precision.verify_freeze(root / "out-a/freeze.json")
            self.assertEqual(frozen["selected_config"], first["selected_config"])
            selected = precision.load_model(root / "out-a/model.nnue")
            initial = precision.load_float_parameters(root / "initial.npz")
            reference = precision.quantize(initial, selected["input_scale"])
            self.assertGreater(np.count_nonzero(selected["input_weights"] != reference["input_weights"]), 0)

    def test_freeze_detects_each_bound_file_and_config_tamper(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_dataset(root / "data")
            self.initial_files(root)
            self.select(root)
            output = root / "selected"
            frozen = precision.verify_freeze(output / "freeze.json")
            for file in (output / "model.nnue", output / "selection.json", output / "grid_plan.json",
                         root / "data/train.jsonl", root / "data/split_manifest.json"):
                saved = file.read_bytes()
                file.write_bytes(saved + b"\n")
                with self.subTest(file=file.name), self.assertRaisesRegex(ValueError, "checksum|hash"):
                    precision.verify_freeze(output / "freeze.json")
                file.write_bytes(saved)
                precision.verify_freeze(output / "freeze.json")
            freeze_path = output / "freeze.json"
            original = freeze_path.read_bytes()
            frozen["selected_config"]["input_scale"] = 256
            base.write_json(freeze_path, frozen)
            with self.assertRaises(ValueError):
                precision.verify_freeze(freeze_path)
            freeze_path.write_bytes(original)
            with self.assertRaisesRegex(ValueError, "overwrite"):
                self.select(root)

    def test_explicit_plan_and_plan_tamper_are_bound_to_freeze(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_dataset(root / "data")
            self.initial_files(root)
            plan = root / "plan.json"
            base.write_json(plan, {"candidate_grid": self.GRID, "purpose": "tiny synthetic test"})
            precision.select_model(root / "data", root / "initial.nnue", root / "initial.npz",
                                   root / "selected", plan_path=plan)
            precision.verify_freeze(root / "selected/freeze.json")
            plan.write_bytes(plan.read_bytes() + b"\n")
            with self.assertRaisesRegex(ValueError, "checksum|hash"):
                precision.verify_freeze(root / "selected/freeze.json")

    def test_no_integer_input_update_cannot_be_frozen(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_dataset(root / "data")
            self.initial_files(root)
            initial = precision.load_float_parameters(root / "initial.npz")
            with mock.patch.object(precision, "FIXED_GRID", self.GRID), \
                    mock.patch.object(precision, "train_candidate", return_value=(initial, [])):
                with self.assertRaisesRegex(ValueError, "input"):
                    self.select(root)
            self.assertFalse((root / "selected/freeze.json").exists())


if __name__ == "__main__":
    unittest.main()
