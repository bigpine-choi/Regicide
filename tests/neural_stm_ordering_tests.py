"""Pair-MSE signs, legacy STE reuse, and validation/anchor-only selection."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import neural_stm as stm
import neural_stm_ordering as ordering


def fixture():
    initial = stm.with_turn_parameters({
        'input_weights': np.zeros((768, 16), dtype=np.float64),
        'hidden_bias': np.zeros(16, dtype=np.float64),
        'output_weights': np.zeros(16, dtype=np.float64)})
    initial['hidden_bias'][0] = 10
    initial['input_weights'][0, 0] = 3
    initial['input_weights'][1, 0] = -1
    initial['output_weights'][0] = 2
    a = np.full((1, 2, 32), 768, dtype=np.int64)
    a[:, 0, :2], a[:, 1, 0] = 0, 1
    b = np.full_like(a, 768)
    b[:, 0, :2], b[:, 1, 0] = 1, 0
    side = np.array([0], dtype=np.int64)
    pairs = (a, np.array([80.]), side, b, np.array([20.]), side)
    config = {'candidate_id': 'tiny-ordering-control', 'input_scale': 32,
              'epochs': 20, 'input_bias_lr': .05, 'output_lr': .1,
              'seed': 20261020, 'batch_size': 1, 'loss': 'mse'}
    return initial, a, side, pairs, config


class OrderingTests(unittest.TestCase):
    def test_zero_weight_exactly_preserves_every_legacy_gradient(self):
        initial, indices, turns, pairs, _ = fixture()
        target = np.array([101.])
        actual = ordering.qat_gradients(initial, indices, target, 32, turns=turns,
                                         pairs=pairs, pair_weight=0)
        expected = stm.qat_gradients(initial, indices, target, 32, turns=turns)
        for key in stm.PARAMETER_KEYS:
            np.testing.assert_array_equal(actual[key], expected[key])

    def test_pair_derivative_uses_pair_count_and_correct_white_and_black_direction(self):
        initial, indices, turns, pairs, _ = fixture()
        predicted = stm.qat_forward(initial, indices, 32, turns=turns)[0]
        for sign in (1, -1):
            pair = (pairs[0], sign * pairs[1], pairs[2],
                    pairs[3], sign * pairs[4], pairs[5])
            actual = ordering.qat_gradients(initial, indices, predicted, 32,
                                             turns=turns, pairs=pair)
            # Integer predictions 14 and -10; hidden differences 7 and -5.
            expected = 2 * (24 - sign * 60) / 10000 * 12
            self.assertAlmostEqual(actual['output_weights'][0], expected, places=15)
            self.assertEqual(actual['output_weights'][0] < 0, sign == 1)
            for value in actual.values():
                self.assertTrue(np.all(np.isfinite(value)))

    def test_swapping_children_is_invariant_and_target_tie_has_zero_gradient(self):
        initial, indices, turns, pairs, _ = fixture()
        predicted = stm.qat_forward(initial, indices, 32, turns=turns)[0]
        swapped = pairs[3:] + pairs[:3]
        a = ordering.qat_gradients(initial, indices, predicted, turns=turns, pairs=pairs)
        b = ordering.qat_gradients(initial, indices, predicted, turns=turns, pairs=swapped)
        tie = (indices, np.array([0.]), turns, indices.copy(), np.array([0.]), turns.copy())
        zero = ordering.qat_gradients(initial, indices, predicted, turns=turns, pairs=tie)
        for key in stm.PARAMETER_KEYS:
            np.testing.assert_array_equal(a[key], b[key])
            np.testing.assert_array_equal(zero[key], np.zeros_like(zero[key]))
        self.assertEqual(ordering.pair_metrics(initial, tie)['target_ties'], 1)

    def train(self, *, anchor_target=100., validation_target=18., patience=3, **kwargs):
        initial, indices, turns, _, config = fixture()
        config.update(kwargs.pop('config', {}))
        tie = (indices, np.array([0.]), turns, indices.copy(), np.array([0.]), turns.copy())
        result = ordering.train_candidate(
            initial, indices, np.array([100.]), config, turns=turns, pairs=tie,
            validation_indices=indices.copy(), validation_targets=np.array([validation_target]),
            validation_turns=turns.copy(), validation_pairs=tie,
            anchor_indices=indices.copy(), anchor_targets=np.array([anchor_target]),
            anchor_turns=turns.copy(), patience=patience, eval_interval=1,
            min_delta_cp2=0., **kwargs)
        return initial, indices, turns, result

    def test_best_checkpoint_restored_and_initial_unchanged(self):
        initial, indices, turns, (result, history, selection) = self.train()
        self.assertEqual(selection['selected_epoch'], 4)
        self.assertEqual(selection['stopped_epoch'], 7)
        self.assertEqual(selection['selected_validation']['mse_cp2'], 0)
        self.assertGreater(selection['last_validation']['mse_cp2'], 0)
        np.testing.assert_array_equal(stm.predict_integer(result, indices, 32, turns=turns), [18])
        reference = fixture()[0]
        for key in stm.PARAMETER_KEYS:
            np.testing.assert_array_equal(initial[key], reference[key])
            self.assertFalse(np.shares_memory(initial[key], result[key]))
        self.assertEqual(len(history), 7)

    def test_anchor_rejects_objective_improvement_and_epoch_zero_survives(self):
        initial, _, _, (result, _, selection) = self.train(anchor_target=14., patience=2)
        self.assertEqual(selection['selected_epoch'], 0)
        self.assertEqual(selection['stopped_epoch'], 2)
        later = selection['validation_evaluations'][1:]
        self.assertTrue(all(row['mse_cp2'] < selection['initial_validation']['mse_cp2']
                            for row in later))
        self.assertTrue(all(not row['eligible'] for row in later))
        for key in stm.PARAMETER_KEYS:
            np.testing.assert_array_equal(initial[key], result[key])

    def test_validation_and_anchor_never_enter_gradients_or_adam_updates(self):
        observed = []
        original = ordering.qat_gradients
        def recording(parameters, indices, targets, *args, **kwargs):
            observed.extend(targets.tolist())
            return original(parameters, indices, targets, *args, **kwargs)
        with patch.object(ordering, 'qat_gradients', side_effect=recording):
            _, _, _, (_, history, selection) = self.train(
                validation_target=-444., anchor_target=-555., patience=100,
                config={'epochs': 5})
        _, _, _, (_, different_history, _) = self.train(
            validation_target=1000., anchor_target=1200., patience=100, config={'epochs': 5})
        self.assertEqual(observed, [100.] * 5)
        self.assertEqual(history, different_history)
        self.assertFalse(selection['validation_used_in_gradients'])
        self.assertFalse(selection['validation_pairs_used_in_gradients'])
        self.assertFalse(selection['anchor_used_in_gradients'])

    def test_callback_cannot_mutate_checkpoint_or_evaluations(self):
        def observer(epoch, parameters, current):
            for value in parameters.values():
                value[:] = 0
            current['point']['mse_cp2'] = -1
        *_, (actual, history, selection) = self.train(progress=observer)
        *_, (reference, other_history, other_selection) = self.train()
        self.assertEqual(history, other_history)
        self.assertEqual(selection, other_selection)
        for key in stm.PARAMETER_KEYS:
            np.testing.assert_array_equal(actual[key], reference[key])

    def test_invalid_weights_pairs_and_guard_settings_rejected(self):
        initial, indices, turns, pairs, _ = fixture()
        for weight in (-1., float('nan'), float('inf'), True, '1'):
            with self.subTest(weight=weight), self.assertRaises(ValueError):
                ordering.qat_gradients(initial, indices, np.array([0.]), turns=turns,
                                         pairs=pairs, pair_weight=weight)
        for bad in ((), pairs[:3], pairs[:3] + (pairs[3][:0], pairs[4][:0], pairs[5][:0]),
                    (pairs[0], np.array([np.nan]), *pairs[2:])):
            with self.assertRaises(ValueError):
                ordering.pair_metrics(initial, bad)
        for bad in ({'patience': 0}, {'enable_turn': 1}, {'progress': 1}):
            with self.assertRaises(ValueError):
                self.train(**bad)
        with self.assertRaisesRegex(ValueError, 'MSE'):
            self.train(config={'loss': 'h100'})


if __name__ == '__main__':
    unittest.main(verbosity=2)
