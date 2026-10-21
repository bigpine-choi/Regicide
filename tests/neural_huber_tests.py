"""Fixed H100 math, scale/normalization and preserved historical MSE training."""
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np
import chess

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import neural_precision as precision
import neural_training as base
from neural_precision_tests import gradient_fixture
import prepare_neural_huber as preparation


class HuberTests(unittest.TestCase):
    def test_exact_inside_outside_and_boundary_values(self):
        residual = np.array([-500., -100., -50., 0., 50., 100., 500.])
        zero = np.zeros_like(residual)
        np.testing.assert_array_equal(precision.loss_terms(residual, zero, 'h100'), [9, 1, .25, 0, .25, 1, 9])
        np.testing.assert_array_equal(precision.loss_terms(residual, zero, 'mse'), [25, 1, .25, 0, .25, 1, 25])
        expected = 2 * np.array([-100., -100., -50., 0., 50., 100., 100.]) / 70000
        np.testing.assert_array_equal(precision.loss_derivative(residual, zero, 'h100'), expected)

    def test_scalar_loss_finite_difference_including_both_boundaries(self):
        for point in (-500., -100., -50., 0., 50., 100., 500.):
            delta = .0001
            plus = precision.loss_terms(np.array([point + delta]), np.zeros(1), 'h100').mean()
            minus = precision.loss_terms(np.array([point - delta]), np.zeros(1), 'h100').mean()
            actual = precision.loss_derivative(np.array([point]), np.zeros(1), 'h100')[0]
            self.assertAlmostEqual(actual, (plus - minus) / (2 * delta), delta=1e-8)

    def test_mean_normalization_and_fixed_small_error_gradient(self):
        values = np.array([50., -100.])
        np.testing.assert_array_equal(precision.loss_derivative(values, np.zeros(2), 'h100'),
                                      precision.loss_derivative(values, np.zeros(2), 'mse'))
        once = precision.loss_derivative(np.array([500.]), np.zeros(1), 'h100')[0]
        twice = precision.loss_derivative(np.array([500., 500.]), np.zeros(2), 'h100')
        np.testing.assert_array_equal(twice, [once / 2, once / 2])

    def test_independent_sparse_h100_ste_including_duplicate_features(self):
        parameters, packed = gradient_fixture()
        # Hand arithmetic: white sums 16,3; black 9,-1; outputs 2,1 ->17cp.
        expected = {key: np.zeros_like(value) for key, value in parameters.items()}
        expected['input_weights'][0, :2] = [.08, .04]
        expected['input_weights'][1, 0] = -.04
        expected['hidden_bias'][1] = .02
        expected['output_weights'][:2] = [.14, .06]
        for scale in (8, 16, 32, 256):
            gradients = precision.qat_gradients(parameters, packed, np.array([-483.]), scale, loss='h100')
            for key in expected:
                np.testing.assert_allclose(gradients[key], expected[key], rtol=0, atol=1e-16)

    def test_inner_h100_and_mse_parameter_gradients_are_exact(self):
        parameters, packed = gradient_fixture()
        for residual in (-100., -50., 0., 50., 100.):
            targets = precision.qat_forward(parameters, packed, 32)[0] - residual
            mse = precision.qat_gradients(parameters, packed, targets, 32)
            robust = precision.qat_gradients(parameters, packed, targets, 32, loss='h100')
            for key in mse:
                np.testing.assert_array_equal(mse[key], robust[key])

    def config(self, loss=None):
        result = {'candidate_id': 'unit', 'input_scale': 32, 'epochs': 20, 'batch_size': 2,
                  'input_bias_lr': .00002, 'output_lr': .005, 'seed': 20261020}
        if loss is not None:
            result['loss'] = loss
        return result

    def batch(self):
        initial, packed = gradient_fixture()
        return initial, np.repeat(packed, 4, axis=0), np.array([-483., 67., 117., -383.])

    def test_explicit_mse_and_observational_callback_preserve_all_arrays(self):
        initial, packed, targets = self.batch()
        original, history = precision.train_candidate(initial, packed, targets, self.config())
        epochs = []
        def observe(epoch, copies, entry):
            epochs.append(epoch)
            for value in copies.values():
                value[:] = 0  # Observational copies cannot change optimizer parameters.
            entry.clear()
        explicit, same_history = precision.train_candidate(initial, packed, targets, self.config('mse'), progress=observe)
        self.assertEqual(epochs, [10, 20])
        self.assertEqual(history, same_history)
        for key in original:
            np.testing.assert_array_equal(original[key], explicit[key])

    def test_h100_deterministic_and_serialization_requantization_exact(self):
        initial, packed, targets = self.batch()
        first, history = precision.train_candidate(initial, packed, targets, self.config('h100'))
        second, repeated = precision.train_candidate(initial, packed, targets, self.config('h100'))
        self.assertEqual(history, repeated)
        for key in first:
            np.testing.assert_array_equal(first[key], second[key])
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'candidate.nnue'
            model = precision.quantize(first, 32)
            precision.save_model(model, path)
            loaded = precision.load_model(path)
            restored = {key: loaded[key].astype(np.float64) / (8 if key == 'output_weights' else 32)
                        for key in precision.PARAMETER_KEYS}
            requantized = precision.quantize(restored, 32)
            for key in precision.PARAMETER_KEYS:
                np.testing.assert_array_equal(model[key], loaded[key])
                np.testing.assert_array_equal(model[key], requantized[key])
            np.testing.assert_array_equal(precision.predict(model, packed), precision.predict(loaded, packed))

    def test_invalid_losses_shapes_and_configurations_are_rejected(self):
        for loss in ('h50', 'huber', '', None, 100, True):
            with self.subTest(loss=loss), self.assertRaises(ValueError):
                precision.loss_terms(np.zeros(1), np.zeros(1), loss)
        for values, targets in (([], []), ([1, 2], [1]), ([[1]], [[1]]), ([np.nan], [0])):
            with self.assertRaises(ValueError):
                precision.loss_terms(np.asarray(values), np.asarray(targets), 'h100')
        initial, packed, targets = self.batch()
        for config in ({**self.config(), 'loss': 'h50'}, {**self.config(), 'delta_cp': 50}):
            with self.assertRaises(ValueError):
                precision.train_candidate(initial, packed, targets, config)

    def test_preparation_freezes_twelve_paired_conditions_without_tuning(self):
        conditions = [preparation.config(arm, loss, seed) for loss in preparation.LOSSES
                      for seed in preparation.SEEDS for arm in preparation.TARGETS]
        self.assertEqual(len({value['candidate_id'] for value in conditions}), 12)
        for value in conditions:
            self.assertEqual((value['epochs'], value['batch_size'], value['input_scale']), (120, 128, 32))
            self.assertEqual((value['input_bias_lr'], value['output_lr']), (.00002, .005))
            self.assertIn(value['seed'], (20261020, 20261021, 20261022))

    def test_paired_target_selection_does_not_mutate_or_renegate_black_rows(self):
        board = chess.Board()
        board.push_uci('e2e4')
        rows = [{'fen': board.fen(), 'classical_static_white_cp': 23, 'target_white_cp': -105}]
        old = dict(rows[0])
        a, y_a = preparation.packed(rows, 'A')
        b, y_b = preparation.packed(rows, 'B')
        np.testing.assert_array_equal(a, b)
        np.testing.assert_array_equal(y_a, [23])
        np.testing.assert_array_equal(y_b, [-105])
        self.assertEqual(rows[0], old)

    def test_special_fixture_generation_preserves_exact_count_and_validity(self):
        fens = preparation.special_positions()
        self.assertEqual(len(fens), 46)
        self.assertTrue(all(chess.Board(fen).is_valid() for fen in fens))


if __name__ == '__main__':
    unittest.main()
