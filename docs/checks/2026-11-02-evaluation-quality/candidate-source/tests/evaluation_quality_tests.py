"""Measurement integrity checks; these tests never start a chess engine."""
from copy import deepcopy
import json
import pathlib
import sys
import tempfile
import unittest

import chess
import chess.engine

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
from check_evaluation_quality import (advantage, changed_pairs, comparison_pairs, digest,
                                      exact_packet, load_positions, query,
                                      same_reference_choice_delta, summarize, validate_budgets)


def packet(board, cp=30, pv=None):
    return {'depth': 5, 'nodes': 100, 'score': chess.engine.PovScore(chess.engine.Cp(cp), board.turn),
            'pv': [chess.Move.from_uci(move) for move in (pv or ['e2e4', 'e7e5'])]}


def measured(cp=30, best='e2e4', mate=None):
    return {'status': 'ok', 'wall_ms': 1000,
            'result': {'cp_white': cp, 'mate_white': mate, 'best_uci': best, 'depth': 5}}


class FakeAnalysis:
    def __init__(self, packets, move):
        self.packets, self.move = packets, move

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def __iter__(self):
        return iter(self.packets)

    def wait(self):
        return chess.engine.BestMove(chess.Move.from_uci(self.move), None)


class FakeEngine:
    def __init__(self, packets, best='e2e4'):
        self.packets, self.best, self.configured, self.closed = packets, best, [], False

    def configure(self, value):
        self.configured.append(value)

    def analysis(self, board, limit, **kwargs):
        self.board, self.limit, self.kwargs = board, limit, kwargs
        return FakeAnalysis(self.packets, self.best)

    def close(self):
        self.closed = True


class QualityMeasurementTests(unittest.TestCase):
    def test_refinement_budget_is_disabled_or_strictly_longer(self):
        validate_budgets(1000, 2000, 0)
        validate_budgets(1000, 2000, 3000)
        for refined in (500, 1000):
            with self.assertRaisesRegex(ValueError, 'strictly longer'):
                validate_budgets(1000, 2000, refined)
        with self.assertRaisesRegex(ValueError, '100..5000ms'):
            validate_budgets(1000, 2000, 5001)

    def test_frozen_public_fixture_identity_and_legality(self):
        path = pathlib.Path(__file__).resolve().parent / 'data/evaluation_quality_development_v1.json'
        self.assertEqual(digest(path), '8f0d6ce8a03636b4f5b0f8ffd65bdffef8e2efc4a6932a1525d970b73f74ef00')
        _document, roots = load_positions(path)
        self.assertEqual(len(roots), 22)
        self.assertEqual(sum(board.turn == chess.BLACK for _, board in roots), 6)

    def test_white_and_black_perspectives(self):
        white = exact_packet(chess.Board(), packet(chess.Board(), 90))
        self.assertEqual((white['cp_white'], white['cp_mover']), (90, 90))
        board = chess.Board()
        board.push_uci('e2e4')
        black = exact_packet(board, packet(board, 90, ['e7e5', 'g1f3']))
        self.assertEqual((black['cp_white'], black['cp_mover']), (-90, 90))
        self.assertEqual(black['pv_san'], ['e5', 'Nf3'])

    def test_mate_remains_separate_from_centipawns(self):
        board = chess.Board()
        board.push_uci('e2e4')
        info = packet(board, pv=['e7e5'])
        info['score'] = chess.engine.PovScore(chess.engine.Mate(3), chess.BLACK)
        parsed = exact_packet(board, info)
        self.assertEqual((parsed['cp_white'], parsed['mate_white']), (None, -3))
        self.assertEqual(advantage(parsed, 100), 'black')
        self.assertEqual(advantage({'mate_white': 0}), 'mate_zero')
        delta = same_reference_choice_delta(board, measured(mate=2), measured(mate=3))
        self.assertEqual(delta['status'], 'mate_separate')
        self.assertNotIn('new_minus_old_cp_mover', delta)

    def test_bounds_and_incomplete_packets_rejected(self):
        board = chess.Board()
        for key in ('lowerbound', 'upperbound'):
            self.assertIsNone(exact_packet(board, {**packet(board), key: True}))
        for key in ('depth', 'score', 'pv'):
            info = packet(board)
            del info[key]
            self.assertIsNone(exact_packet(board, info))
        for depth in (0, True, '5'):
            self.assertIsNone(exact_packet(board, {**packet(board), 'depth': depth}))
        self.assertIsNone(exact_packet(board, {**packet(board), 'multipv': 2}))

    def test_illegal_pv_wrong_score_turn_and_constrained_root_rejected(self):
        board = chess.Board()
        self.assertIsNone(exact_packet(board, packet(board, pv=['e2e4', 'e4e5'])))
        wrong = {**packet(board), 'score': chess.engine.PovScore(chess.engine.Cp(30), chess.BLACK)}
        self.assertIsNone(exact_packet(board, wrong))
        self.assertIsNone(exact_packet(board, packet(board), chess.Move.from_uci('d2d4')))

    def test_stream_does_not_merge_a_partial_score_into_a_complete_pv(self):
        board = chess.Board()
        complete = packet(board, 30)
        partial = {'depth': 6, 'score': chess.engine.PovScore(chess.engine.Cp(900), chess.WHITE)}
        engine = FakeEngine([complete, partial])
        result = query(engine, board, 100)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['result']['cp_white'], 30)
        self.assertEqual(result['completed_exact_packets'], 1)
        self.assertEqual(len(result['score_packets']), 2)
        self.assertEqual(engine.configured, [{'Clear Hash': None}])
        self.assertFalse(engine.closed)

    def test_stream_rejects_unmatched_final_bestmove(self):
        result = query(FakeEngine([packet(chess.Board())], best='d2d4'), chess.Board(), 100)
        self.assertEqual(result['status'], 'no_exact_matching_final_packet')

    def test_stream_keeps_full_history_and_enforces_searchmove(self):
        board = chess.Board()
        for move in ['g1f3', 'g8f6', 'f3g1', 'f6g8']:
            board.push_uci(move)
        engine = FakeEngine([packet(board)])
        result = query(engine, board, 100, 'e2e4')
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(engine.board.move_stack, board.move_stack)
        self.assertTrue(engine.board.is_repetition(2))
        self.assertEqual(engine.kwargs['root_moves'], [chess.Move.from_uci('e2e4')])
        with self.assertRaisesRegex(ValueError, 'Illegal constrained'):
            query(engine, board, 100, 'e2e5')

    def test_same_reference_delta_uses_side_to_move(self):
        old, new = measured(cp=100), measured(cp=160)
        self.assertEqual(same_reference_choice_delta(chess.Board(), old, new)['new_minus_old_cp_mover'], 60)
        black = chess.Board()
        black.push_uci('e2e4')
        self.assertEqual(same_reference_choice_delta(black, old, new)['new_minus_old_cp_mover'], -60)

    def test_reference_direction_threshold_is_not_cross_engine_mae(self):
        self.assertEqual(advantage(measured(cp=99)['result'], 100), 'neutral')
        self.assertEqual(advantage(measured(cp=100)['result'], 100), 'white')
        self.assertEqual(advantage(measured(cp=-100)['result'], 100), 'black')
        self.assertEqual(advantage(measured(cp=-1)['result']), 'black')

    def test_core_and_refinement_comparisons_are_distinct(self):
        row = {'baseline': measured(best='e2e4'), 'candidate': measured(best='d2d4'),
               'baseline_refined': measured(best='g1f3'), 'candidate_refined': measured(best='d2d4')}
        self.assertEqual(comparison_pairs(0), {'core_short': ('baseline', 'candidate')})
        pairs = changed_pairs(row, 3000)
        self.assertEqual(set(pairs), {'core_short', 'core_refined', 'baseline_refinement'})
        self.assertEqual(len({move for pair in pairs.values() for move in pair}), 3)
        row['candidate']['status'] = 'incomplete'
        self.assertNotIn('core_short', changed_pairs(row, 3000))

    def test_summary_keeps_budgets_and_mates_separate(self):
        row = {'baseline': measured(cp=-10), 'candidate': measured(cp=30),
               'baseline_refined': measured(cp=0), 'candidate_refined': measured(cp=20),
               'reference': measured(cp=150)}
        comparisons = {'core_short': {'delta': {'status': 'finite', 'new_minus_old_cp_mover': 30}},
                       'baseline_refinement': {'delta': {'status': 'finite', 'new_minus_old_cp_mover': -25}},
                       'candidate_refinement': {'delta': {'status': 'mate_separate'}}}
        summary = summarize([row], [{'evaluations': {'e2e4': {}, 'd2d4': {}}, 'comparisons': comparisons}], 3000)
        self.assertEqual(summary['engines']['baseline']['direction_categories'], {'opposed_or_unavailable': 1})
        self.assertEqual(summary['engines']['candidate']['direction_categories'], {'same_direction': 1})
        self.assertEqual(summary['engines']['baseline_refined']['direction_categories'], {'neutral': 1})
        metrics = summary['changed_choice_confirmations']
        self.assertEqual(metrics['core_short']['resource_comparison'], 'equal requested time')
        self.assertEqual(metrics['core_short']['reference_prefers_new_over_20cp'], 1)
        self.assertEqual(metrics['baseline_refinement']['reference_prefers_old_over_20cp'], 1)
        self.assertEqual(metrics['candidate_refinement']['finite'], 0)
        self.assertEqual(summary['constrained_reference_queries'], 2)

    def read_document(self, rows):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / 'positions.json'
            path.write_text(json.dumps({'positions': rows}), encoding='utf-8')
            return load_positions(path)

    def source_row(self):
        board = chess.Board()
        moves = ['g1f3', 'g8f6', 'f3g1', 'f6g8']
        for move in moves:
            board.push_uci(move)
        return {'id': 'root', 'initial_fen': chess.STARTING_FEN,
                'history_uci': moves, 'fen': board.fen(en_passant='fen')}

    def test_loader_preserves_repetition_history(self):
        _document, rows = self.read_document([self.source_row()])
        self.assertEqual(len(rows[0][1].move_stack), 4)
        self.assertTrue(rows[0][1].is_repetition(2))

    def test_loader_rejects_duplicate_ids_illegal_history_and_mismatched_fen(self):
        source = self.source_row()
        with self.assertRaisesRegex(ValueError, 'unique'):
            self.read_document([source, source])
        corrupt = deepcopy(source)
        corrupt['history_uci'] = ['e2e5']
        with self.assertRaisesRegex(ValueError, 'Illegal source'):
            self.read_document([corrupt])
        corrupt = deepcopy(source)
        corrupt['history_uci'] = []
        with self.assertRaisesRegex(ValueError, 'does not match'):
            self.read_document([corrupt])

    def test_loader_rejects_terminal_roots(self):
        fen = '8/8/8/8/8/8/4k3/6K1 w - - 0 1'
        with self.assertRaisesRegex(ValueError, 'Terminal roots'):
            self.read_document([{'id': 'terminal', 'initial_fen': fen, 'history_uci': [], 'fen': fen}])


if __name__ == '__main__':
    unittest.main()
