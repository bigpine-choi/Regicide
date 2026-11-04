"""Local investment is geometry, never an oracle for a good chess move."""
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

import chess

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import sound_sacrifice as local


class GeometryTests(unittest.TestCase):
    def test_new_knight_offer_counts_net_investment_and_preserves_history(self):
        board = chess.Board("k5r1/5p2/8/8/5N2/1B6/8/7K w - - 0 1")
        before = board.fen(), list(board.move_stack)
        value = local.evidence(board, "f4g6")
        self.assertTrue(value["eligible"], value)
        offer = next(row for row in value["offers"] if row["capture_uci"] == "f7g6")
        self.assertEqual(offer["net_investment_cp"], 300)
        self.assertTrue(offer["moved_piece_is_bait"] and offer["recapture_exchange"]["complete"])
        self.assertEqual(before, (board.fen(), list(board.move_stack)))

    def test_immediate_capture_credit_excludes_equal_nonpawn_exchange(self):
        board = chess.Board("7k/8/4p3/3b4/5N2/8/8/K7 w - - 0 1")
        value = local.evidence(board, "f4d5")
        self.assertFalse(value["eligible"])
        offer = next(row for row in value["offers"] if row["capture_uci"] == "e6d5")
        self.assertEqual(offer["candidate_immediate_gain_cp"], 300)
        self.assertEqual(offer["net_investment_cp"], 0)

    def test_complete_recapture_credit_excludes_equal_exchange(self):
        board = chess.Board("7k/8/8/7b/8/4N2P/8/7K w - - 0 1")
        value = local.evidence(board, "e3g4")
        self.assertFalse(value["eligible"], value)
        offer = next(row for row in value["offers"] if row["capture_uci"] == "h5g4")
        self.assertEqual(offer["recapture_exchange"]["gain_cp"], 300)
        self.assertEqual(offer["net_investment_cp"], 0)

    def test_leaving_an_already_hanging_piece_is_not_a_new_sacrifice(self):
        board = chess.Board("7k/8/8/4p3/5N2/8/8/R6K w - - 0 1")
        value = local.evidence(board, "a1a2")
        self.assertFalse(value["eligible"])
        self.assertEqual(value["offers"], [])

    def test_relocating_an_already_hanging_piece_is_not_a_new_investment(self):
        board = chess.Board("k5r1/5p2/8/4p3/5N2/1B6/8/7K w - - 0 1")
        value = local.evidence(board, "f4g6")
        self.assertFalse(value["eligible"])
        self.assertTrue(value["moved_piece_already_hanging"])
        self.assertEqual(value["prior_moved_piece_exchanges"][0]["net_investment_cp"], 300)

    def test_a_preexisting_attack_with_complete_equal_recapture_does_not_disqualify(self):
        board = chess.Board("k5r1/5p2/8/6b1/5N2/1B2P3/8/7K w - - 0 1")
        value = local.evidence(board, "f4g6")
        self.assertTrue(value["eligible"], value)
        self.assertFalse(value["moved_piece_already_hanging"])
        self.assertEqual(value["prior_moved_piece_exchanges"][0]["net_investment_cp"], 0)

    def test_moving_a_knight_can_newly_expose_a_different_queen(self):
        board = chess.Board()
        for san in "e4 e5 Nf3 d6 Bc4 Bg4 Nc3 g6".split():
            board.push_san(san)
        before = board.fen(), list(board.move_stack)
        value = local.evidence(board, "f3e5")
        queen = next(offer for offer in value["offers"] if offer["capture_uci"] == "g4d1")
        self.assertTrue(value["eligible"] and queen["eligible"])
        self.assertEqual(queen["victim_piece"], "queen")
        self.assertFalse(queen["moved_piece_is_bait"])
        self.assertTrue(queen["newly_exposed"])
        self.assertEqual(before, (board.fen(), list(board.move_stack)))

    def test_an_illegal_pinned_capture_cannot_establish_a_sacrifice(self):
        board = chess.Board("4k3/4b3/8/8/1Q6/8/8/K3R3 w - - 0 1")
        value = local.evidence(board, "b4d6")
        self.assertFalse(value["eligible"], value)

    def test_pawns_only_illegal_moves_and_check_evasion_are_excluded(self):
        board = chess.Board()
        self.assertFalse(local.evidence(board, "e2e4")["eligible"])
        self.assertEqual(local.evidence(board, "e2e5")["reason"], "invalid_move")
        checked = chess.Board("6k1/5ppp/8/8/8/8/5PqP/6K1 w - - 0 30")
        self.assertEqual(local.evidence(checked, next(iter(checked.legal_moves)))["reason"], "in_check")
        self.assertEqual(local.evidence(board, "e1e3")["reason"], "invalid_move")

    def test_one_legal_reply_is_not_a_discretionary_sacrifice(self):
        board = chess.Board("7k/8/5K2/6Q1/8/8/8/8 b - - 0 1")
        self.assertEqual(board.legal_moves.count(), 1)
        self.assertEqual(local.evidence(board, next(iter(board.legal_moves)))["reason"], "forced_or_terminal")

    def test_incomplete_exchange_and_expired_or_nonfinite_deadline_fail_closed(self):
        board = chess.Board("k5r1/5p2/8/8/5N2/1B6/8/7K w - - 0 1")
        with patch.object(local.sparring_material, "exchange_gain", return_value={"gain_cp": 0, "complete": False}):
            value = local.evidence(board, "f4g6")
        self.assertFalse(value["eligible"] or value["complete"])
        self.assertIsNone(value["investment_cp"])
        self.assertEqual(value["reason"], "exchange_incomplete")
        self.assertFalse(local.evidence(board, "f4g6", deadline=time.monotonic() - 1)["eligible"])
        for deadline in (float("nan"), float("inf")):
            self.assertEqual(local.evidence(board, "f4g6", deadline=deadline)["reason"], "invalid_deadline")


if __name__ == "__main__":
    unittest.main()
