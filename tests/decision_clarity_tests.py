"""Independent decision-clarity examples; no external engine or real sleeps."""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

import chess

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import decision_support as support
import engine_pacing


FREE_QUEEN = "6k1/5ppp/8/8/3q4/8/3R4/5K2 w - - 0 30"
RECAPTURE_QUEEN = "3r2k1/5ppp/8/3q4/8/8/5PPP/3Q2K1 w - - 0 20"
TACTICAL_EARLY = "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1"


def clock(board, *, left=600000, spent=0, increment=0):
    own = "white" if board.turn else "black"
    return {"enabled": True, "status": "running", "running_color": own,
            "white_ms": left if board.turn else 600000,
            "black_ms": 600000 if board.turn else left,
            "turn_elapsed_ms": spent, "increment_ms": increment}


def line(san):
    board = chess.Board()
    for move in san.split():
        board.push_san(move)
    return board


def assess(board, **times):
    return support.assess(board, board.turn, clock(board, **times))


class DecisionClarityTests(unittest.TestCase):
    def test_known_opening_has_short_budget_with_familiarity_qualification(self):
        for board in (chess.Board(), line("e4 e5 Nf3 Nc6")):
            with self.subTest(fen=board.fen()):
                value = assess(board)
                self.assertEqual(value["complexity"]["clarity"]["kind"], "familiar_opening")
                self.assertTrue(value["complexity"]["clarity"]["familiarity_assumed"])
                self.assertLessEqual(value["recommendation"]["total_ms"], 2500)
                self.assertIn("불확실", value["recommendation"]["reason"])

    def test_early_move_number_alone_does_not_receive_book_discount(self):
        board = chess.Board(TACTICAL_EARLY)
        self.assertTrue(board.is_valid())
        value = assess(board)
        self.assertEqual(value["complexity"]["clarity"]["kind"], "unclassified")
        self.assertGreater(value["recommendation"]["total_ms"], 2500)
        invalid = support.assess(chess.Board(None), chess.WHITE, clock(chess.Board()))
        self.assertEqual(invalid["status"], "inactive")

    def test_exact_book_recognition_retains_castling_rights_and_move_counter(self):
        original = chess.Board()
        self.assertEqual(assess(original)["complexity"]["clarity"]["kind"], "familiar_opening")
        no_rights = chess.Board(); no_rights.castling_rights = 0
        late = chess.Board(); late.fullmove_number = 11
        for board in (no_rights, late):
            self.assertEqual(assess(board)["complexity"]["clarity"]["kind"], "unclassified")

    def test_single_legal_move_has_small_budget_for_both_colors(self):
        board = chess.Board("8/8/8/8/8/k7/8/Kr6 w - - 0 1")
        for current in (board, board.mirror()):
            self.assertEqual(current.legal_moves.count(), 1)
            value = assess(current)
            self.assertEqual(value["complexity"]["clarity"]["kind"], "single_reply")
            self.assertLessEqual(value["recommendation"]["total_ms"], 1000)

    def test_actual_mate_in_one_is_quick_without_returning_a_move_hint(self):
        board = line("f3 e5 g4")
        after = board.copy(); after.push_san("Qh4#")
        self.assertTrue(after.is_checkmate())
        value = assess(board)
        self.assertEqual(value["complexity"]["clarity"]["kind"], "mate_in_one")
        self.assertLessEqual(value["recommendation"]["total_ms"], 2000)
        encoded = json.dumps(value)
        self.assertNotIn("d8h4", encoded)
        self.assertNotIn("best_move", value)

    def test_free_queen_receives_bounded_short_time(self):
        for board in (chess.Board(FREE_QUEEN), chess.Board("5rk1/5ppp/8/3q4/4P3/8/5PPP/5RK1 w - - 0 20")):
            self.assertTrue(board.is_valid())
            value = assess(board)
            self.assertEqual(value["complexity"]["clarity"]["kind"], "clear_queen_gain")
            self.assertLessEqual(value["recommendation"]["total_ms"], 2500)

    def test_queen_trade_with_immediate_recapture_is_not_called_free(self):
        board = chess.Board(RECAPTURE_QUEEN)
        after = board.copy(); after.push_san("Qxd5"); after.push_san("Rxd5")
        self.assertEqual(len(after.pieces(chess.QUEEN, chess.WHITE)), 0)
        for current in (board, board.mirror()):
            value = assess(current)
            self.assertEqual(value["complexity"]["clarity"]["kind"], "unclassified")
            self.assertGreater(value["recommendation"]["total_ms"], 2500)

    def test_legal_trap_poisoned_queen_is_not_given_quick_discount(self):
        board = line("e4 e5 Nf3 Nc6 Bc4 d6 Nc3 Bg4 Nxe5")
        after = board.copy()
        for move in ("Bxd1", "Bxf7+", "Ke7", "Nd5#"):
            after.push_san(move)
        self.assertTrue(after.is_checkmate())
        for current in (board, board.mirror()):
            value = assess(current)
            self.assertEqual(value["complexity"]["clarity"]["kind"], "unclassified")
            self.assertGreater(value["recommendation"]["total_ms"], 2500)

    def test_capped_budget_keeps_fixed_deadline_under_polling(self):
        for board in (chess.Board(), chess.Board(FREE_QUEEN)):
            total = assess(board)["recommendation"]["total_ms"]
            for elapsed in (1, 600, total - 1, total, total + 500):
                value = assess(board, left=600000-elapsed, spent=elapsed)["recommendation"]
                self.assertEqual(value["total_ms"], total)
                self.assertEqual(value["remaining_ms"], max(0, total-elapsed))

    def test_inputs_and_move_history_are_unchanged(self):
        board = line("e4 e5 Nf3 Nc6 Bc4 d6 Nc3 Bg4 Nxe5")
        before = (board.fen(), list(board.move_stack)); timing = clock(board)
        timing_copy = copy.deepcopy(timing)
        support.assess(board, board.turn, timing)
        self.assertEqual((board.fen(), list(board.move_stack)), before)
        self.assertEqual(timing, timing_copy)

    def test_mutating_returned_features_cannot_pollute_cached_position(self):
        board = chess.Board(FREE_QUEEN)
        first = assess(board); expected = copy.deepcopy(first)
        first["complexity"]["features"]["legal_choices"] = -50
        first["complexity"]["clarity"]["kind"] = "corrupt"
        first["complexity"]["reasons"].clear()
        self.assertEqual(assess(board), expected)

    def test_repeated_polling_reuses_tactical_scan(self):
        board = chess.Board(FREE_QUEEN)
        support._position_complexity.cache_clear()
        with mock.patch.object(support, "_clarity", wraps=support._clarity) as scanner:
            for elapsed in range(100):
                assess(board, left=600000-elapsed, spent=elapsed)
            self.assertEqual(scanner.call_count, 1)
        # Cached structure must not cache a previous clock's seconds.
        high = assess(board)["recommendation"]
        low = assess(board, left=1000, increment=10000)["recommendation"]
        self.assertLess(low["total_ms"], high["total_ms"])

    def test_same_quick_budget_drives_engine_and_automatic_search(self):
        for board in (chess.Board(), chess.Board(FREE_QUEEN), line("f3 e5 g4")):
            timing = clock(board); value = assess(board)
            plan = engine_pacing.plan(board, timing)
            self.assertEqual(plan["recommended_total_ms"], value["recommendation"]["total_ms"])
            self.assertLessEqual(plan["target_total_ms"], 2500)
            allocation = engine_pacing.search_budget(plan, timing, 5000, automatic=True)
            self.assertLessEqual(allocation["baseline_ms"], allocation["total_search_ms"])
            self.assertLess(allocation["baseline_ms"], 1000)

    def test_quick_search_floor_never_overrides_tiny_remaining_budget(self):
        board = chess.Board()
        for remaining in (1000, 100, 10, 1):
            timing = clock(board, left=remaining, increment=10000)
            plan = engine_pacing.plan(board, timing)
            allocation = engine_pacing.search_budget(plan, timing, 5000, automatic=True)
            self.assertLessEqual(allocation["baseline_ms"], allocation["total_search_ms"])
            self.assertLessEqual(allocation["baseline_ms"], remaining)
            self.assertLess(allocation["baseline_ms"], 100)
            self.assertEqual(engine_pacing.remaining_wait_ms(plan, timing), 0)


if __name__ == "__main__":
    unittest.main()
