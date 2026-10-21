"""Position-only reservation and immutable dual-binary gate contracts."""
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
import nnue_tactical_search_gate as gate


def opening():
    history = ['e2e4', 'e7e5', 'g1f3', 'b8c6']
    board = chess.Board()
    for text in history:
        board.push_uci(text)
    return {'moves': history, 'fen_after_opening': board.fen(en_passant='fen'),
            'family_id': 'fixed-family', 'cluster_id': 'fixed-cluster'}


class TacticalSearchGateTests(unittest.TestCase):
    def test_fixed_even_families_unchanged_criteria_and_frozen_model(self):
        self.assertEqual(gate.INDICES, list(range(0, 32, 2)))
        self.assertEqual(gate.EXTENSION_PLIES, (4, 6, 8, 10))
        self.assertEqual(gate.CRITERIA, gate.previous.CRITERIA)
        self.assertEqual(gate.CRITERIA['games_if_pass'], 16)
        self.assertEqual(gate.CRITERIA['games_if_fail'], 0)
        self.assertEqual(gate.sha(gate.MODEL), gate.MODEL_SHA)

    def test_deterministic_second_quiet_move_avoids_ancestral_repetitions(self):
        source = opening()
        rows, excluded, blocked = gate.select_roots(set(), openings=[source], indices=[0])
        self.assertEqual(len(rows), 1)
        self.assertEqual(excluded, [])
        board, visited = chess.Board(), set()
        visited.add(base.feature_identity(board))
        for text in source['moves']:
            board.push_uci(text)
            visited.add(base.feature_identity(board))
        for text in rows[0]['deterministic_extension_uci']:
            choices = []
            for move in sorted(board.legal_moves, key=lambda move: move.uci()):
                child = board.copy(stack=False)
                child.push(move)
                if not board.is_capture(move) and not board.gives_check(move) and base.feature_identity(child) not in visited:
                    choices.append(move)
            self.assertEqual(text, choices[min(1, len(choices) - 1)].uci())
            board.push_uci(text)
            self.assertNotIn(base.feature_identity(board), visited)
            visited.add(base.feature_identity(board))
        self.assertEqual(len(rows[0]['deterministic_extension_uci']), 4)
        self.assertEqual(rows[0]['fen'], board.fen(en_passant='fen'))
        self.assertEqual(blocked, gate.root_features(board))

    def test_only_exposure_extends_same_path_without_outcome_refill(self):
        source = opening()
        rows, _, _ = gate.select_roots(set(), openings=[source], indices=[0])
        first = rows[0]
        rows, exclusions, blocked = gate.select_roots({first['feature_hash']}, openings=[source], indices=[0])
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]['deterministic_extension_uci']), 6)
        self.assertEqual(rows[0]['deterministic_extension_uci'][:4], first['deterministic_extension_uci'])
        self.assertEqual([row['extension_plies'] for row in exclusions], [4])
        self.assertNotIn(first['feature_hash'], blocked)
        cumulative, lengths = set(), []
        while rows:
            cumulative.add(rows[0]['feature_hash'])
            cumulative.add(first['feature_hash'])
            rows, exclusions, _ = gate.select_roots(cumulative, openings=[source], indices=[0])
            lengths.extend([row['extension_plies'] for row in exclusions])
        self.assertIn(10, lengths)

    def test_history_and_nested_pv_exposure_include_consumed_gate_children(self):
        source = opening()
        rows, _, blocked = gate.select_roots(set(), openings=[source], indices=[0])
        row = rows[0]
        board = gate.source_board(row)
        move = next(iter(board.legal_moves))
        board.push(move)
        observed = gate.exposure.position_features({'root': {'fen': row['fen']},
                    'search': {'pv': [move.uci()]}, 'history': row['history']})
        self.assertIn(base.feature_identity(board), observed)
        self.assertIn(row['feature_hash'], observed)
        gate.previous.assert_reserved_unexposed(rows, set())
        with self.assertRaisesRegex(ValueError, 'entered train/checkpoint'):
            gate.previous.assert_reserved_unexposed(rows, observed)
        self.assertIn(base.feature_identity(board), blocked)

    def test_equal_model_budget_and_single_explicit_search_option(self):
        options = gate.search_options()
        self.assertEqual(options['warm']['NeuralModelFile'], str(gate.MODEL))
        self.assertEqual(options['candidate']['NeuralModelFile'], str(gate.MODEL))
        self.assertNotIn('UseQuiescenceThreats', options['warm'])
        self.assertIs(options['candidate']['UseQuiescenceThreats'], True)
        candidate = dict(options['candidate']); candidate.pop('UseQuiescenceThreats')
        self.assertEqual(candidate, options['warm'])

    def test_seal_rejects_failed_known_case_before_fresh_labels(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            old, new = directory / 'old.exe', directory / 'new.exe'
            old.write_bytes(b'old'); new.write_bytes(b'new')
            correct, known, protocol = (directory / name for name in ('correct.json', 'known.json', 'protocol.json'))
            binding = {'model_sha256': gate.MODEL_SHA, 'old_binary_sha256': gate.sha(old),
                       'new_binary_sha256': gate.sha(new)}
            gate.write(correct, dict(binding, completed=True, passed=True, native_mismatches=0))
            gate.write(known, dict(binding, completed=True, known_position_passed=False))
            gate.write(protocol, {})
            with patch.object(gate, 'authenticate_reservation', return_value=(directory, {}, [])):
                with self.assertRaisesRegex(ValueError, 'Problem-position'):
                    gate.seal(directory, old, new, correct, known, protocol)
            self.assertFalse((directory / 'plan.json').exists())
            self.assertFalse((directory / 'label-started.json').exists())

    def test_seal_rejects_same_binary_and_wrong_model_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            old, new = directory / 'old.exe', directory / 'new.exe'
            old.write_bytes(b'old'); new.write_bytes(b'new')
            correct, known, protocol = (directory / name for name in ('correct.json', 'known.json', 'protocol.json'))
            binding = {'model_sha256': '0' * 64, 'old_binary_sha256': gate.sha(old),
                       'new_binary_sha256': gate.sha(new)}
            gate.write(correct, dict(binding, completed=True, passed=True, native_mismatches=0))
            gate.write(known, dict(binding, completed=True, known_position_passed=True))
            gate.write(protocol, {})
            with patch.object(gate, 'authenticate_reservation', return_value=(directory, {}, [])):
                with self.assertRaisesRegex(ValueError, 'binaries must differ'):
                    gate.seal(directory, old, old, correct, known, protocol)
                with self.assertRaisesRegex(ValueError, 'another model'):
                    gate.seal(directory, old, new, correct, known, protocol)

    def test_seal_binds_both_binaries_protocol_and_prerequisites_without_queries(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            old, new = directory / 'old.exe', directory / 'new.exe'
            old.write_bytes(b'old'); new.write_bytes(b'new')
            correct, known, protocol, profile = (directory / name for name in
                                                ('correct.json', 'known.json', 'protocol.json', 'profile.json'))
            binding = {'model_sha256': gate.MODEL_SHA, 'old_binary_sha256': gate.sha(old),
                       'new_binary_sha256': gate.sha(new)}
            gate.write(correct, dict(binding, completed=True, passed=True, native_mismatches=0))
            gate.write(known, dict(binding, completed=True, known_position_passed=True))
            gate.write(protocol, {}); gate.write(profile, {'options': {'UseQuiescenceThreats': True}})
            gate.write(directory / 'reservation.json', {'input_sha256': {}})
            reservation = {'input_sha256': {}}
            with patch.object(gate, 'authenticate_reservation', return_value=(directory, reservation, [])), \
                 patch.object(gate.exposure, 'exposure_inventory', return_value=(set(), [])), \
                 patch.object(gate, 'CANDIDATE_PROFILE', profile), \
                 patch.object(gate, 'label_rows') as teacher:
                plan = gate.seal(directory, old, new, correct, known, protocol)
            teacher.assert_not_called()
            for path in (old, new, correct, known, protocol, profile):
                self.assertEqual(plan['input_sha256'][str(path)], gate.sha(path))
            with patch.object(gate, 'authenticate_reservation', return_value=(directory, reservation, [])):
                with self.assertRaisesRegex(ValueError, 'no reselection'):
                    gate.seal(directory, old, new, correct, known, protocol)

    def test_both_color_cp_sign_and_unchanged_gate_thresholds(self):
        self.assertEqual(gate.previous.quality_delta_cp(chess.WHITE, 10, 40), 30)
        self.assertEqual(gate.previous.quality_delta_cp(chess.BLACK, 10, 40), -30)
        cases = []
        for index in range(16):
            cases.append({'source': {'query_id': str(index), 'initial_fen': chess.STARTING_FEN,
                                    'history': [], 'fen': chess.STARTING_FEN},
                'teacher_bestmove_uci': 'e2e4', 'searches': {'warm': {'move_uci': 'd2d4'}, 'candidate': {'move_uci': 'e2e4'}},
                'child_white_cp': {'warm': 0, 'candidate': 20}})
        result = gate.previous.evaluate_gate(cases)
        self.assertTrue(result['passed']); self.assertEqual(result['authorized_games'], 16)
        broken = deepcopy(cases); broken[0]['child_white_cp']['candidate'] = -200
        result = gate.previous.evaluate_gate(broken)
        self.assertFalse(result['passed']); self.assertEqual(result['authorized_games'], 0)
        self.assertFalse(result['retry_authorized'])


if __name__ == '__main__':
    unittest.main()
