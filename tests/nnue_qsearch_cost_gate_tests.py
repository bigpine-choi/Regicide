"""Position-only contracts for cache/priority costs with unchanged tactical moves."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import chess

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import neural_training as base
import nnue_qsearch_cost_gate as gate
import nnue_reservation_audit as historical


class QsearchCostGateTests(unittest.TestCase):
    def test_same_tactics_and_frozen_model_only_cache_and_priority_differ(self):
        options = gate.search_options()
        self.assertIs(options['warm']['UseQuiescenceThreats'], True)
        self.assertIs(options['candidate']['UseQuiescenceThreats'], True)
        self.assertNotIn(gate.CACHE_OPTION, options['warm'])
        self.assertNotIn(gate.ORDER_OPTION, options['warm'])
        candidate = dict(options['candidate'])
        self.assertIs(candidate.pop(gate.CACHE_OPTION), True)
        self.assertIs(candidate.pop(gate.ORDER_OPTION), True)
        self.assertEqual(candidate, options['warm'])
        self.assertEqual(options['warm']['NeuralModelFile'], str(gate.MODEL))
        self.assertEqual(gate.sha(gate.MODEL), gate.MODEL_SHA)

    def test_fixed_criteria_budgets_and_conditional_games(self):
        self.assertEqual(gate.CRITERIA, gate.legacy.previous.CRITERIA)
        self.assertEqual(gate.CRITERIA['minimum_bestmove_match_gain'], 2)
        self.assertEqual(gate.CRITERIA['minimum_mean_mover_quality_gain_cp'], 20)
        self.assertEqual(gate.CRITERIA['minimum_accepted_positions'], 12)
        self.assertEqual(gate.CRITERIA['games_if_pass'], 16)
        self.assertEqual(gate.CRITERIA['games_if_fail'], 0)
        self.assertFalse(gate.CRITERIA['retry_or_gate_checkpoint_selection'])

    def test_original_reservation_has_no_queries_and_exactly_594_features(self):
        source, reservation, roots, blocked = historical.audit_original()
        self.assertEqual(len(roots), 16)
        self.assertEqual(len(blocked), 594)
        self.assertEqual({path.name for path in source.iterdir()}, gate.SOURCE_FILES)
        self.assertEqual(reservation['exposure_files'], 256)

    def test_source_rejects_even_one_extra_query_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            for name in gate.SOURCE_FILES | {'label-started.json'}:
                gate.write(source / name, {})
            with self.assertRaisesRegex(ValueError, 'queried, sealed, consumed'):
                gate.authenticate_source(source)

    def test_incremental_inventory_replays_new_wrapped_pv_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            source, experiment, destination = parent / 'old/gate', parent / 'new', parent / 'new/gate'
            source.mkdir(parents=True); destination.mkdir(parents=True)
            receipt = experiment / 'diagnostic.json'
            gate.write(receipt, {'root': {'fen': chess.STARTING_FEN},
                                 'search': {'pv_uci': ['e2e4', 'e7e5']}})
            # A reservation file must never count as observation.
            gate.write(destination / 'frozen-roots.json', [{'fen': chess.STARTING_FEN}])
            with patch.object(gate, 'authenticate_source', return_value=(source, {'input_sha256': {}}, [], {'z' * 64})):
                features, paths = gate.delta_inventory(directory=destination, experiment=experiment)
            board = chess.Board(); board.push_uci('e2e4'); board.push_uci('e7e5')
            self.assertIn(base.feature_identity(board), features)
            self.assertEqual(paths, [receipt])

    def test_incremental_inventory_rejects_any_reserved_feature(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            source, experiment = parent / 'old/gate', parent / 'new'
            source.mkdir(parents=True); experiment.mkdir()
            gate.write(experiment / 'diagnostic.json', {'fen': chess.STARTING_FEN})
            blocked = {base.feature_identity(chess.Board())}
            with patch.object(gate, 'authenticate_source', return_value=(source, {'input_sha256': {}}, [], blocked)):
                with self.assertRaisesRegex(ValueError, 'entered later diagnostics'):
                    gate.delta_inventory(experiment=experiment)

    def test_reservation_copies_exact_bytes_without_teacher_or_search(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            source, destination = parent / 'old', parent / 'new'
            source.mkdir()
            for name in gate.SOURCE_FILES:
                (source / name).write_bytes(b'[  ]\n')
            original = {'input_sha256': {}, 'exposure_files': 256,
                        'exposure_unique_features': 121417, 'limitations': 'Small screen'}
            snapshot = parent / 'snapshot.json'; gate.write(snapshot, {})
            with patch.object(gate, 'authenticate_source', return_value=(source, original, [None] * 16, {'x'})), \
                 patch.object(gate, 'authenticate_superseded', return_value=(source, snapshot, {})), \
                 patch.object(gate, 'delta_inventory', return_value=(set(), [])), \
                 patch.object(gate.legacy, 'label_rows') as teacher:
                reservation = gate.reserve(destination)
            teacher.assert_not_called()
            self.assertEqual(reservation['reserved_roots'], 16)
            for name in ('frozen-roots.json', 'blocked-features.json'):
                self.assertEqual((source / name).read_bytes(), (destination / name).read_bytes())
            self.assertFalse((destination / 'plan.json').exists())

    def test_seal_rejects_failed_known_case_before_fresh_answers(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            old, new, correct, known, protocol = (directory / name for name in
                                                 ('old.exe', 'new.exe', 'correct.json', 'known.json', 'protocol.json'))
            old.write_bytes(b'old'); new.write_bytes(b'new')
            binding = {'model_sha256': gate.MODEL_SHA, 'old_binary_sha256': gate.sha(old),
                       'new_binary_sha256': gate.sha(new)}
            gate.write(correct, dict(binding, completed=True, passed=True, native_mismatches=0))
            gate.write(known, dict(binding, completed=True, known_position_passed=False))
            gate.write(protocol, {})
            with patch.object(gate, 'authenticate_reservation', return_value=(directory, {}, [])):
                with self.assertRaisesRegex(ValueError, 'Strict problem-position'):
                    gate.seal(directory, old, new, correct, known, protocol)
            self.assertFalse((directory / 'plan.json').exists())
            self.assertFalse((directory / 'label-started.json').exists())

    def test_shared_execution_restores_dependencies_even_on_failure(self):
        names = ('authenticate', 'search_options', 'exposure')
        saved = {name: getattr(gate.legacy, name) for name in names}
        with self.assertRaisesRegex(RuntimeError, 'fixture'):
            with gate.configured():
                self.assertIs(gate.legacy.authenticate, gate.authenticate)
                self.assertEqual(gate.legacy.search_options(), gate.search_options())
                raise RuntimeError('fixture')
        for name, value in saved.items():
            self.assertIs(getattr(gate.legacy, name), value)

    def test_known_receipt_requires_actual_fixed_moves_complete_iterations_and_same_tactics(self):
        receipt = dict(completed=True, known_position_passed=True, native_mismatches=0,
                       training_runs=0, teacher_queries=0, native_searches=12, nodes_per_search=10000,
                       critical_first_iteration_completed=True, critical_first_iteration_nodes=9999,
                       cases=[])
        for source in gate.known_cases():
            searches = {}
            for role in ('baseline', 'candidate_off', 'candidate', 'candidate_full'):
                searches[role] = dict(options={'UseQuiescenceThreats': True,
                    gate.CACHE_OPTION: role in ('candidate', 'candidate_full'),
                    gate.ORDER_OPTION: role in ('candidate', 'candidate_full')}, nodes=10000,
                    completed_iteration=True, depth=1, move_uci=source['expected_move'],
                    first_iteration_nodes=9999)
            receipt['cases'].append(dict(source=source, passed=True, disabled_path_equal=True,
                                         full_incremental_equal=True, searches=searches))
        gate.validate_known_receipt(receipt)
        broken = deepcopy(receipt); broken['cases'][0]['searches']['candidate']['move_uci'] = 'a2a3'
        with self.assertRaisesRegex(ValueError, 'fixed move'):
            gate.validate_known_receipt(broken)
        broken = deepcopy(receipt); broken['critical_first_iteration_nodes'] = None
        with self.assertRaisesRegex(ValueError, 'first iteration'):
            gate.validate_known_receipt(broken)
        broken = deepcopy(receipt); broken['cases'][0]['searches']['baseline']['options']['UseQuiescenceThreats'] = False
        with self.assertRaisesRegex(ValueError, 'same tactical policy'):
            gate.validate_known_receipt(broken)

    def test_failed_quality_screen_authorizes_zero_games_and_no_retry(self):
        cases = [{'source': {'query_id': str(index), 'initial_fen': chess.STARTING_FEN,
                             'history': [], 'fen': chess.STARTING_FEN},
                  'teacher_bestmove_uci': 'e2e4',
                  'searches': {'warm': {'move_uci': 'e2e4'}, 'candidate': {'move_uci': 'e2e4'}},
                  'child_white_cp': {'warm': 0, 'candidate': 0}}
                 for index in range(16)]
        result = gate.legacy.previous.evaluate_gate(cases)
        self.assertFalse(result['passed'])
        self.assertEqual(result['authorized_games'], 0)
        self.assertFalse(result['retry_authorized'])
        improved = deepcopy(cases)
        for row in improved:
            row['searches']['warm']['move_uci'] = 'd2d4'
            row['child_white_cp']['candidate'] = 20
        result = gate.legacy.previous.evaluate_gate(improved)
        self.assertTrue(result['passed'])
        self.assertEqual(result['authorized_games'], 16)


if __name__ == '__main__':
    unittest.main()
