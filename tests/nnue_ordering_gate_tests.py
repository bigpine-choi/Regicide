"""The fixed move-choice gate rejects regression, leaked signs and false PVs."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

import chess

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import nnue_ordering_gate as gate


def cases(matching):
    # Warm correct roots 2 and 7, of which root 7 belongs to tuning.
    pairs = [1, 2, 3, 4, 5, 6, 7, 7, 8, 8]
    return [{'source': {'query_id': str(i), 'source_pair': pairs[i]},
             'teacher_bestmove_uci': 'e2e4',
             'searches': {'warm': {'move_uci': 'e2e4' if i in (2, 7) else 'd2d4'},
                          'candidate': {'move_uci': 'e2e4' if i in matching else 'd2d4'}}}
            for i in range(10)]


def packet(board, cp=-30, pv=None):
    return {'nodes': gate.NODES, 'depth': 3,
            'score': {'pov': 'white' if board.turn else 'black',
                      'relative': str(cp if board.turn else -cp),
                      'white_cp': cp, 'white_mate': None},
            'pv': pv or ['e7e5']}


class OrderingGateTests(unittest.TestCase):
    def test_gate_requires_gain_preservation_and_tuning_gain(self):
        passed = gate.evaluate_gate(cases({0, 1, 2, 6, 7}))
        self.assertTrue(passed['passed'])
        self.assertEqual((passed['warm_matches'], passed['candidate_matches']), (2, 5))
        self.assertEqual(passed['candidate_tuning_matches'], 2)
        self.assertFalse(passed['retry_authorized'])
        for matching in ({0, 2, 6, 7}, {0, 1, 3, 6, 7}, {0, 1, 2, 3, 7}):
            with self.subTest(matching=matching):
                self.assertFalse(gate.evaluate_gate(cases(matching))['passed'])

    def test_gate_rejects_changed_baseline_and_duplicate_roots(self):
        values = cases({0, 1, 2, 6, 7})
        values[0]['searches']['warm']['move_uci'] = 'e2e4'
        with self.assertRaisesRegex(ValueError, 'Warm total baseline'):
            gate.evaluate_gate(values)
        values = cases({0, 1, 2, 6, 7})
        values[0]['source']['query_id'] = values[1]['source']['query_id']
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            gate.evaluate_gate(values)

    def test_final_teacher_packet_rejects_bound_and_black_sign_error(self):
        board = chess.Board()
        board.push_uci('e2e4')
        actual = packet(board)
        self.assertEqual(gate.score_packet(actual, board, exact_cp=True), (-30, None))
        for changes, message in (({'lowerbound': True}, 'bound'),
                                 ({'upperbound': True}, 'bound')):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, message):
                gate.score_packet(dict(actual, **changes), board, exact_cp=True)
        wrong = deepcopy(actual)
        wrong['score']['relative'] = '-30'
        with self.assertRaisesRegex(ValueError, 'sign'):
            gate.score_packet(wrong, board, exact_cp=True)
        wrong = deepcopy(actual)
        wrong['score']['white_cp'] = None
        wrong['score']['white_mate'] = 2
        with self.assertRaisesRegex(ValueError, 'must be CP'):
            gate.score_packet(wrong, board, exact_cp=True)

    def test_replayed_history_and_legal_pv_are_required(self):
        board = chess.Board()
        board.push_uci('e2e4')
        source = {'initial_fen': chess.STARTING_FEN, 'history': ['e2e4'],
                  'fen': board.fen(en_passant='fen')}
        self.assertEqual(len(gate.source_board(source).move_stack), 1)
        with self.assertRaisesRegex(ValueError, 'history'):
            gate.source_board(dict(source, history=['d2d4']))
        gate.legal_pv(board, ['e7e5', 'g1f3'], 'e7e5')
        with self.assertRaisesRegex(ValueError, 'PV/bestmove'):
            gate.legal_pv(board, ['e7e5'], 'c7c5')
        with self.assertRaisesRegex(ValueError, 'Illegal PV'):
            gate.legal_pv(board, ['e7e5', 'g1g5'], 'e7e5')

    def test_search_validates_weight_hash_options_nodes_and_packet(self):
        board = chess.Board()
        board.push_uci('e2e4')
        raw = packet(board)
        search = {'model_sha256': 'frozen', 'options': {'Hash': 16},
                  'nodes': gate.NODES, 'depth': 3, 'raw_info': raw,
                  'white_cp': -30, 'white_mate': None, 'pv_uci': ['e7e5'],
                  'move_uci': 'e7e5', 'move_san': 'e5'}
        gate.validate_search(search, board, {'Hash': 16}, 'frozen')
        for changes, message in (({'model_sha256': 'other'}, 'model hash'),
                                 ({'options': {'Hash': 32}}, 'options'),
                                 ({'nodes': gate.NODES - 1}, 'node request'),
                                 ({'white_cp': 30}, 'score mismatch')):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, message):
                gate.validate_search(dict(search, **changes), board, {'Hash': 16}, 'frozen')


if __name__ == '__main__':
    unittest.main()
