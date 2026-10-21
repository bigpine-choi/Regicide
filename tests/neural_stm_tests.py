"""Turn feature arithmetic, legacy parity and unchanged-MSE trainer checks."""
from __future__ import annotations

import copy
from pathlib import Path
import sys
import tempfile
import unittest

import chess
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import neural_training as base
import neural_precision as precision
import neural_stm as stm


def fixture():
    initial = {"input_weights": np.zeros((768, 16), dtype=np.float64),
               "hidden_bias": np.zeros(16, dtype=np.float64),
               "output_weights": np.zeros(16, dtype=np.float64)}
    initial["hidden_bias"][0] = 10
    initial["input_weights"][0, 0] = 3
    initial["input_weights"][1, 0] = -1
    initial["output_weights"][0] = 2
    indices = np.full((2, 2, 32), 768, dtype=np.int64)
    indices[:, 0, :2] = 0
    indices[:, 1, 0] = 1
    return stm.with_turn_parameters(initial), indices, np.array([0, 1], dtype=np.int64)


class TurnFeatureTests(unittest.TestCase):
    def test_zero_init_preserves_old_predictions_and_common_arrays(self):
        initial = base.bootstrap_float()
        new = stm.with_turn_parameters(initial)
        for key in precision.PARAMETER_KEYS:
            np.testing.assert_array_equal(new[key], initial[key])
            self.assertFalse(np.shares_memory(new[key], initial[key]))
        np.testing.assert_array_equal(new["turn_weights"], np.zeros(16))
        rows = [{"fen": chess.Board().fen(), "target_white_cp": 0}]
        board = chess.Board()
        for move in ("e2e4", "c7c5", "g1f3"):
            board.push_uci(move)
            rows.append({"fen": board.fen(), "target_white_cp": 0})
        packed, _, turns = stm.pack_records(rows)
        for scale in (2, 32, 256):
            np.testing.assert_array_equal(
                stm.predict_integer(new, packed, scale, turns=turns),
                precision.predict(precision.quantize(initial, scale), packed))

    def test_turn_changes_white_score_with_same_piece_input(self):
        parameters, indices, turns = fixture()
        parameters["turn_weights"][0] = 1
        np.testing.assert_array_equal(stm.predict_integer(parameters, indices, 32, turns=turns), [16, 12])
        # Both rows contain exactly the same piece indices; only the mover differs.
        np.testing.assert_array_equal(indices[0], indices[1])

    def test_turn_is_added_before_activation_clipping(self):
        parameters, indices, turns = fixture()
        parameters["hidden_bias"][0] = 125
        parameters["turn_weights"][0] = 10
        # White piece view saturates in both cases; black view saturates only on black's turn.
        np.testing.assert_array_equal(stm.predict_integer(parameters, indices, 32, turns=turns), [6, 0])

    def test_turn_gradient_matches_independent_active_view_derivative(self):
        parameters, indices, turns = fixture()
        parameters["turn_weights"][0] = 1
        targets = np.array([5., -7.])
        gradients = stm.qat_gradients(parameters, indices, targets, 32, turns=turns)
        # MSE derivative: 2*(score-target)/(N*10000), then +/- output weight 2.
        expected = 2 * ((16 - 5) / 10000) - 2 * ((12 + 7) / 10000)
        self.assertAlmostEqual(gradients["turn_weights"][0], expected, places=15)
        np.testing.assert_array_equal(gradients["turn_weights"][1:], np.zeros(15))
        disabled = stm.qat_gradients(parameters, indices, targets, 32, turns=turns, enable_turn=False)
        old = precision.qat_gradients({key: parameters[key] for key in precision.PARAMETER_KEYS}, indices, targets, 32)
        for key in precision.PARAMETER_KEYS:
            np.testing.assert_array_equal(disabled[key], old[key])
        np.testing.assert_array_equal(disabled["turn_weights"], np.zeros(16))

    def test_disabled_turn_trainer_exactly_reproduces_old_adam(self):
        parameters, indices, turns = fixture()
        targets = np.array([800., -700.])
        config = {"candidate_id": "tiny-mse-control", "input_scale": 32, "epochs": 3,
                  "input_bias_lr": .00002, "output_lr": .005,
                  "seed": 20261020, "batch_size": 1, "loss": "mse"}
        common = {key: parameters[key] for key in precision.PARAMETER_KEYS}
        old, old_history = precision.train_candidate(common, indices, targets, config)
        new, new_history = stm.train_candidate(parameters, indices, targets, config, turns=turns, enable_turn=False)
        self.assertEqual(old_history, new_history)
        for key in precision.PARAMETER_KEYS:
            np.testing.assert_array_equal(old[key], new[key])
        np.testing.assert_array_equal(new["turn_weights"], np.zeros(16))

    def test_enabled_turn_is_learned_without_mutating_initializer(self):
        parameters, indices, turns = fixture()
        config = {"candidate_id": "tiny-turn", "input_scale": 32, "epochs": 2,
                  "input_bias_lr": .00002, "output_lr": .005,
                  "seed": 20261020, "batch_size": 1}
        result, _ = stm.train_candidate(parameters, indices, np.array([500., -500.]), config, turns=turns)
        self.assertTrue(np.any(result["turn_weights"] != 0))
        np.testing.assert_array_equal(parameters["turn_weights"], np.zeros(16))
        with self.assertRaisesRegex(ValueError, "MSE"):
            stm.train_candidate(parameters, indices, np.zeros(2), dict(config, loss="h100"), turns=turns)

    def test_color_rank_mirror_reverses_white_score(self):
        parameters = stm.with_turn_parameters(base.bootstrap_float())
        parameters["turn_weights"][:] = np.arange(16) / 8
        model = stm.quantize(parameters, 32)
        board = chess.Board()
        for move in ("e2e4", "c7c5", "g1f3", "d7d6", "d2d4"):
            board.push_uci(move)
            self.assertEqual(stm.quantized_white(board, model), -stm.quantized_white(board.mirror(), model))

    def test_v3_roundtrip_checksum_and_legacy_bytes(self):
        parameters, _, _ = fixture()
        parameters["turn_weights"][:] = np.arange(-8, 8) / 32
        model = stm.quantize(parameters, 32)
        # Assemble the canonical byte stream independently of the writer.
        payload = [3, 769, 16, 4064, 256, 32]
        for key in ("hidden_bias", "output_weights", "input_weights", "turn_weights"):
            payload.extend(int(value) for value in model[key].flat)
        checksum = 14695981039346656037
        for value in payload:
            for byte in (value & 0xFFFFFFFF).to_bytes(4, "little"):
                checksum = ((checksum ^ byte) * 1099511628211) & 0xFFFFFFFFFFFFFFFF
        self.assertEqual(stm.model_checksum(model), checksum)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path = directory / "turn.nnue"
            stm.save_model(model, path)
            loaded = stm.load_model(path)
            for key in stm.PARAMETER_KEYS:
                np.testing.assert_array_equal(loaded[key], model[key])
            with self.assertRaises(FileExistsError):
                stm.save_model(model, path)
            content = path.read_text(encoding="ascii")
            changed = content.rsplit("checksum", 1)[0].splitlines()
            changed[-1] = "0 " + " ".join(changed[-1].split()[1:])
            bad = directory / "bad.nnue"
            bad.write_text("\n".join(changed) + "\nchecksum " + str(checksum) + "\n", encoding="ascii")
            with self.assertRaisesRegex(ValueError, "checksum"):
                stm.load_model(bad)
            common = {key: parameters[key] for key in precision.PARAMETER_KEYS}
            legacy = precision.quantize(common, 32)
            old = directory / "old.nnue"
            copy_path = directory / "copy.nnue"
            precision.save_model(legacy, old)
            stm.save_model(stm.load_model(old), copy_path)
            self.assertEqual(old.read_bytes(), copy_path.read_bytes())
            unsupported = stm.load_model(old)
            unsupported["turn_weights"][0] = 1
            with self.assertRaises(ValueError):
                stm.save_model(unsupported, directory / "lost.nnue")

    def test_invalid_turns_metadata_and_weights_rejected(self):
        parameters, indices, turns = fixture()
        for invalid in (np.array([0, 2]), np.array([True, False]), np.array([0.0, 1.0]), [0]):
            with self.assertRaises(ValueError):
                stm.predict_integer(parameters, indices, 32, turns=invalid)
        model = stm.quantize(parameters, 32)
        for key, value in (("input_scale", 3), ("clip", 4063), ("output_scale", 255), ("format_version", True)):
            with self.assertRaises(ValueError):
                stm.validate_model(dict(model, **{key: value}))
        broken = copy.deepcopy(model)
        broken["turn_weights"][0] = 32768
        with self.assertRaises(ValueError):
            stm.validate_model(broken)


if __name__ == "__main__":
    unittest.main(verbosity=2)
