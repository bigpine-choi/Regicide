"""Warm-output MSE derivative, training-only replay, quantization, and guards."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import neural_stm as stm
import neural_stm_ordering as ordering
import neural_stm_preservation as preservation


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
    turns = np.array([0], dtype=np.int64)
    tie = (a, np.array([0.]), turns, a.copy(), np.array([0.]), turns.copy())
    config = {'candidate_id': 'tiny-preservation-control', 'input_scale': 32,
              'epochs': 20, 'input_bias_lr': .05, 'output_lr': .1,
              'seed': 20261020, 'batch_size': 1, 'loss': 'mse'}
    return initial, a, b, turns, tie, config


def train_fixture(*, weight=4., validation_target=18., anchor_target=100.,
                  patience=3, epochs=20, callback=None):
    initial, a, _, turns, pairs, config = fixture()
    config['epochs'] = epochs
    args = dict(turns=turns, pairs=pairs, validation_indices=a.copy(),
                validation_targets=np.array([validation_target]),
                validation_turns=turns.copy(), validation_pairs=pairs,
                anchor_indices=a.copy(), anchor_targets=np.array([anchor_target]),
                anchor_turns=turns.copy(), patience=patience, eval_interval=1,
                min_delta_cp2=0., progress=callback)
    result = preservation.train_candidate(
        initial, a, np.array([100.]), config,
        preservation_indices=a.copy(), preservation_turns=turns.copy(),
        preservation_weight=weight, **args)
    return initial, a, turns, config, args, result


class PreservationTests(unittest.TestCase):
    def test_added_mse_gradient_matches_existing_ste_and_finite_output_surrogate(self):
        initial, a, b, turns, pairs, _ = fixture()
        replay = np.concatenate([a, b])
        replay_turns = np.concatenate([turns, turns])
        frozen = stm.qat_forward(initial, replay, turns=replay_turns)[0]
        current = {key: value.copy() for key, value in initial.items()}
        current['output_weights'][0] += 1
        target = np.array([101.])
        combined = preservation.qat_gradients(
            current, a, target, turns=turns, pairs=pairs,
            preservation_indices=replay, preservation_targets=frozen,
            preservation_turns=replay_turns, preservation_weight=4.)
        legacy = ordering.qat_gradients(current, a, target, turns=turns, pairs=pairs)
        replay_gradient = stm.qat_gradients(current, replay, frozen, turns=replay_turns)
        for key in stm.PARAMETER_KEYS:
            np.testing.assert_allclose(combined[key], legacy[key] + 4 * replay_gradient[key],
                                       atol=1e-15, rtol=1e-15)
        predicted, cache = stm.qat_forward(current, replay, turns=replay_turns)
        hidden = cache['differences'][:, 0] / 32
        # The local smooth surrogate fixes quantized hidden activations and
        # integer score offsets. It validates STE, not integer-rounding calculus.
        def smooth_output_mse(delta):
            return 4 * np.mean((predicted + delta * hidden - frozen) ** 2) / 10000
        eps = 1e-5
        finite = (smooth_output_mse(eps) - smooth_output_mse(-eps)) / (2 * eps)
        added = combined['output_weights'][0] - legacy['output_weights'][0]
        self.assertAlmostEqual(added, finite, places=11)
        self.assertGreater(added, 0)

    def test_zero_weight_retains_legacy_parameters_history_and_selection_exactly(self):
        initial, a, turns, config, args, (actual, history, selection) = train_fixture(weight=0.)
        expected, old_history, old_selection = ordering.train_candidate(
            initial, a, np.array([100.]), config, **args)
        self.assertEqual(history, old_history)
        self.assertEqual(selection, old_selection)
        for key in stm.PARAMETER_KEYS:
            np.testing.assert_array_equal(actual[key], expected[key])
        with_family = dict(args, pair_family_ids=['train-family'])
        candidate, new_history, new_selection = preservation.train_candidate(
            initial, a, np.array([100.]), config, preservation_indices=a,
            preservation_turns=turns, preservation_weight=0., **with_family)
        legacy, old_history, old_selection = ordering.train_candidate(
            initial, a, np.array([100.]), config, **with_family)
        self.assertEqual(new_history, old_history)
        self.assertEqual(new_selection, old_selection)
        for key in stm.PARAMETER_KEYS:
            np.testing.assert_array_equal(candidate[key], legacy[key])

    def test_epoch_zero_preservation_has_zero_gradient_and_targets_use_integer_scores(self):
        initial, a, _, turns, pairs, _ = fixture()
        initial['output_weights'][0] += .001
        integer = stm.qat_forward(initial, a, turns=turns)[0]
        self.assertNotEqual(float(integer[0]), stm._float_predictions(initial, a, turns, True)[0])
        combined = preservation.qat_gradients(
            initial, a, np.array([101.]), turns=turns, pairs=pairs,
            preservation_indices=a, preservation_targets=integer,
            preservation_turns=turns)
        expected = ordering.qat_gradients(initial, a, np.array([101.]), turns=turns, pairs=pairs)
        for key in stm.PARAMETER_KEYS:
            np.testing.assert_array_equal(combined[key], expected[key])
        targets_seen = []
        original = preservation.qat_gradients
        def record(*args, **kwargs):
            targets_seen.append(kwargs['preservation_targets'].copy())
            return original(*args, **kwargs)
        with patch.object(preservation, 'qat_gradients', side_effect=record):
            *_, (_, _, selection) = train_fixture(epochs=5, patience=100)
        self.assertEqual(len(targets_seen), 5)
        for targets in targets_seen:
            np.testing.assert_array_equal(targets, [14.])
        self.assertEqual(selection['preservation_targets']['positions'], 1)
        self.assertEqual(selection['initial_validation']['preservation_train']['mse_cp2'], 0)

    def test_old_train_subset_and_turn_are_required_and_invalid_weights_rejected(self):
        initial, a, b, turns, pairs, config = fixture()
        base_args = dict(turns=turns, pairs=pairs, validation_indices=b,
                         validation_targets=np.array([100.]), validation_turns=turns,
                         validation_pairs=pairs, anchor_indices=b,
                         anchor_targets=np.array([100.]), anchor_turns=turns,
                         eval_interval=1, preservation_indices=b, preservation_turns=turns)
        with self.assertRaisesRegex(ValueError, 'TRAIN subset'):
            preservation.train_candidate(initial, a, np.array([100.]), config, **base_args)
        base_args['preservation_indices'] = a
        base_args['preservation_turns'] = 1 - turns
        with self.assertRaisesRegex(ValueError, 'TRAIN subset'):
            preservation.train_candidate(initial, a, np.array([100.]), config, **base_args)
        base_args['preservation_indices'] = np.concatenate([a, a])
        base_args['preservation_turns'] = np.concatenate([turns, turns])
        with self.assertRaisesRegex(ValueError, 'unique'):
            preservation.train_candidate(initial, a, np.array([100.]), config, **base_args)
        for weight in (-1., float('nan'), float('inf'), True, '4'):
            with self.subTest(weight=weight), self.assertRaises(ValueError):
                preservation.qat_gradients(
                    initial, a, np.array([100.]), turns=turns, pairs=pairs,
                    preservation_indices=a, preservation_targets=np.array([14.]),
                    preservation_turns=turns, preservation_weight=weight)

    def test_validation_anchor_never_enter_gradients_or_change_training_trajectory(self):
        seen = []
        original = preservation.qat_gradients
        def record(parameters, indices, targets, *args, **kwargs):
            seen.append((targets.tolist(), kwargs['preservation_targets'].tolist()))
            return original(parameters, indices, targets, *args, **kwargs)
        with patch.object(preservation, 'qat_gradients', side_effect=record):
            *_, (_, history, selection) = train_fixture(
                validation_target=-444., anchor_target=-555., patience=100, epochs=5)
        *_, (_, other_history, _) = train_fixture(
            validation_target=1000., anchor_target=1200., patience=100, epochs=5)
        self.assertEqual(seen, [([100.], [14.])] * 5)
        self.assertEqual(history, other_history)
        self.assertFalse(selection['validation_used_in_gradients'])
        self.assertFalse(selection['validation_pairs_used_in_gradients'])
        self.assertFalse(selection['anchor_used_in_gradients'])
        self.assertFalse(selection['preservation_targets_used_in_selection'])

    def test_anchor_rejects_changed_integer_predictions_and_restores_epoch_zero(self):
        initial, _, _, _, _, (actual, history, selection) = train_fixture(
            anchor_target=14., patience=2)
        self.assertEqual(selection['selected_epoch'], 0)
        self.assertTrue(all(not e['eligible'] for e in selection['validation_evaluations'][1:]))
        self.assertGreater(history[-1]['preservation_train']['mse_cp2'], 0)
        self.assertEqual(selection['preservation_targets']['selected_drift']['mse_cp2'], 0)
        for key in stm.PARAMETER_KEYS:
            np.testing.assert_array_equal(actual[key], initial[key])

    def test_best_eligible_checkpoint_is_copied_and_callback_cannot_mutate_it(self):
        def observer(epoch, parameters, evaluation):
            for value in parameters.values():
                value[:] = 0
            evaluation['preservation_train']['mse_cp2'] = -1
        *_, (actual, history, selection) = train_fixture(callback=observer)
        *_, (reference, other_history, other_selection) = train_fixture()
        self.assertEqual(history, other_history)
        self.assertEqual(selection, other_selection)
        eligible = [e for e in selection['validation_evaluations'] if e['eligible']]
        self.assertEqual(selection['selected_validation']['mse_cp2'],
                         min(e['mse_cp2'] for e in eligible))
        for key in stm.PARAMETER_KEYS:
            np.testing.assert_array_equal(actual[key], reference[key])
            self.assertFalse(np.shares_memory(actual[key], reference[key]))


if __name__ == '__main__':
    unittest.main(verbosity=2)
