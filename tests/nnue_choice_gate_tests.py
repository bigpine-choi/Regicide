"""Fresh-choice reservation tests; no native, teacher or learning processes."""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import chess

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import neural_training as base
import nnue_choice_gate as gate
import nnue_generalization_gate as previous


def opening():
    moves = ['e2e4', 'e7e5', 'g1f3', 'b8c6']
    board = chess.Board()
    for move in moves:
        board.push_uci(move)
    return {'moves': moves, 'fen_after_opening': board.fen(en_passant='fen'),
            'family_id': 'fixed-family', 'cluster_id': 'fixed-cluster'}


def root_at(length):
    source = opening()
    board = chess.Board()
    for text in source['moves']:
        board.push_uci(text)
    for _ in range(length):
        quiet = [move for move in board.legal_moves
                 if not board.is_capture(move) and not board.gives_check(move)]
        board.push(min(quiet, key=lambda move: move.uci()))
    return board


class ChoiceGateTests(unittest.TestCase):
    def test_fixed_even_family_indices_and_unchanged_gate_criteria(self):
        self.assertEqual(gate.INDICES, list(range(0, 32, 2)))
        self.assertEqual(gate.EXTENSION_PLIES, (4, 6, 8))
        self.assertEqual(gate.CRITERIA, previous.CRITERIA)
        self.assertEqual(gate.CRITERIA['games_if_pass'], 16)
        self.assertEqual(gate.CRITERIA['games_if_fail'], 0)

    def test_exact_four_quiet_lexicographic_moves_and_all_children_reserved(self):
        rows, excluded, blocked = gate.select_roots(set(), openings=[opening()], indices=[0])
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]['deterministic_extension_uci']), 4)
        self.assertEqual(rows[0]['fen'], root_at(4).fen(en_passant='fen'))
        self.assertEqual(blocked, gate.root_features(root_at(4)))
        self.assertEqual(excluded, [])

    def test_exposed_four_ply_child_extends_same_path_to_six(self):
        board = root_at(4)
        board.push(next(iter(board.legal_moves)))
        exposed = {base.feature_identity(board.mirror())}
        rows, excluded, blocked = gate.select_roots(exposed, openings=[opening()], indices=[0])
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]['deterministic_extension_uci']), 6)
        self.assertEqual(rows[0]['fen'], root_at(6).fen(en_passant='fen'))
        self.assertFalse(blocked & exposed)
        self.assertEqual([row['extension_plies'] for row in excluded], [4])

    def test_exposed_four_and_six_extend_to_eight_without_other_family_refill(self):
        exposed = {base.feature_identity(root_at(4)), base.feature_identity(root_at(6))}
        rows, excluded, blocked = gate.select_roots(exposed, openings=[opening()], indices=[0])
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]['deterministic_extension_uci']), 8)
        self.assertEqual([row['extension_plies'] for row in excluded], [4, 6])
        exposed.add(base.feature_identity(root_at(8)))
        rows, excluded, blocked = gate.select_roots(exposed, openings=[opening()], indices=[0])
        self.assertEqual(rows, [])
        self.assertEqual(blocked, set())
        self.assertEqual([row['extension_plies'] for row in excluded], [4, 6, 8])

    def test_configured_binds_latest_warm_and_restores_all_globals_on_error(self):
        names = ('BINARY', 'BINARY_SHA', 'WARM', 'WARM_SHA', 'exposure_inventory')
        old = {name: getattr(previous, name) for name in names}
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            gate.write(directory / 'plan.json', {'criteria': gate.CRITERIA, 'teacher_depth': 10,
                       'warm_model': str(gate.WARM), 'warm_sha256': gate.WARM_SHA,
                       'binary_path': str(directory / 'native.exe'), 'binary_sha256': 'b' * 64})
            with self.assertRaises(RuntimeError):
                with gate.configured(directory):
                    self.assertEqual(previous.WARM, gate.WARM)
                    self.assertEqual(previous.WARM_SHA, gate.WARM_SHA)
                    self.assertEqual(previous.BINARY, directory / 'native.exe')
                    with patch.object(gate, 'exposure_inventory', return_value=({'feature'}, [])) as inventory:
                        self.assertEqual(previous.exposure_inventory(), ({'feature'}, []))
                        inventory.assert_called_once_with(directory=directory)
                    raise RuntimeError('mock failure')
        self.assertEqual({name: getattr(previous, name) for name in names}, old)

    def test_configuration_rejects_stale_warm_or_changed_criteria(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            plan = {'criteria': gate.CRITERIA, 'teacher_depth': 10,
                    'warm_model': str(gate.WARM), 'warm_sha256': previous.WARM_SHA}
            gate.write(directory / 'plan.json', plan)
            with self.assertRaisesRegex(ValueError, 'Choice warm model differs'):
                with gate.configured(directory):
                    self.fail('stale model accepted')
            plan['warm_sha256'] = gate.WARM_SHA
            plan['criteria'] = dict(gate.CRITERIA, minimum_bestmove_match_gain=0)
            gate.write(directory / 'plan.json', plan)
            with self.assertRaisesRegex(ValueError, 'criteria/teacher depth changed'):
                with gate.configured(directory):
                    self.fail('changed criteria accepted')


if __name__ == '__main__':
    unittest.main()
