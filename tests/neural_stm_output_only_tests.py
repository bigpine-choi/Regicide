"""Output-only derivatives, immutable hidden tensors, and guarded restoration."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import neural_stm as stm
import neural_stm_preservation as preservation
import neural_stm_output_only as output
from neural_stm_preservation_tests import fixture


def train_fixture(*, validation_target=18., anchor_target=100., patience=3,
                  epochs=20, progress=None, pair_family_ids=None):
    initial, a, _, turns, pairs, config = fixture()
    # Byte-level checks should also preserve signed zero in frozen shadows.
    initial['turn_weights'][1] = -0.0
    config['epochs'] = epochs
    result = output.train_candidate(
        initial, a, np.array([100.]), config, turns=turns, pairs=pairs,
        validation_indices=a.copy(), validation_targets=np.array([validation_target]),
        validation_turns=turns.copy(), validation_pairs=pairs,
        anchor_indices=a.copy(), anchor_targets=np.array([anchor_target]),
        anchor_turns=turns.copy(), patience=patience, eval_interval=1,
        min_delta_cp2=0., progress=progress, pair_family_ids=pair_family_ids,
        preservation_indices=a.copy(), preservation_turns=turns.copy(),
        preservation_weight=4.)
    return initial, a, turns, result


class OutputOnlyTests(unittest.TestCase):
    def assertFrozen(self, initial, actual, scale=32):
        original_model = stm.quantize(initial, scale)
        actual_model = stm.quantize(actual, scale)
        for key in output.FROZEN_KEYS:
            self.assertEqual(actual[key].dtype, initial[key].dtype)
            self.assertEqual(actual[key].tobytes(), initial[key].tobytes())
            np.testing.assert_array_equal(actual_model[key], original_model[key])

    def test_only_output_derivative_equals_existing_three_mse_gradient(self):
        initial, a, b, turns, _, _ = fixture()
        old = np.concatenate([a, b])
        sides = np.concatenate([turns, turns])
        frozen = stm.qat_forward(initial, old, turns=sides)[0]
        current = {key: value.copy() for key, value in initial.items()}
        current['output_weights'][0] += 1
        pairs = (a, np.array([80.]), turns, b, np.array([20.]), turns)
        arguments = dict(turns=turns, pairs=pairs, preservation_indices=old,
                         preservation_targets=frozen, preservation_turns=sides,
                         preservation_weight=4.)
        expected = preservation.qat_gradients(current, a, np.array([100.]), **arguments)
        with patch.object(stm, 'qat_gradients', side_effect=AssertionError('Frozen derivative path')):
            actual = output.qat_gradients(current, a, np.array([100.]), **arguments)
        self.assertEqual(set(actual), {'output_weights'})
        self.assertEqual(actual['output_weights'].shape, (16,))
        np.testing.assert_array_equal(actual['output_weights'], expected['output_weights'])
        self.assertGreater(sum(np.sum(expected[key] ** 2) for key in output.FROZEN_KEYS), 0)

    def test_exact_three_independent_mse_means_remain_unchanged(self):
        initial, a, b, turns, _, _ = fixture()
        indices, sides = np.concatenate([a, b]), np.concatenate([turns, turns])
        frozen = stm.qat_forward(initial, indices, turns=sides)[0]
        current = {key: value.copy() for key, value in initial.items()}
        current['output_weights'][0] += 1
        pairs = (a, np.array([80.]), turns, b, np.array([20.]), turns)
        predictions = stm.qat_forward(current, indices, turns=sides)[0]
        target = np.array([100., 20.])
        expected = (np.mean((predictions - target) ** 2)
                    + (predictions[0] - predictions[1] - 60.) ** 2
                    + 4 * np.mean((predictions - frozen) ** 2))
        actual = output.training_objective(
            current, indices, target, turns=sides, pairs=pairs,
            preservation_indices=indices, preservation_targets=frozen,
            preservation_turns=sides, preservation_weight=4.)
        self.assertEqual(actual, expected)

    def test_every_backtracking_trial_changes_only_output_and_uses_fixed_scales(self):
        initial, a, _, turns, _, _ = fixture()
        initial['turn_weights'][1] = -0.0
        proposal = initial['output_weights'].copy()
        proposal[0] = 4.
        seen = []
        def objective(values):
            self.assertFrozen(initial, values)
            seen.append(values['output_weights'][0])
            return float((stm.qat_forward(values, a, turns=turns)[0][0] - 15) ** 2)
        selected, loss, scale, trials = output.backtracked_step(initial, proposal, objective, 1.)
        self.assertEqual(scale, .125)
        self.assertEqual(loss, 0)
        self.assertEqual(seen, [4., 3., 2.5, 2.25])
        self.assertEqual([row['scale'] for row in trials], [1., .5, .25, .125])
        self.assertFrozen(initial, selected)
        self.assertEqual(initial['output_weights'][0], 2.)
        self.assertEqual(proposal[0], 4.)
        rejected, loss, scale, trials = output.backtracked_step(
            initial, proposal, lambda _: 1., 0.)
        self.assertEqual(scale, 0.)
        self.assertEqual(len(trials), 6)
        for key in stm.PARAMETER_KEYS:
            np.testing.assert_array_equal(rejected[key], initial[key])
            self.assertFalse(np.shares_memory(rejected[key], initial[key]))

    def test_solvable_problem_changes_output_and_best_restore_keeps_frozen_arrays(self):
        observed = []
        initial_ref = fixture()[0]
        initial_ref['turn_weights'][1] = -0.0
        def observer(epoch, parameters, evaluation):
            self.assertFrozen(initial_ref, parameters)
            observed.append((epoch, parameters['output_weights'].copy()))
            # Observers receive copies and cannot overwrite state/checkpoints.
            for value in parameters.values():
                value[:] = 0
            evaluation['mse_cp2'] = -1
        initial, a, turns, (actual, history, selection) = train_fixture(progress=observer)
        self.assertTrue(observed)
        self.assertGreater(selection['selected_epoch'], 0)
        self.assertLess(selection['selected_epoch'], selection['stopped_epoch'])
        self.assertFalse(np.array_equal(initial['output_weights'], actual['output_weights']))
        before = stm.qat_forward(initial, a, turns=turns)[0][0]
        after = stm.qat_forward(actual, a, turns=turns)[0][0]
        self.assertLess(abs(after - 18), abs(before - 18))
        self.assertFrozen(initial, actual)
        checkpoint = next(values for epoch, values in observed if epoch == selection['selected_epoch'])
        np.testing.assert_array_equal(actual['output_weights'], checkpoint)
        self.assertEqual(selection['output_only']['trainable_parameters'], 16)
        self.assertEqual(selection['output_only']['adam_moment_keys'], ['output_weights'])
        self.assertEqual(selection['output_only']['proposal_delta_keys'], ['output_weights'])
        for entry in history + selection['validation_evaluations']:
            self.assertTrue(entry['frozen_parameters']['float_bytes_unchanged'])
            self.assertTrue(entry['frozen_parameters']['quantized_unchanged'])

    def test_anchor_rejects_changed_checkpoint_and_restores_epoch_zero(self):
        initial, _, _, (actual, history, selection) = train_fixture(anchor_target=14., patience=2)
        self.assertEqual(selection['selected_epoch'], 0)
        self.assertTrue(any(not value['eligible'] for value in selection['validation_evaluations'][1:]))
        self.assertTrue(all(not value['eligible'] for value in
                            selection['validation_evaluations'][1:]
                            if value['anchor']['mse_cp2'] > 0))
        self.assertGreater(history[-1]['preservation_train']['mse_cp2'], 0)
        for key in stm.PARAMETER_KEYS:
            self.assertEqual(actual[key].tobytes(), initial[key].tobytes())
        self.assertEqual(selection['preservation_targets']['selected_drift']['mse_cp2'], 0)

    def test_active_gradient_norm_and_all_actual_proposals_only_use_output(self):
        captured = []
        original = output.backtracked_step
        def record(parameters, proposal, objective, current):
            self.assertIsInstance(proposal, np.ndarray)
            self.assertEqual(proposal.shape, (16,))
            self.assertEqual(parameters['input_weights'].shape, (768, 16))
            captured.append(proposal.copy())
            return original(parameters, proposal, objective, current)
        with patch.object(output, 'backtracked_step', side_effect=record):
            initial, a, turns, (_, history, selection) = train_fixture(epochs=1)
        _, _, _, _, pairs, _ = fixture()
        gradient = output.qat_gradients(
            initial, a, np.array([100.]), turns=turns, pairs=pairs,
            preservation_indices=a, preservation_targets=np.array([14.]),
            preservation_turns=turns, preservation_weight=4.)['output_weights']
        self.assertAlmostEqual(history[0]['max_batch_gradient_global_norm'],
                               float(np.linalg.norm(gradient)), places=15)
        self.assertEqual(history[0]['gradient_norm_parameter_keys'], ['output_weights'])
        self.assertEqual(len(captured), selection['optimizer_steps'])
        self.assertEqual(selection['output_only']['frozen_checks_after_proposals'], len(captured))

    def test_held_out_labels_never_change_gradient_trajectory_or_step_scales(self):
        observed = []
        original = output.training_objective
        def record(parameters, indices, targets, **kwargs):
            observed.append((targets.tolist(), kwargs['preservation_targets'].tolist()))
            return original(parameters, indices, targets, **kwargs)
        with patch.object(output, 'training_objective', side_effect=record):
            *_, (_, history, selection) = train_fixture(
                validation_target=-444., anchor_target=-555., patience=100, epochs=5,
                pair_family_ids=['family-A'])
        *_, (_, other_history, _) = train_fixture(
            validation_target=1000., anchor_target=1200., patience=100, epochs=5,
            pair_family_ids=['family-A'])
        self.assertTrue(observed)
        self.assertTrue(all(value == ([100.], [14.]) for value in observed))
        self.assertEqual(history, other_history)
        self.assertEqual(selection['observed_pairs_per_epoch'], [1] * 5)
        for row in history:
            control = row['integer_step_control']
            self.assertLessEqual(control['objective_after_cp2'], control['objective_before_cp2'])
        self.assertFalse(selection['validation_used_in_gradients'])
        self.assertFalse(selection['anchor_used_in_gradients'])
        self.assertFalse(selection['integer_step_control']['validation_used_for_step_scale'])

    def test_frozen_audit_detects_bit_changes_and_invalid_proposals_fail(self):
        initial, _, _, _, _, _ = fixture()
        snapshot = output._frozen_snapshot(initial)
        changed = {key: value.copy() for key, value in initial.items()}
        changed['turn_weights'][1] = -0.0
        with self.assertRaisesRegex(ValueError, 'Frozen NNUE'):
            output._check_frozen(changed, snapshot)
        for proposal in (np.zeros(15), np.full(16, np.nan), np.zeros(16, dtype=np.int64)):
            with self.subTest(proposal=proposal), self.assertRaises(ValueError):
                output.backtracked_step(initial, proposal, lambda _: 0., 0.)
        with self.assertRaises(ValueError):
            output.backtracked_step(initial, np.zeros(16), lambda _: float('nan'), 0.)


if __name__ == '__main__':
    unittest.main(verbosity=2)
