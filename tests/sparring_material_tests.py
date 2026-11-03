"""Legal local exchange accounting; not search strength or Elo evidence."""
from pathlib import Path
import sys
import unittest
from unittest import mock

import chess

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import sparring_material as material


class MaterialExchangeTests(unittest.TestCase):
    def evaluate(self, fen, square, expected):
        board = chess.Board(fen)
        self.assertTrue(board.is_valid())
        before = board.fen(en_passant="fen"), list(board.move_stack)
        result = material.exchange_gain(board, chess.parse_square(square))
        self.assertTrue(result["complete"], result)
        self.assertEqual(result["status"], "complete")
        self.assertFalse(result["exhausted"])
        self.assertEqual(result["gain_cp"], expected)
        self.assertLessEqual(result["nodes"], result["node_cap"])
        self.assertLessEqual(result["cache_entries"], result["nodes"])
        self.assertEqual(before, (board.fen(en_passant="fen"), list(board.move_stack)))
        return result

    def test_undefended_pawn_and_optional_equal_exchange(self):
        self.evaluate("7k/8/8/8/3p4/4P3/8/7K w - - 0 1", "d4", 100)
        self.evaluate("7k/8/8/2p5/3p4/4P3/8/7K w - - 0 1", "d4", 0)

    def test_poisoned_queen_recapture_is_not_compensation(self):
        # Qxd4 wins a rook immediately but ...Qxd4 wins that queen: decline.
        result = self.evaluate("3q2k1/8/8/8/3r4/8/8/3Q2K1 w - - 0 1", "d4", 0)
        self.assertEqual(result["root_captures_uci"], ["d1d4"])

    def test_real_h5_rook_exchange_includes_the_second_recapture(self):
        board = chess.Board("r2qr1k1/pp3ppp/2p2n2/5b2/1PNb4/P4B2/3R1PPP/3Q1RK1 b - - 2 18")
        board.push_uci("h7h5")
        board.push_uci("d2d4")  # Rxd4 took the bishop; Black may decline Qxd4.
        before = board.fen(en_passant="fen"), list(board.move_stack)
        black_gain = material.exchange_gain(board, chess.D4)
        self.assertTrue(black_gain["complete"], black_gain)
        self.assertEqual(black_gain["gain_cp"], 0)
        self.assertIn("d8d4", black_gain["root_captures_uci"])
        board.push_uci("d8d4")
        white_gain = material.exchange_gain(board, chess.D4)
        self.assertTrue(white_gain["complete"], white_gain)
        self.assertEqual(white_gain["gain_cp"], 900)
        self.assertIn("d1d4", white_gain["root_captures_uci"])
        board.pop()
        self.assertEqual(before, (board.fen(en_passant="fen"), list(board.move_stack)))

    def test_pinned_attacker_and_unsafe_king_capture_are_not_legal_options(self):
        pinned = self.evaluate("4r1k1/8/8/8/8/8/3pR3/2B1K3 w - - 0 1", "d2", 100)
        self.assertNotIn("e2d2", pinned["root_captures_uci"])
        defended = self.evaluate("3r2k1/8/8/8/8/8/3p4/2B1K3 w - - 0 1", "d2", 100)
        self.assertNotIn("e1d2", defended["root_captures_uci"])
        self.assertIn("c1d2", defended["root_captures_uci"])

    def test_all_promotion_choices_and_promotion_value_are_accounted_for(self):
        free = self.evaluate("7k/8/8/8/8/8/1p6/R6K b - - 0 1", "a1", 1300)
        self.assertEqual(set(free["root_captures_uci"]), {"b2a1q", "b2a1r", "b2a1b", "b2a1n"})
        # All four promotions can be recaptured: rook 500 minus pawn 100.
        caught = self.evaluate("7k/8/8/8/8/8/1p5K/R5R1 b - - 0 1", "a1", 400)
        self.assertEqual(set(caught["root_captures_uci"]), {"b2a1q", "b2a1r", "b2a1b", "b2a1n"})

    def test_en_passant_removes_the_off_square_victim(self):
        result = self.evaluate("7k/8/8/3pP3/8/8/8/7K w - d6 0 2", "d6", 100)
        self.assertEqual(result["root_captures_uci"], ["e5d6"])

    def test_node_cutoff_propagates_zero_credit_to_the_top(self):
        board = chess.Board("7k/8/8/8/3p4/4P3/8/7K w - - 0 1")
        before = board.fen()
        incomplete = material.exchange_gain(board, chess.D4, max_nodes=1)
        self.assertEqual(incomplete["gain_cp"], 0)  # Must not be 100 - leaf(0).
        self.assertFalse(incomplete["complete"])
        self.assertTrue(incomplete["exhausted"])
        self.assertEqual(incomplete["reason"], "exchange_node_cap")
        self.assertEqual(incomplete["nodes"], 1)
        self.assertEqual(board.fen(), before)
        complete = material.exchange_gain(board, chess.D4, max_nodes=2)
        self.assertTrue(complete["complete"])
        self.assertEqual(complete["gain_cp"], 100)

    def test_deeper_cutoff_never_returns_partial_best_gain(self):
        board = chess.Board("3q2k1/8/8/8/3r4/8/8/3Q2K1 w - - 0 1")
        result = material.exchange_gain(board, chess.D4, max_nodes=2)
        self.assertFalse(result["complete"])
        self.assertEqual(result["gain_cp"], 0)
        self.assertEqual(result["nodes"], 2)

    def test_time_cutoff_also_supplies_zero_credit(self):
        board = chess.Board("7k/8/8/8/3p4/4P3/8/7K w - - 0 1")
        with mock.patch.object(material.time, "monotonic", side_effect=[0, .002, .002]):
            result = material.exchange_gain(board, chess.D4, max_time_ms=1)
        self.assertFalse(result["complete"])
        self.assertEqual(result["gain_cp"], 0)
        self.assertEqual(result["reason"], "exchange_time_cap")
        self.assertEqual(result["nodes"], 0)

    def test_caller_cannot_disable_hard_work_limits(self):
        board = chess.Board()
        for values in ({"max_nodes": 0}, {"max_nodes": 1025}, {"max_nodes": True},
                       {"max_time_ms": 0}, {"max_time_ms": 101}, {"max_time_ms": None}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                material.exchange_gain(board, chess.D4, **values)
        for square in (-1, 64, True, "d4"):
            with self.subTest(square=square), self.assertRaises(ValueError):
                material.exchange_gain(board, square)


if __name__ == "__main__":
    unittest.main()
