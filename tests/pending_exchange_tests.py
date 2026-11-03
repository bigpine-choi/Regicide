"""Recent equal exchange receipts never authorize a new material gift."""
import json
from pathlib import Path
import sys
import unittest
from unittest import mock
import chess
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"scripts"))
import pending_exchange
import sparring_play


def white_pending():
    board=chess.Board()
    for uci in "e2e4 e7e5 g1f3 b8c6 f1b5 a7a6 b5c6 g7g6".split():
        board.push_uci(uci)
    return board


def black_pending():
    board=chess.Board()
    for uci in ("e2e3 g8f6 d2d4 b8c6 f1e2 e7e6 a2a3 d7d5 b2b4 f8d6 g1f3 e8g8 "
                "c1d2 e6e5 d4e5 c6e5 f3e5 d6e5 d2c3 f8e8 e1g1 c8d7 a1a2 c7c6 c3d4 e5d4 e2f3").split():
        board.push_uci(uci)
    return board


def changed_attacker_pending():
    board=chess.Board()
    sequence=("b1c3 c7c5 g1f3 b8c6 e2e4 e7e6 f1c4 a7a6 d2d4 c6a5 c4e2 c5d4 f3d4 "
              "b7b5 e1g1 f8c5 d1d3 c8b7 c1e3 c5a7 f1b1 h7h6 d3d2 a8c8 d4b3 a7b8 b3a5 d8c7")
    for uci in sequence.split():board.push_uci(uci)
    return board


class PendingExchangeTests(unittest.TestCase):
    def test_both_colors_can_complete_the_immediately_declined_equal_exchange(self):
        for board,uci,square in [(white_pending(),"h2h3","c6"),(black_pending(),"a7a6","d4")]:
            with self.subTest(turn=board.turn):
                move=chess.Move.from_uci(uci)
                self.assertIn(move,board.legal_moves)
                self.assertFalse(sparring_play.naturalness(board,move)["admitted"])
                value=pending_exchange.naturalness(board,move)
                self.assertTrue(value["admitted"])
                self.assertEqual(value["reason"],"pending_equal_exchange")
                proof=value["pending_exchange"]
                self.assertEqual(proof["piece_square"],square)
                self.assertEqual(proof["maximum_exchange_credit_cp"],300)
                self.assertGreaterEqual(proof["captured_value_cp"],300)
                self.assertTrue(set(proof["permitted_replies"])<=set(proof["original_legal_recaptures"]))

    def test_opponent_may_change_attacker_before_the_quiet_candidate(self):
        board=changed_attacker_pending();move=chess.Move.from_uci("g1f1")
        strict=sparring_play.naturalness(board,move)
        self.assertFalse(strict["admitted"])
        value=pending_exchange.naturalness(board,move)
        self.assertTrue(value["admitted"])
        proof=value["pending_exchange"]
        self.assertEqual(proof["piece_square"],"a5")
        self.assertIn("d8a5",proof["original_legal_recaptures"])
        self.assertIn("c7a5",proof["existing_legal_recaptures"])
        self.assertIn("c7a5",proof["permitted_replies"])
        self.assertEqual(proof["opponent_changed_recapture"],["c7a5"])
        self.assertEqual(proof["maximum_exchange_credit_cp"],300)
        self.assertFalse(value["strict_naturalness"]["admitted"])

    def test_castling_preserves_the_same_pending_piece_and_still_checks_other_losses(self):
        board=white_pending();move=chess.Move.from_uci("e1g1")
        self.assertTrue(board.is_castling(move))
        self.assertTrue(pending_exchange.naturalness(board,move)["admitted"])
        self.assertEqual(pending_exchange.naturalness(board,move)["pending_exchange"]["piece_square"],"c6")

    def test_new_queen_donation_cannot_be_offset_by_the_old_bishop_capture(self):
        board=black_pending();move=chess.Move.from_uci("d8a5")
        self.assertIn(move,board.legal_moves)
        value=pending_exchange.naturalness(board,move)
        self.assertFalse(value["admitted"])
        self.assertIsNone(value["pending_exchange"])
        self.assertTrue(any(row["victim_cp"]==900 for row in value["vulnerable_captures"]))

    def test_moving_the_exposed_piece_to_a_new_capture_square_is_not_credited(self):
        board=black_pending();move=chess.Move.from_uci("d4c3")
        self.assertIn(move,board.legal_moves)
        value=pending_exchange.naturalness(board,move)
        self.assertFalse(value["admitted"])
        self.assertIsNone(value["pending_exchange"])

    def test_credit_expires_after_one_declined_reply(self):
        board=white_pending();board.push_uci("h2h3");board.push_uci("h7h6")
        value=pending_exchange.naturalness(board,chess.Move.from_uci("a2a3"))
        self.assertFalse(value["admitted"])
        self.assertIsNone(value["pending_exchange"])

    def test_knight_for_pawn_is_not_an_equal_exchange(self):
        board=chess.Board()
        for uci in "g1f3 a7a6 f3e5 a6a5 e5c4 b7b6 c4a5 g7g6".split():board.push_uci(uci)
        value=pending_exchange.naturalness(board,chess.Move.from_uci("h2h3"))
        self.assertFalse(value["admitted"])
        self.assertIsNone(value["pending_exchange"])

    def test_identical_position_without_history_cannot_invent_exchange_credit(self):
        board=white_pending();board=chess.Board(board.fen())
        value=pending_exchange.naturalness(board,chess.Move.from_uci("h2h3"))
        self.assertFalse(value["admitted"])
        self.assertIsNone(value["pending_exchange"])

    def test_en_passant_is_never_a_major_piece_exchange_receipt(self):
        board=chess.Board()
        for uci in "e2e4 a7a6 e4e5 d7d5 e5d6 c7c6".split():board.push_uci(uci)
        self.assertTrue(board.root().is_valid())
        value=pending_exchange.naturalness(board,chess.Move.from_uci("h2h3"))
        self.assertIsNone(value["pending_exchange"])

    def test_ordinary_safe_moves_are_unchanged(self):
        board=chess.Board();move=chess.Move.from_uci("e2e4")
        strict=sparring_play.naturalness(board,move)
        value=pending_exchange.naturalness(board,move)
        self.assertTrue(value["admitted"])
        self.assertEqual({key:value[key] for key in strict},strict)
        self.assertIsNone(value["pending_exchange"])

    def test_incomplete_capture_tree_cannot_receive_exchange_credit(self):
        board=white_pending();move=chess.Move.from_uci("h2h3")
        with mock.patch.object(sparring_play.sparring_material,"exchange_gain",return_value={"gain_cp":0,"complete":False}):
            value=pending_exchange.naturalness(board,move)
        self.assertFalse(value["admitted"])
        self.assertIsNone(value["pending_exchange"])


if __name__=="__main__":unittest.main()
