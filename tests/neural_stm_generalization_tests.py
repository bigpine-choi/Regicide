"""Family-balanced pair visits without weakening MSE or checkpoint guards."""
from pathlib import Path
import importlib.util
import sys
import unittest
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'tests'))
import neural_stm as stm
import neural_stm_ordering as ordering
from neural_stm_ordering_tests import fixture


def fit(module=ordering, *, point_count=3, pair_count=6, family_ids=None,
        epochs=3, validation_target=18., anchor_target=100., patience=100):
    initial, a, turns, original_pairs, config = fixture()
    indices = np.repeat(a, point_count, axis=0)
    sides = np.repeat(turns, point_count)
    pairs = tuple(np.repeat(value, pair_count, axis=0) for value in original_pairs)
    # Distinct target values make visits observable without invoking any engine.
    pairs = (pairs[0], np.arange(pair_count, dtype=float) + 80, pairs[2], *pairs[3:])
    config.update(epochs=epochs, batch_size=1)
    kwargs = {} if family_ids is None else {'pair_family_ids': family_ids}
    return module.train_candidate(
        initial, indices, np.full(point_count, 100.), config, turns=sides, pairs=pairs,
        validation_indices=a.copy(), validation_targets=np.array([validation_target]),
        validation_turns=turns.copy(), validation_pairs=original_pairs,
        anchor_indices=a.copy(), anchor_targets=np.array([anchor_target]),
        anchor_turns=turns.copy(), patience=patience, min_delta_cp2=0., eval_interval=1,
        **kwargs)


class GeneralizationTests(unittest.TestCase):
    def test_legacy_none_matches_frozen_original_parameters_history_and_selection(self):
        source = ROOT / 'docs/checks/2026-10-22-nnue-ordering/attempt-001/source-snapshot/scripts/neural_stm_ordering.py'
        spec = importlib.util.spec_from_file_location('frozen_ordering_control', source)
        old = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(old)
        expected, history, selection = fit(old)
        actual, actual_history, actual_selection = fit()
        self.assertEqual(actual_history, history)
        self.assertEqual(actual_selection, selection)
        for key in stm.PARAMETER_KEYS:
            np.testing.assert_array_equal(actual[key], expected[key])

    def test_each_pair_visits_exactly_once_each_epoch_and_preserves_mean_pair_gradient(self):
        observed = []
        original = ordering.qat_gradients
        def recording(parameters, indices, targets, *args, **kwargs):
            observed.append(kwargs['pairs'][1].tolist())
            return original(parameters, indices, targets, *args, **kwargs)
        with patch.object(ordering, 'qat_gradients', side_effect=recording):
            _, history, selection = fit(family_ids=['a', 'a', 'b', 'b', 'c', 'c'])
        self.assertEqual(len(observed), 9)
        for epoch in range(3):
            visits = sum(observed[3 * epoch:3 * epoch + 3], [])
            self.assertEqual(sorted(visits), list(range(80, 86)))
            self.assertEqual(len(visits), len(set(visits)))
        self.assertEqual(selection['observed_pairs_per_epoch'], [6, 6, 6])
        self.assertEqual(selection['pair_weight'], 1.)
        self.assertEqual(len(history), 3)

    def test_family_round_robin_is_balanced_and_seeded_without_duplicate_visits(self):
        families = ['a'] * 4 + ['b'] * 4 + ['c'] * 4
        def schedule(seed):
            return np.concatenate(ordering._family_pair_batches(families, 4, np.random.default_rng(seed)))
        first = schedule(20261020)
        np.testing.assert_array_equal(first, schedule(20261020))
        self.assertFalse(np.array_equal(first, schedule(123456)))
        self.assertEqual(sorted(first.tolist()), list(range(12)))
        for start in range(0, 12, 3):
            self.assertEqual({families[index] for index in first[start:start + 3]}, {'a', 'b', 'c'})

    def test_invalid_ids_and_too_few_pairs_rejected_before_optimization(self):
        for ids in ([], ['a'], [1] * 6, [''] * 6, [' '] * 6, 'abcdef', np.array(['a'] * 6)):
            with self.subTest(ids=repr(ids)), self.assertRaises(ValueError):
                fit(family_ids=ids)
        with self.assertRaisesRegex(ValueError, 'at least one pair'):
            fit(point_count=4, pair_count=3, family_ids=['a', 'b', 'c'])

    def test_validation_and_anchor_targets_never_change_gradients_or_training_history(self):
        observed_points, observed_pairs = [], []
        original = ordering.qat_gradients
        def recording(parameters, indices, targets, *args, **kwargs):
            observed_points.extend(targets.tolist())
            observed_pairs.extend(kwargs['pairs'][1].tolist())
            return original(parameters, indices, targets, *args, **kwargs)
        ids = ['a', 'a', 'b', 'b', 'c', 'c']
        with patch.object(ordering, 'qat_gradients', side_effect=recording):
            _, history, selection = fit(family_ids=ids, validation_target=-444., anchor_target=-555.)
        _, other_history, _ = fit(family_ids=ids, validation_target=1000., anchor_target=1200.)
        self.assertEqual(history, other_history)
        self.assertEqual(observed_points, [100.] * 9)
        self.assertEqual(sorted(observed_pairs), sorted(list(range(80, 86)) * 3))
        self.assertFalse(selection['validation_used_in_gradients'])
        self.assertFalse(selection['validation_pairs_used_in_gradients'])
        self.assertFalse(selection['anchor_used_in_gradients'])

    def test_family_schedule_still_restores_best_and_preserves_epoch_zero_anchor(self):
        initial, indices, turns, _, config = fixture()
        tie = (indices, np.array([0.]), turns, indices.copy(), np.array([0.]), turns.copy())
        def run(anchor_target):
            return ordering.train_candidate(
                initial, indices, np.array([100.]), config, turns=turns, pairs=tie,
                validation_indices=indices.copy(), validation_targets=np.array([18.]),
                validation_turns=turns.copy(), validation_pairs=tie,
                anchor_indices=indices.copy(), anchor_targets=np.array([anchor_target]),
                anchor_turns=turns.copy(), patience=3, min_delta_cp2=0., eval_interval=1,
                pair_family_ids=['one'])
        best, _, selection = run(100.)
        self.assertEqual(selection['selected_epoch'], 4)
        self.assertEqual(selection['stopped_epoch'], 7)
        np.testing.assert_array_equal(stm.predict_integer(best, indices, turns=turns), [18])
        protected, _, guard = run(14.)
        self.assertEqual(guard['selected_epoch'], 0)
        for key in stm.PARAMETER_KEYS:
            np.testing.assert_array_equal(protected[key], initial[key])


if __name__ == '__main__':
    unittest.main(verbosity=2)
