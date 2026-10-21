"""Policy and leakage tests; no engine process or training is launched."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys
import unittest

import chess

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import neural_training as base
import nnue_generalization_gate as gate


def cases(count=12):
    result = []
    for index in range(count):
        result.append({'source': {'query_id': f'root-{index}', 'fen': chess.STARTING_FEN,
                                 'initial_fen': chess.STARTING_FEN, 'history': []},
                       'teacher_bestmove_uci': 'e2e4',
                       'searches': {'warm': {'move_uci': 'e2e4' if index < 2 else 'd2d4'},
                                    'candidate': {'move_uci': 'e2e4' if index < 4 else 'd2d4'}},
                       'child_white_cp': {'warm': 0, 'candidate': 20}})
    return result


class GateTests(unittest.TestCase):
    def test_exact_minimum_boundaries_authorize_only_sixteen(self):
        result = gate.evaluate_gate(cases())
        self.assertTrue(result['passed'])
        self.assertEqual(result['authorized_games'], 16)
        self.assertFalse(result['retry_authorized'])
        self.assertEqual(result['mean_mover_quality_gain_cp'], 20)

    def test_position_count_match_gain_and_native_fail_closed(self):
        self.assertFalse(gate.evaluate_gate(cases(11))['passed'])
        rows = cases()
        rows[3]['searches']['candidate']['move_uci'] = 'd2d4'
        self.assertFalse(gate.evaluate_gate(rows)['passed'])
        self.assertFalse(gate.evaluate_gate(cases(), native_mismatches=1)['passed'])

    def test_quality_and_major_regression_boundaries(self):
        rows = cases()
        rows[0]['child_white_cp']['candidate'] = 19
        self.assertFalse(gate.evaluate_gate(rows)['passed'])
        rows = cases()
        rows[0]['child_white_cp']['candidate'] = -199
        rows[1]['child_white_cp']['candidate'] = 239
        self.assertTrue(gate.evaluate_gate(rows)['passed'])
        rows[0]['child_white_cp']['candidate'] = -200
        rows[1]['child_white_cp']['candidate'] = 240
        self.assertFalse(gate.evaluate_gate(rows)['passed'])

    def test_child_white_scores_use_root_turn_not_child_turn(self):
        self.assertEqual(gate.quality_delta_cp(chess.WHITE, -50, 30), 80)
        self.assertEqual(gate.quality_delta_cp(chess.BLACK, -50, 30), -80)
        self.assertEqual(gate.quality_delta_cp(chess.BLACK, 100, 40), 60)
        with self.assertRaises(ValueError):
            gate.quality_delta_cp(chess.WHITE, None, 0)

    def test_reserve_blocks_every_legal_child_and_color_mirror_leak(self):
        board = chess.Board()
        for text in ('e2e4', 'c7c5', 'g1f3', 'd7d6'):
            board.push_uci(text)
        row = {'initial_fen': chess.STARTING_FEN,
               'history': [move.uci() for move in board.move_stack],
               'fen': board.fen(en_passant='fen')}
        blocked = gate.root_features(board)
        self.assertIn(base.feature_identity(board), blocked)
        for move in board.legal_moves:
            child = board.copy(stack=True)
            child.push(move)
            self.assertIn(base.feature_identity(child), blocked)
            self.assertEqual(base.feature_identity(child), base.feature_identity(child.mirror()))
        gate.assert_reserved_unexposed([row], set())
        child = board.copy(stack=True)
        child.push(next(iter(board.legal_moves)))
        with self.assertRaises(ValueError):
            gate.assert_reserved_unexposed([row], {base.feature_identity(child.mirror())})
        with self.assertRaises(ValueError):
            gate.evaluate_gate([cases()[0], deepcopy(cases()[0])])


if __name__ == '__main__':
    unittest.main()
