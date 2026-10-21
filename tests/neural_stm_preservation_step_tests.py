"""Actual-integer training-step acceptance, rounding, and held-out isolation."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import neural_stm as stm
import neural_stm_preservation_step as step
from neural_stm_preservation_tests import fixture


def train_fixture(*, validation_target=18., anchor_target=100., patience=3,
                  epochs=20, progress=None):
    initial, a, _, turns, pairs, config = fixture()
    config['epochs'] = epochs
    result = step.train_candidate(
        initial, a, np.array([100.]), config, turns=turns, pairs=pairs,
        validation_indices=a.copy(), validation_targets=np.array([validation_target]),
        validation_turns=turns.copy(), validation_pairs=pairs,
        anchor_indices=a.copy(), anchor_targets=np.array([anchor_target]),
        anchor_turns=turns.copy(), patience=patience, eval_interval=1,
        min_delta_cp2=0., progress=progress, preservation_indices=a.copy(),
        preservation_turns=turns.copy(), preservation_weight=4.)
    return initial, a, turns, result


class PreservationStepTests(unittest.TestCase):
    def test_first_nonincreasing_integer_step_is_selected_without_mutation(self):
        initial, a, _, turns, _, _ = fixture()
        proposal = {key: value.copy() for key, value in initial.items()}
        proposal['output_weights'][0] = 4
        def objective(values):
            return float((stm.qat_forward(values, a, turns=turns)[0][0] - 15) ** 2)
        selected, loss, scale, trials = step.backtracked_step(
            initial, proposal, objective, objective(initial))
        self.assertEqual(scale, .125)
        self.assertEqual(loss, 0)
        self.assertEqual([r['scale'] for r in trials], [1., .5, .25, .125])
        self.assertTrue(all(r['objective_cp2'] > 1 for r in trials[:-1]))
        self.assertEqual(initial['output_weights'][0], 2)
        self.assertEqual(proposal['output_weights'][0], 4)
        self.assertEqual(selected['output_weights'][0], 2.25)

    def test_equal_integer_plateau_allows_shadow_progress_and_all_bad_steps_skip(self):
        p = {'x': np.array([0.])}
        selected, loss, scale, trials = step.backtracked_step(
            p, {'x': np.array([1.])}, lambda v: float(np.rint(v['x'][0]) ** 2), 0.)
        self.assertEqual(scale, .5)
        self.assertEqual(loss, 0)
        self.assertEqual(selected['x'][0], .5)
        self.assertEqual(len(trials), 2)
        initial, a, _, turns, _, _ = fixture()
        proposal = {key: value.copy() for key, value in initial.items()}
        proposal['output_weights'][0] = -2
        def objective(values):
            return float((stm.qat_forward(values, a, turns=turns)[0][0] - 14) ** 2)
        selected, loss, scale, trials = step.backtracked_step(initial, proposal, objective, 0.)
        self.assertEqual(scale, 0)
        self.assertEqual(loss, 0)
        self.assertEqual(len(trials), 6)
        for key in stm.PARAMETER_KEYS:
            np.testing.assert_array_equal(selected[key], initial[key])
            self.assertFalse(np.shares_memory(selected[key], initial[key]))

    def test_nonfinite_objectives_and_proposals_are_errors_not_silent_acceptance(self):
        p, proposed = {'x': np.array([0.])}, {'x': np.array([1.])}
        for bad in (float('nan'), float('inf'), True, '0'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                step.backtracked_step(p, proposed, lambda _: bad, 0.)
            with self.subTest(current=bad), self.assertRaises(ValueError):
                step.backtracked_step(p, proposed, lambda _: 0., bad)
        with self.assertRaises(ValueError):
            step.backtracked_step(p, {'x': np.array([np.nan])}, lambda _: 0., 0.)

    def test_full_objective_has_independent_means_and_fixed_pair_and_preservation_weights(self):
        initial, a, b, turns, pairs, _ = fixture()
        indices = np.concatenate([a, b])
        side = np.concatenate([turns, turns])
        frozen = stm.qat_forward(initial, indices, turns=side)[0]
        changed = {key: value.copy() for key, value in initial.items()}
        changed['output_weights'][0] += 1
        target = np.array([100., 20.])
        pairs = (a, np.array([80.]), turns, b, np.array([20.]), turns)
        predicted = stm.qat_forward(changed, indices, turns=side)[0]
        expected = (float(np.mean((predicted - target) ** 2))
                    + float((predicted[0] - predicted[1] - 60.) ** 2)
                    + 4 * float(np.mean((predicted - frozen) ** 2)))
        actual = step.training_objective(
            changed, indices, target, turns=side, pairs=pairs,
            preservation_indices=indices, preservation_targets=frozen,
            preservation_turns=side, preservation_weight=4.)
        self.assertEqual(actual, expected)

    def test_only_train_arrays_choose_scales_and_gradients_and_trajectory_is_reproducible(self):
        observed = []
        objective = step.training_objective
        def record(values, indices, targets, **kwargs):
            observed.append((targets.tolist(), kwargs['preservation_targets'].tolist(),
                             kwargs['pairs'][1].tolist(), kwargs['pairs'][4].tolist()))
            return objective(values, indices, targets, **kwargs)
        with patch.object(step, 'training_objective', side_effect=record):
            *_, (_, history, selection) = train_fixture(
                validation_target=-444., anchor_target=-555., patience=100, epochs=5)
        *_, (_, different_history, _) = train_fixture(
            validation_target=1000., anchor_target=1200., patience=100, epochs=5)
        self.assertTrue(observed)
        self.assertTrue(all(row == ([100.], [14.], [0.], [0.]) for row in observed))
        self.assertEqual(history, different_history)
        for row in history:
            control = row['integer_step_control']
            self.assertLessEqual(control['objective_after_cp2'], control['objective_before_cp2'])
            self.assertEqual(sum(control['accepted_scale_histogram'].values())
                             + control['skipped_updates'], control['adam_proposals'])
        self.assertFalse(selection['integer_step_control']['validation_used_for_step_scale'])
        self.assertFalse(selection['integer_step_control']['anchor_used_for_step_scale'])
        self.assertEqual(selection['optimizer_steps'], 5)

    def test_checkpoint_anchor_and_best_restore_still_hold_and_callback_cannot_mutate(self):
        initial, _, _, (actual, _, selection) = train_fixture(anchor_target=14., patience=2)
        self.assertEqual(selection['selected_epoch'], 0)
        for key in stm.PARAMETER_KEYS:
            np.testing.assert_array_equal(actual[key], initial[key])
        def observer(epoch, parameters, evaluation):
            for value in parameters.values():
                value[:] = 0
            evaluation['mse_cp2'] = -1
        *_, (actual, history, selection) = train_fixture(progress=observer)
        *_, (reference, other_history, other_selection) = train_fixture()
        self.assertEqual(history, other_history)
        self.assertEqual(selection, other_selection)
        eligible = [e for e in selection['validation_evaluations'] if e['eligible']]
        self.assertEqual(selection['selected_validation']['mse_cp2'],
                         min(e['mse_cp2'] for e in eligible))
        for key in stm.PARAMETER_KEYS:
            np.testing.assert_array_equal(actual[key], reference[key])


if __name__ == '__main__':
    unittest.main(verbosity=2)
