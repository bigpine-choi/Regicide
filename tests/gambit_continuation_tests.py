"""Bounded native-PV exchange measurements; no external evaluation or search."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import chess

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import gambit_operations as operations


def replay(moves):
    board = chess.Board()
    for move in moves.split():
        board.push_uci(move)
    return board


def mirrored(pv):
    result = []
    for uci in pv:
        move = chess.Move.from_uci(uci)
        result.append(chess.Move(chess.square_mirror(move.from_square),
                                 chess.square_mirror(move.to_square), promotion=move.promotion).uci())
    return result


BDG = "d2d4 d7d5 e2e4 d5e4 b1c3 g8f6 f2f3 e4f3 g1f3 c8g4"
EXCHANGE = "3r1bk1/8/5n2/3p4/2B1P3/5N2/8/4Q1K1 w - - 0 1"


class ContinuationTests(unittest.TestCase):
    def test_bdg_equal_exchange_is_settled_and_not_development_retreat(self):
        board = replay(BDG)
        before = board.fen(en_passant="fen"), list(board.move_stack)
        pv = "h2h3 g4f3 d1f3".split()
        value = operations.continuation_evidence(board, pv, "development")
        self.assertEqual(value["pv_prefix_uci"], pv)
        self.assertEqual(value["measured_plies"], 3)
        self.assertEqual(value["material_change_cp"], 0)
        self.assertEqual(value["developed_minors_change"], -1)
        self.assertEqual(value["development_progress"], 0)
        self.assertEqual(value["undeveloped_minors_before"], 2)
        self.assertEqual(value["undeveloped_minors_after"], 2)
        self.assertEqual(value["points"], .75)
        self.assertEqual(value["extensions"], [{"ply": 3, "uci": "d1f3", "reason": "recapture"}])
        self.assertEqual((board.fen(en_passant="fen"), board.move_stack), before)
        self.assertEqual(pv, "h2h3 g4f3 d1f3".split())

    def test_missing_recapture_supplies_no_partial_exchange_credit(self):
        board = replay(BDG)
        self.assertIsNone(operations.continuation_evidence(board, "h2h3 g4f3".split(), "development"))

    def test_explicit_quiet_decline_does_not_add_unrelated_pv_development(self):
        board = replay(BDG)
        pv = "h2h3 g4f3 f1e2 e7e6".split()
        value = operations.continuation_evidence(board, pv, "development")
        self.assertEqual(value["pv_prefix_uci"], pv[:2])
        self.assertEqual(value["material_change_cp"], -300)
        self.assertEqual(value["development_progress"], -1)
        self.assertEqual(value["extensions"], [])

    def test_chain_recapture_and_countercheck_are_both_completed(self):
        board = chess.Board(EXCHANGE)
        pv = "e4d5 f6d5 c4d5 d8d5".split()
        value = operations.continuation_evidence(board, pv, "material_recovery")
        self.assertEqual(value["pv_prefix_uci"], pv)
        self.assertEqual(value["material_change_cp"], 0)
        self.assertEqual([row["reason"] for row in value["extensions"]], ["recapture", "check_evasion"])
        self.assertIsNone(operations.continuation_evidence(board, pv[:3], "material_recovery"))

    def test_check_evasion_can_be_a_king_move_not_a_capture(self):
        board = chess.Board("4r1k1/8/8/8/8/8/6PP/2R3K1 w - - 0 1")
        pv = "h2h3 e8e1 g1h2".split()
        self.assertIsNone(operations.continuation_evidence(board, pv[:2], "defensive_transition"))
        value = operations.continuation_evidence(board, pv, "defensive_transition")
        self.assertEqual(value["pv_prefix_uci"], pv)
        self.assertEqual(value["extensions"][0]["reason"], "check_evasion")
        self.assertFalse(chess.Board(value["reply_fen"]).is_check())

    def test_reply_check_cannot_hide_a_postponed_recapture(self):
        board = chess.Board("r2q2k1/p7/8/8/8/8/7P/R3K3 w - - 0 1")
        pv = "a1a7 d8h4 e1e2 a8a7".split()
        value = operations.continuation_evidence(board, pv, "material_recovery")
        self.assertEqual(value["pv_prefix_uci"], pv)
        self.assertEqual(value["material_change_cp"], -400)
        self.assertIsNone(operations.continuation_evidence(board, pv[:3], "material_recovery"))

    def test_native_check_before_recapture_is_bounded_not_invented(self):
        board = replay(BDG)
        pv = "h2h3 g4f3 f1b5 b8c6 d1f3".split()
        value = operations.continuation_evidence(board, pv, "development")
        self.assertEqual(value["pv_prefix_uci"], pv)
        self.assertEqual(value["material_change_cp"], 0)
        self.assertEqual([row["reason"] for row in value["extensions"]],
                         ["intermediate_check", "check_evasion", "recapture"])
        for length in (2, 3, 4):
            self.assertIsNone(operations.continuation_evidence(board, pv[:length], "development"))

    def test_capturing_checker_does_not_forget_the_original_exchange(self):
        board = chess.Board("r2q2k1/p7/8/8/8/5N2/7P/R3K3 w - - 0 1")
        pv = "a1a7 d8h4 f3h4 a8a7".split()
        value = operations.continuation_evidence(board, pv, "material_recovery")
        self.assertEqual(value["material_change_cp"], 500)
        self.assertEqual(value["measured_plies"], 4)
        self.assertIsNone(operations.continuation_evidence(board, pv[:3], "material_recovery"))

    def test_hard_limit_rejects_unfinished_exchange_without_partial_score(self):
        board = chess.Board(EXCHANGE)
        pv = "e4d5 f6d5 c4d5 d8d5".split()
        with patch.object(operations, "MAX_CONTINUATION_PLIES", 3):
            self.assertIsNone(operations.continuation_evidence(board, pv, "material_recovery"))
        with patch.object(operations, "MAX_CONTINUATION_PLIES", 4):
            self.assertEqual(operations.continuation_evidence(board, pv, "material_recovery")["measured_plies"], 4)

    def test_default_eight_ply_limit_stops_a_legal_check_sequence(self):
        board = chess.Board("r2q2k1/p7/8/8/8/8/7P/R3K3 w - - 0 1")
        pv = "a1a7 d8h4 e1d2 h4d4 d2c1 d4c4 c1b1 c4b4 b1a1".split()
        checked = board.copy()
        for uci in pv:
            move = chess.Move.from_uci(uci)
            self.assertIn(move, checked.legal_moves)
            checked.push(move)
        self.assertEqual(operations.MAX_CONTINUATION_PLIES, 8)
        self.assertIsNone(operations.continuation_evidence(board, pv, "material_recovery"))

    def test_pinned_pseudo_recapture_is_not_an_unfinished_legal_exchange(self):
        board = chess.Board("4r1k1/8/8/8/2b5/3N4/4Q2P/4K3 w - - 0 1")
        pv = "h2h3 c4d3".split()
        after = board.copy()
        for uci in pv:
            after.push_uci(uci)
        pinned = chess.Move.from_uci("e2d3")
        self.assertIn(pinned, after.pseudo_legal_moves)
        self.assertNotIn(pinned, after.legal_moves)
        value = operations.continuation_evidence(board, pv, "development")
        self.assertEqual(value["measured_plies"], 2)
        self.assertEqual(value["material_change_cp"], -300)

    def test_quiet_tail_cannot_buy_extra_development_and_move_objects_work(self):
        board = chess.Board()
        prefix = "e2e4 e7e5".split()
        short = operations.continuation_evidence(board, prefix, "development")
        long = operations.continuation_evidence(board,
            [chess.Move.from_uci(uci) for uci in "e2e4 e7e5 g1f3 b8c6 f1c4 g8f6".split()], "development")
        self.assertEqual(short, long)
        self.assertEqual(long["measured_plies"], 2)

    def test_illegal_or_malformed_required_suffix_is_not_measured(self):
        board = replay(BDG)
        for bad in ("d1f4", "not-a-move", None, 7, [], object()):
            with self.subTest(bad=repr(bad)):
                self.assertIsNone(operations.continuation_evidence(board,
                    ["h2h3", "g4f3", bad], "development"))
        for pv in (None, [], ["h2h3"], "h2h3 g4f3", [None, "g4f3"], ["h2h3", None]):
            self.assertIsNone(operations.continuation_evidence(board, pv, "development"))
        self.assertIsNone(operations.continuation_evidence(None, ["h2h3", "g4f3"], "development"))
        self.assertIsNone(operations.continuation_evidence(chess.Board.empty(), ["h2h3", "g4f3"], "development"))
        self.assertIsNone(operations.continuation_evidence(board, ["h2h3", "g4f3"], "unknown"))

    def test_terminal_reply_still_supplies_no_geometric_credit(self):
        board = replay("f2f3 e7e5")
        self.assertIsNone(operations.continuation_evidence(board, ["g2g4", "d8h4"], "development"))

    def test_en_passant_recapture_uses_actual_landing_square(self):
        board = chess.Board("7k/4p3/8/2PpP3/8/8/7P/7K w - d6 0 1")
        value = operations.continuation_evidence(board, "e5d6 e7d6 c5d6".split(), "material_recovery")
        self.assertEqual(value["material_change_cp"], 100)
        self.assertEqual(value["measured_plies"], 3)

    def test_promotion_exchange_uses_real_promoted_material(self):
        board = chess.Board("1rr5/P5kp/8/8/8/6B1/7P/7K w - - 0 1")
        value = operations.continuation_evidence(board, "a7b8q c8b8 g3b8".split(), "material_recovery")
        self.assertEqual(value["material_change_cp"], 900)
        self.assertEqual(value["measured_plies"], 3)

    def test_normal_development_and_retreat_keep_their_direction(self):
        board = chess.Board()
        forward = operations.continuation_evidence(board, "g1f3 d7d5".split(), "development")
        backward = operations.continuation_evidence(replay("g1f3 d7d5"),
                                                     "f3g1 g8f6".split(), "development")
        self.assertEqual(forward["development_progress"], 1)
        self.assertEqual(forward["developed_minors_change"], 1)
        self.assertEqual(backward["development_progress"], -1)
        self.assertEqual(backward["developed_minors_change"], -1)

    def test_losing_undeveloped_minor_never_earns_free_development(self):
        board = chess.Board("1r4k1/7p/8/8/8/8/6KP/1N6 w - - 0 1")
        value = operations.continuation_evidence(board, "h2h3 b8b1".split(), "development")
        self.assertEqual(value["material_change_cp"], -300)
        self.assertEqual(value["undeveloped_minors_before"] - value["undeveloped_minors_after"], 1)
        self.assertEqual(value["development_progress"], 0)
        self.assertEqual(value["development_change_basis"], "raw_developed_change_after_material_loss")

    def test_material_loss_counts_only_actual_surviving_development(self):
        board = chess.Board("1r4k1/7p/8/8/8/8/6PK/1N4N1 w - - 0 1")
        value = operations.continuation_evidence(board, "g1f3 b8b1".split(), "development")
        self.assertEqual(value["material_change_cp"], -300)
        self.assertEqual(value["undeveloped_minors_before"] - value["undeveloped_minors_after"], 2)
        self.assertEqual(value["development_progress"], 1)

    def test_owner_color_symmetry_for_settlement_and_development(self):
        cases = [(replay(BDG), "h2h3 g4f3 d1f3".split()),
                 (chess.Board(EXCHANGE), "e4d5 f6d5 c4d5 d8d5".split()),
                 (chess.Board(), "g1f3 d7d5".split()),
                 (chess.Board("1r4k1/7p/8/8/8/8/6KP/1N6 w - - 0 1"), "h2h3 b8b1".split())]
        for board, pv in cases:
            with self.subTest(fen=board.fen()):
                white = operations.continuation_evidence(board, pv, "development")
                black = operations.continuation_evidence(board.mirror(), mirrored(pv), "development")
                for key in ("points", "material_change_cp", "developed_minors_change",
                            "development_progress", "king_danger_reduction", "pressure_change", "measured_plies"):
                    self.assertEqual(white[key], black[key], key)


if __name__ == "__main__":
    unittest.main()
