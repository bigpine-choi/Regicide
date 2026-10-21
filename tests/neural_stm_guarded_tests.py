"""Guarded MSE training: unchanged Adam, integer selection, and exact restoration."""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import neural_stm as stm
import neural_stm_guarded as guarded


def fixture():
    initial = stm.with_turn_parameters({
        'input_weights': np.zeros((768, 16), dtype=np.float64),
        'hidden_bias': np.zeros(16, dtype=np.float64),
        'output_weights': np.zeros(16, dtype=np.float64),
    })
    initial['hidden_bias'][0] = 10
    initial['input_weights'][0, 0] = 3
    initial['input_weights'][1, 0] = -1
    initial['output_weights'][0] = 2
    indices = np.full((1, 2, 32), 768, dtype=np.int64)
    indices[:, 0, :2] = 0
    indices[:, 1, 0] = 1
    turns = np.array([0], dtype=np.int64)
    config = {'candidate_id': 'tiny-guard-control', 'input_scale': 32, 'epochs': 20,
              'input_bias_lr': .05, 'output_lr': .1, 'seed': 20261020,
              'batch_size': 1, 'loss': 'mse'}
    return initial, indices, turns, config


class GuardedTrainingTests(unittest.TestCase):
    def run_training(self, validation_target=18., **kwargs):
        initial, indices, turns, config = fixture()
        config.update(kwargs.pop('config', {}))
        result = guarded.train_candidate(
            initial, indices, np.array([100.]), config, turns=turns,
            validation_indices=indices.copy(), validation_targets=np.array([validation_target]),
            validation_turns=turns.copy(), **kwargs)
        return initial, indices, turns, config, result

    def test_disabled_guard_is_exact_historical_adam_independent_of_validation(self):
        initial, indices, turns, config, (result, history, selection) = self.run_training(
            guard_enabled=False, config={'epochs': 5})
        old, old_history = stm.train_candidate(initial, indices, np.array([100.]), config, turns=turns)
        self.assertEqual(history, old_history)
        for key in stm.PARAMETER_KEYS:
            np.testing.assert_array_equal(result[key], old[key])
        different = self.run_training(validation_target=-500., guard_enabled=False, config={'epochs': 5})[-1]
        self.assertEqual(history, different[1])
        for key in stm.PARAMETER_KEYS:
            np.testing.assert_array_equal(result[key], different[0][key])
        self.assertNotEqual(selection['selected_validation'], different[2]['selected_validation'])
        self.assertEqual(selection['selected_epoch'], 5)
        self.assertEqual(selection['stop_reason'], 'guard_disabled')

    def test_best_intermediate_checkpoint_restores_exact_copy_and_unchanged_adam(self):
        initial, indices, turns, config, (result, history, selection) = self.run_training(patience=3)
        self.assertEqual(selection['selected_epoch'], 4)
        self.assertEqual(selection['stopped_epoch'], 7)
        self.assertEqual(selection['stop_reason'], 'patience_exhausted')
        self.assertEqual(selection['selected_validation']['mse_cp2'], 0)
        self.assertEqual(selection['last_validation']['mse_cp2'], 25)
        # Continue the original trainer to epoch seven: validation never changes its updates.
        _, reference_history = stm.train_candidate(
            initial, indices, np.array([100.]), dict(config, epochs=7), turns=turns)
        self.assertEqual(history, reference_history)
        reference, _ = stm.train_candidate(
            initial, indices, np.array([100.]), dict(config, epochs=4), turns=turns)
        for key in stm.PARAMETER_KEYS:
            np.testing.assert_array_equal(result[key], reference[key])
            self.assertFalse(np.shares_memory(result[key], initial[key]))
        scores = stm.predict_integer(result, indices, 32, turns=turns)
        np.testing.assert_array_equal(scores, [18])

    def test_epoch_zero_can_win_and_initial_parameters_are_not_mutated(self):
        before = fixture()[0]
        initial, _, _, _, (result, history, selection) = self.run_training(validation_target=14., patience=2)
        self.assertEqual(len(history), 2)
        self.assertEqual(selection['selected_epoch'], 0)
        self.assertEqual(selection['initial_validation']['mse_cp2'], 0)
        self.assertGreater(selection['last_validation']['mse_cp2'], 0)
        for key in stm.PARAMETER_KEYS:
            np.testing.assert_array_equal(initial[key], before[key])
            np.testing.assert_array_equal(result[key], before[key])
            self.assertFalse(np.shares_memory(result[key], initial[key]))

    def test_minimum_delta_affects_patience_but_preserves_every_better_checkpoint(self):
        *_, (_, _, selection) = self.run_training(patience=2, min_delta_cp2=100.)
        self.assertEqual(selection['stopped_epoch'], 2)
        self.assertEqual(selection['selected_epoch'], 2)
        self.assertEqual(selection['selected_validation']['mse_cp2'], 4)
        self.assertEqual(selection['initial_validation']['mse_cp2'], 16)

    def test_patience_counts_evaluations_not_unevaluated_epochs(self):
        *_, (_, history, selection) = self.run_training(validation_target=14., patience=2, eval_interval=3)
        self.assertEqual(len(history), 6)
        self.assertEqual(selection['stopped_epoch'], 6)
        self.assertEqual([entry['epoch'] for entry in selection['validation_evaluations']], [0, 3, 6])

    def test_last_budget_epoch_is_evaluated_and_ties_keep_earlier_copy(self):
        *_, (_, history, selection) = self.run_training(
            validation_target=14., patience=10, eval_interval=3, config={'epochs': 5})
        self.assertEqual(len(history), 5)
        self.assertEqual([entry['epoch'] for entry in selection['validation_evaluations']], [0, 3, 5])
        self.assertEqual(selection['stop_reason'], 'max_epochs')
        self.assertFalse(selection['stopped_early'])
        # A tiny rate leaves every actual integer prediction at the same score.
        *_, (_, _, ties) = self.run_training(config={
            'epochs': 3, 'input_bias_lr': 1e-10, 'output_lr': 1e-10}, patience=10)
        self.assertEqual(ties['selected_epoch'], 0)
        self.assertTrue(all(row['mse_cp2'] == 16 for row in ties['validation_evaluations']))

    def test_callback_cannot_change_optimizer_or_saved_checkpoint(self):
        observed = []

        def observer(epoch, parameters, evaluation):
            observed.append(epoch)
            for values in parameters.values():
                values[:] = 0
            evaluation['mse_cp2'] = -123

        *_, (result, history, selection) = self.run_training(patience=3, progress=observer)
        *_, (reference, reference_history, reference_selection) = self.run_training(patience=3)
        self.assertEqual(observed, list(range(1, 8)))
        self.assertEqual(history, reference_history)
        self.assertEqual(selection, reference_selection)
        for key in stm.PARAMETER_KEYS:
            np.testing.assert_array_equal(result[key], reference[key])

    def test_disabled_turn_and_mse_constraints_preserved(self):
        initial, indices, turns, config, (result, history, selection) = self.run_training(
            enable_turn=False, patience=3)
        _, old_history = stm.train_candidate(initial, indices, np.array([100.]),
                                              dict(config, epochs=len(history)), turns=turns,
                                              enable_turn=False)
        self.assertEqual(history, old_history)
        np.testing.assert_array_equal(result['turn_weights'], np.zeros(16))
        self.assertFalse(selection['validation_used_in_gradients'])
        with self.assertRaisesRegex(ValueError, 'MSE'):
            self.run_training(config={'loss': 'h100'})

    def test_bad_guard_configuration_and_validation_rejected(self):
        for invalid in ({'patience': 0}, {'patience': True}, {'min_delta_cp2': -1.},
                        {'min_delta_cp2': float('nan')}, {'min_delta_cp2': True},
                        {'eval_interval': 0}, {'eval_interval': 21},
                        {'guard_enabled': 1}, {'enable_turn': 1}, {'progress': 1}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.run_training(**invalid)
        initial, indices, turns, config = fixture()
        for bad_indices, bad_targets, bad_turns in (
                (indices[:0], np.array([]), turns[:0]),
                (indices, np.array([float('inf')]), turns),
                (indices, np.array([0.]), np.array([2]))):
            with self.assertRaises(ValueError):
                guarded.train_candidate(initial, indices, np.array([100.]), config, turns=turns,
                                        validation_indices=bad_indices, validation_targets=bad_targets,
                                        validation_turns=bad_turns)


if __name__ == '__main__':
    unittest.main(verbosity=2)
