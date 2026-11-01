"""History and score-integrity checks for the offline comparison tool."""
import pathlib
import sys
import unittest

import chess
import chess.engine

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
from evaluation_comparison import disagreement, positions, query


class ComparisonTests(unittest.TestCase):
    def record(self, moves):
        board = chess.Board()
        for move in moves:
            board.push_uci(move)
        return {'initial_fen': chess.STARTING_FEN, 'uci_moves': moves,
                'final_fen': board.fen(en_passant='fen')}

    def test_history_preserves_repetitions(self):
        rows = positions(self.record(['g1f3', 'g8f6', 'f3g1', 'f6g8', 'g1f3']))
        self.assertEqual(len(rows[-1][1].move_stack), 4)
        self.assertTrue(rows[-1][1].is_repetition(2))

    def test_corrupt_history_rejected(self):
        record = self.record(['e2e4'])
        record['uci_moves'] = ['e2e5']
        with self.assertRaisesRegex(ValueError, 'Illegal archived'):
            positions(record)
        record = self.record(['e2e4'])
        record['final_fen'] = chess.STARTING_FEN
        with self.assertRaisesRegex(ValueError, 'Final FEN'):
            positions(record)

    def test_black_score_converted_to_white_without_mate_cp_invention(self):
        board = chess.Board()
        board.push_uci('e2e4')
        class FakeEngine:
            def configure(self, options):
                assert options == {'Clear Hash': None}
            def analyse(self, position, limit, **kwargs):
                return {'score': chess.engine.PovScore(chess.engine.Mate(3), chess.BLACK),
                        'pv': [chess.Move.from_uci('e7e5')], 'depth': 4, 'nodes': 500}
        result = query(FakeEngine(), board, 1000)
        self.assertIsNone(result['cp_white'])
        self.assertEqual(result['mate_white'], -3)
        self.assertEqual(result['pv_san'], ['e5'])

    def test_illegal_or_wrong_constrained_pv_rejected(self):
        class FakeEngine:
            def configure(self, options):
                pass
            def analyse(self, position, limit, **kwargs):
                return {'score': chess.engine.PovScore(chess.engine.Cp(20), chess.WHITE),
                        'pv': [chess.Move.from_uci('e2e4'), chess.Move.from_uci('e4e5')]}
        with self.assertRaisesRegex(ValueError, 'Illegal principal'):
            query(FakeEngine(), chess.Board(), 1000)
        class WrongRoot(FakeEngine):
            def analyse(self, position, limit, **kwargs):
                return {'score': chess.engine.PovScore(chess.engine.Cp(20), chess.WHITE),
                        'pv': [chess.Move.from_uci('e2e4')]}
        with self.assertRaisesRegex(ValueError, 'ignored constrained'):
            query(WrongRoot(), chess.Board(), 1000, 'd2d4')

    def test_bounds_do_not_rank_as_exact_disagreements(self):
        bound = {'bound': 'lower', 'mate_white': None, 'cp_white': 1000}
        exact = {'bound': 'exact', 'mate_white': None, 'cp_white': -1000}
        self.assertEqual(disagreement(bound, exact), -1)


if __name__ == '__main__':
    unittest.main()
