"""Position-only contracts for the still-unqueried bounded reply screen."""
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
import nnue_reply_budget_gate as gate
import nnue_reservation_audit as historical


def known_receipt():
    receipt = dict(completed=True, known_position_passed=True, cost_gate_passed=True,
                   native_mismatches=0, training_runs=0, teacher_queries=0,
                   native_searches=12, nodes_per_search=10000,
                   critical_first_iteration_completed=True,
                   critical_first_iteration_nodes=9999, cases=[])
    options = gate.search_options()
    for source in gate.known_cases():
        searches = {}
        for role in ('baseline', 'candidate_off', 'candidate', 'candidate_full'):
            configuration = dict(options['candidate' if role in ('candidate', 'candidate_full') else 'warm'])
            if role == 'candidate_off':
                configuration[gate.REPLY_OPTION] = False
            configuration['UseIncrementalNeuralEvaluation'] = role != 'candidate_full'
            first = 2577 if source['name'] == 'white_reply_after_Bf5' else 9999
            searches[role] = dict(options=configuration, nodes=10000,
                completed_iteration=True, depth=1, move_uci=source['expected_move'],
                first_iteration_nodes=first, white_cp=0, pv_uci=[source['expected_move']])
        receipt['cases'].append(dict(source=source, searches=searches, passed=True,
            disabled_path_equal=True, full_incremental_equal=True))
    return receipt


class ReplyBudgetGateTests(unittest.TestCase):
    def test_same_model_threats_cache_candidate_changes_bounded_reply_and_root_priority(self):
        options = gate.search_options()
        self.assertEqual(gate.sha(gate.MODEL), gate.MODEL_SHA)
        for configuration in options.values():
            self.assertIs(configuration['UseQuiescenceThreats'], True)
            self.assertIs(configuration[gate.CACHE_OPTION], True)
            self.assertEqual(configuration['NeuralModelFile'], str(gate.MODEL))
        self.assertIs(options['warm'][gate.ORDER_OPTION], True)
        self.assertIs(options['candidate'][gate.ORDER_OPTION], False)
        self.assertIs(options['candidate'][gate.REPLY_OPTION], True)
        self.assertNotIn(gate.REPLY_OPTION, options['warm'])
        candidate = dict(options['candidate'])
        candidate.pop(gate.REPLY_OPTION); candidate[gate.ORDER_OPTION] = True
        self.assertEqual(candidate, options['warm'])

    def test_frozen_quality_and_cost_thresholds_are_not_weakened(self):
        self.assertEqual(gate.CRITERIA, gate.legacy.previous.CRITERIA)
        self.assertEqual(gate.CRITERIA['minimum_bestmove_match_gain'], 2)
        self.assertEqual(gate.CRITERIA['minimum_mean_mover_quality_gain_cp'], 20)
        self.assertEqual(gate.CRITERIA['minimum_accepted_positions'], 12)
        self.assertEqual(gate.CRITERIA['maximum_regressions_at_least_200cp'], 0)
        self.assertEqual(gate.CRITERIA['games_if_pass'], 16)
        self.assertEqual(gate.CRITERIA['games_if_fail'], 0)
        self.assertFalse(gate.CRITERIA['retry_or_gate_checkpoint_selection'])
        self.assertEqual(gate.BF5_FIRST_NODE_LIMIT, 2577)

    def test_both_historical_reservations_remain_unused_and_exactly_same_bytes(self):
        source, original, previous, reservation, roots, blocked = historical.audit_cost_source()
        self.assertEqual(len(roots), 16)
        self.assertEqual(len(blocked), 594)
        self.assertEqual(original['exposure_files'], 256)
        self.assertEqual({path.name for path in source.iterdir()}, gate.SOURCE_FILES)
        self.assertEqual({path.name for path in previous.iterdir()}, gate.SOURCE_FILES)
        self.assertEqual(reservation['later_overlap_features'], 0)
        for name in ('frozen-roots.json', 'blocked-features.json'):
            self.assertEqual((source / name).read_bytes(), (previous / name).read_bytes())

    def test_previous_reservation_rejects_one_query_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            previous = Path(temporary)
            for name in gate.SOURCE_FILES | {'label-started.json'}:
                gate.write(previous / name, {})
            with patch.object(gate.cost, 'authenticate_source', return_value=(None, {}, [], set())), \
                 self.assertRaisesRegex(ValueError, 'queried, sealed, consumed'):
                gate.authenticate_source(previous)

    def test_delta_replays_only_later_history_and_pv(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            source, previous, experiment = parent / 'old/gate', parent / 'cost/gate-002', parent / 'new'
            source.mkdir(parents=True); previous.mkdir(parents=True); experiment.mkdir()
            historical = experiment / 'historical.json'
            gate.write(historical, {'fen': chess.STARTING_FEN})
            receipt = experiment / 'diagnostic.json'
            gate.write(receipt, {'root': {'fen': chess.STARTING_FEN},
                                 'search': {'pv_uci': ['e2e4', 'e7e5']}})
            reserved = experiment / 'gate-001'; reserved.mkdir()
            gate.write(reserved / 'frozen-roots.json', [{'fen': chess.STARTING_FEN}])
            original = {'input_sha256': {str(historical): gate.sha(historical)}}
            with patch.object(gate, 'authenticate_source', return_value=(source, original,
                    previous, {'input_sha256': {}}, [], {'z' * 64})), \
                 patch.object(gate.cost, 'EXPERIMENT', parent / 'cost'):
                features, paths = gate.delta_inventory(experiment=experiment)
            board = chess.Board(); board.push_uci('e2e4'); board.push_uci('e7e5')
            self.assertIn(base.feature_identity(board), features)
            self.assertEqual(paths, [receipt])

    def test_delta_rejects_even_one_reserved_root_or_child_feature(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            source, previous, experiment = parent / 'old/gate', parent / 'cost/gate-002', parent / 'new'
            source.mkdir(parents=True); previous.mkdir(parents=True); experiment.mkdir()
            gate.write(experiment / 'diagnostic.json', {'fen': chess.STARTING_FEN})
            with patch.object(gate, 'authenticate_source', return_value=(source, {'input_sha256': {}},
                    previous, {'input_sha256': {}}, [], {base.feature_identity(chess.Board())})), \
                 patch.object(gate.cost, 'EXPERIMENT', parent / 'cost'):
                with self.assertRaisesRegex(ValueError, 'entered later diagnostics'):
                    gate.delta_inventory(experiment=experiment)

    def test_reserve_copies_positions_without_teacher_or_native_queries(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            source, previous, destination = parent / 'source', parent / 'previous', parent / 'new'
            source.mkdir(); previous.mkdir()
            for name in gate.SOURCE_FILES:
                (source / name).write_bytes(b'[  ]\n')
                (previous / name).write_bytes(b'[  ]\n')
            original = {'input_sha256': {}, 'exposure_files': 256,
                'exposure_unique_features': 121417, 'limitations': 'Small screen'}
            prior = {'input_sha256': {}, 'later_diagnostic_files': 22,
                     'later_diagnostic_features': 23565}
            with patch.object(gate, 'authenticate_source', return_value=(source, original, previous,
                    prior, [None] * 16, {'x'})), \
                 patch.object(gate, 'delta_inventory', return_value=(set(), [])), \
                 patch.object(gate.legacy, 'label_rows') as teacher:
                reservation = gate.reserve(destination)
            teacher.assert_not_called()
            self.assertEqual(reservation['reserved_roots'], 16)
            self.assertEqual(reservation['search_options'], gate.search_options())
            for name in ('frozen-roots.json', 'blocked-features.json'):
                self.assertEqual((destination / name).read_bytes(), (source / name).read_bytes())
            self.assertFalse((destination / 'plan.json').exists())

    def test_strict_known_receipt_accepts_fixed_moves_costs_and_oracles(self):
        gate.validate_known_receipt(known_receipt())

    def test_wrong_move_or_incomplete_iteration_cannot_pass(self):
        broken = known_receipt()
        for role in ('candidate', 'candidate_full'):
            broken['cases'][0]['searches'][role]['move_uci'] = 'a2a3'
        with self.assertRaisesRegex(ValueError, 'fixed move'):
            gate.validate_known_receipt(broken)
        broken = known_receipt()
        broken['cases'][-1]['searches']['candidate']['first_iteration_nodes'] = None
        with self.assertRaisesRegex(ValueError, 'first iteration'):
            gate.validate_known_receipt(broken)

    def test_bf5_must_recover_original_cost_and_cannot_hide_failed_cost_gate(self):
        broken = known_receipt()
        broken['cases'][1]['searches']['candidate']['first_iteration_nodes'] = 2578
        with self.assertRaisesRegex(ValueError, 'Bf5 first iteration'):
            gate.validate_known_receipt(broken)
        broken = known_receipt(); broken['cost_gate_passed'] = False
        with self.assertRaisesRegex(ValueError, 'Strict problem-position'):
            gate.validate_known_receipt(broken)

    def test_no_dropping_tactical_policy_or_oracle_guards(self):
        broken = known_receipt()
        broken['cases'][1]['searches']['candidate']['options']['UseQuiescenceThreats'] = False
        with self.assertRaisesRegex(ValueError, 'fixed tactical policy'):
            gate.validate_known_receipt(broken)
        broken = known_receipt(); broken['cases'][1]['full_incremental_equal'] = False
        with self.assertRaisesRegex(ValueError, 'oracle equality'):
            gate.validate_known_receipt(broken)
        broken = known_receipt(); broken['cases'][0]['searches']['candidate_full']['white_cp'] = 1
        with self.assertRaisesRegex(ValueError, 'oracle flags'):
            gate.validate_known_receipt(broken)

    def test_seal_rejects_failed_prerequisite_before_gate_answers(self):
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

    def test_shared_execution_always_restores_dependencies(self):
        names = ('authenticate', 'search_options', 'exposure')
        saved = {name: getattr(gate.legacy, name) for name in names}
        with self.assertRaisesRegex(RuntimeError, 'fixture'):
            with gate.configured():
                self.assertIs(gate.legacy.authenticate, gate.authenticate)
                self.assertEqual(gate.legacy.search_options(), gate.search_options())
                raise RuntimeError('fixture')
        for name, value in saved.items():
            self.assertIs(getattr(gate.legacy, name), value)

    def test_same_choices_authorize_zero_games_no_retry(self):
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
