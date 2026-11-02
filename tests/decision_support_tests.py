"""Behavioral checks for human difficulty and finite per-turn time budgets."""
import copy
import json
from pathlib import Path
import sys
import time
import unittest

import chess

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import decision_support as support


TACTICAL = "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 12"


def clock(own=600000, other=600000, spent=0, increment=0, color=chess.WHITE):
    return {"enabled": True, "status": "running", "running_color": "white" if color else "black",
            "white_ms": own if color else other, "black_ms": other if color else own,
            "turn_elapsed_ms": spent, "increment_ms": increment,
            "initial_ms": 600000, "control_id": "10+5" if increment else "10+0"}


def evaluation(board, cp=0):
    return {"fen": board.fen(en_passant="fen"), "reliability": "stable", "bound": None,
            "source": "search", "depth": 8, "cp": cp, "mate": None}


class DecisionSupportTests(unittest.TestCase):
    def test_tactical_relations_more_demanding_than_initial_position(self):
        opening = support.assess(chess.Board(), chess.WHITE, clock())
        tactical = support.assess(chess.Board(TACTICAL), chess.WHITE, clock())
        self.assertEqual(opening["status"], "ready")
        self.assertGreater(tactical["complexity"]["score"], opening["complexity"]["score"])
        self.assertGreater(tactical["recommendation"]["total_ms"], opening["recommendation"]["total_ms"])
        self.assertTrue(tactical["complexity"]["reasons"])

    def test_one_legal_reply_is_easy_even_when_checked(self):
        board = chess.Board("8/8/8/8/8/k7/8/Kr6 w - - 0 1")
        self.assertTrue(board.is_valid())
        self.assertEqual(board.legal_moves.count(), 1)
        result = support.assess(board, chess.WHITE, clock())
        self.assertEqual(result["complexity"]["score"], 5)
        self.assertLess(result["recommendation"]["total_ms"], 10000)

    def test_pawn_race_is_not_treated_as_empty_easy_board(self):
        board = chess.Board("8/5k2/8/3P4/p7/8/5K2/8 w - - 0 50")
        result = support.assess(board, chess.WHITE, clock())
        features = result["complexity"]["features"]
        self.assertTrue(features["pawn_ending"])
        self.assertEqual(features["own_advanced_passers"], 1)
        self.assertEqual(features["enemy_advanced_passers"], 1)
        self.assertGreaterEqual(result["complexity"]["score"], 35)
        self.assertTrue(any("통과폰" in x for x in result["complexity"]["reasons"]))

    def test_same_position_polling_consumes_fixed_total_allowance(self):
        board = chess.Board(TACTICAL)
        first = support.assess(board, chess.WHITE, clock())["recommendation"]
        total = first["total_ms"]
        for spent in (1000, 8000, total, total + 30000):
            current = support.assess(board, chess.WHITE, clock(600000-spent, spent=spent))["recommendation"]
            self.assertEqual(current["total_ms"], total)
            self.assertEqual(current["remaining_ms"], max(0, total - spent))
        self.assertEqual(current["urgency"], "budget_used")

    def test_unspent_remaining_never_increases_as_time_passes(self):
        board = chess.Board(TACTICAL)
        for start, inc in ((600000, 0), (60000, 10000), (15000, 5000), (9000, 10000), (500, 10000)):
            values = []
            for elapsed in range(0, start, max(1, start // 30)):
                current = start - elapsed
                r = support.assess(board, chess.WHITE, clock(current, spent=elapsed, increment=inc))["recommendation"]
                self.assertLessEqual(r["remaining_ms"], current)
                self.assertGreaterEqual(r["remaining_ms"], 0)
                values.append(r["remaining_ms"])
            self.assertEqual(values, sorted(values, reverse=True))

    def test_increment_increases_budget_without_spending_unearned_increment(self):
        board = chess.Board(TACTICAL)
        no = support.assess(board, chess.WHITE, clock())["recommendation"]
        inc = support.assess(board, chess.WHITE, clock(increment=10000))["recommendation"]
        self.assertGreater(inc["total_ms"], no["total_ms"])
        tiny = support.assess(board, chess.WHITE, clock(1500, increment=10000))["recommendation"]
        self.assertLessEqual(tiny["remaining_ms"], 1500)
        self.assertEqual(tiny["urgency"], "critical")

    def test_fifteen_minutes_allows_more_than_ten(self):
        board = chess.Board(TACTICAL)
        ten = support.assess(board, chess.WHITE, clock())["recommendation"]
        fifteen = support.assess(board, chess.WHITE, clock(900000, 900000, increment=10000))["recommendation"]
        self.assertGreater(fifteen["total_ms"], ten["total_ms"])
        self.assertLess(fifteen["total_ms"], 90000)

    def test_behind_opponent_clock_reduces_budget_at_turn_start(self):
        board = chess.Board(TACTICAL)
        equal = support.assess(board, chess.WHITE, clock(90000, 90000))["recommendation"]
        behind = support.assess(board, chess.WHITE, clock(90000, 300000))["recommendation"]
        self.assertLess(behind["total_ms"], equal["total_ms"])
        after = support.assess(board, chess.WHITE, clock(85000, 300000, spent=5000))["recommendation"]
        self.assertEqual(after["total_ms"], behind["total_ms"])

    def test_mirrored_color_keeps_same_features_and_time(self):
        board = chess.Board(TACTICAL)
        white = support.assess(board, chess.WHITE, clock(80000, 230000, spent=3000, increment=5000), evaluation(board, 45))
        mirror = board.mirror()
        black = support.assess(mirror, chess.BLACK, clock(80000, 230000, spent=3000, increment=5000, color=chess.BLACK), evaluation(mirror, -45))
        self.assertEqual(white["complexity"], black["complexity"])
        self.assertEqual(white["recommendation"], black["recommendation"])

    def test_winning_losing_and_depth_do_not_define_complexity(self):
        board = chess.Board(TACTICAL)
        outputs = []
        for cp, depth in ((0, 4), (2500, 30), (-2500, 1)):
            ev = evaluation(board, cp); ev["depth"] = depth
            outputs.append(support.assess(board, chess.WHITE, clock(), ev)["complexity"])
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(outputs[1], outputs[2])

    def test_stale_bounded_or_shallow_eval_cannot_adjust_time(self):
        board = chess.Board(TACTICAL)
        plain = support.assess(board, chess.WHITE, clock())["recommendation"]
        good = support.assess(board, chess.WHITE, clock(), evaluation(board))["recommendation"]
        self.assertTrue(good["balanced_evaluation_used"])
        self.assertGreater(good["total_ms"], plain["total_ms"])
        actual_packet = {**evaluation(board), "bound": "exact"}
        self.assertEqual(support.assess(board, chess.WHITE, clock(), actual_packet)["recommendation"], good)
        for change in ({"fen":chess.Board().fen()}, {"bound":"lower"}, {"depth":3}, {"reliability":"volatile"}, {"source":"result"}, {"cp":True}):
            ev = evaluation(board);ev.update(change)
            current = support.assess(board, chess.WHITE, clock(), ev)["recommendation"]
            self.assertEqual(current, plain)

    def test_missing_untimed_or_paused_clock_keeps_complexity_but_no_seconds(self):
        for value in (None, {}, {"enabled":False}, {**clock(), "status":"paused"}):
            result = support.assess(chess.Board(), chess.WHITE, value)
            self.assertEqual(result["status"], "ready")
            self.assertIsInstance(result["complexity"]["score"], int)
            self.assertEqual(result["recommendation"]["status"], "inactive")
            self.assertIsNone(result["recommendation"]["remaining_ms"])

    def test_opponent_turn_terminal_invalid_are_inactive(self):
        board = chess.Board()
        self.assertEqual(support.assess(board, chess.BLACK, clock())["status"], "inactive")
        finished = {**clock(), "status":"finished"}
        self.assertEqual(support.assess(board, chess.WHITE, finished)["status"], "inactive")
        terminal = {**evaluation(board), "status":"terminal"}
        self.assertEqual(support.assess(board, chess.WHITE, clock(), terminal)["status"], "inactive")
        for position in (chess.Board(None), None, chess.Board("7k/6Q1/6K1/8/8/8/8/8 b - - 0 1")):
            self.assertEqual(support.assess(position, chess.WHITE, clock())["status"], "inactive")

    def test_soft_human_timeout_keeps_complexity_without_finite_recommendation(self):
        for color in (chess.WHITE, chess.BLACK):
            board = chess.Board(TACTICAL)
            if not color:
                board = board.mirror()
            c = clock(0, spent=650000, color=color)
            c.update(timeout_policy="engine_only", soft_timeout_color="white" if color else "black")
            result = support.assess(board, color, c)
            self.assertEqual(result["status"], "ready")
            self.assertIsInstance(result["complexity"]["score"], int)
            self.assertEqual(result["recommendation"]["status"], "inactive")
            self.assertIsNone(result["recommendation"]["remaining_ms"])
            self.assertEqual(result["recommendation"]["reason"], "연습 시간이 소진됐지만 계속 둘 수 있습니다.")
            restored = {**c, "white_ms" if color else "black_ms": 5000, "turn_elapsed_ms": 0}
            self.assertEqual(support.assess(board, color, restored)["recommendation"]["status"], "ready")

    def test_zero_time_exception_does_not_apply_to_engine_or_legacy_clock(self):
        board = chess.Board(TACTICAL)
        for policy, soft_color in (("both", "white"), ("engine_only", "black"), ("engine_only", None)):
            c = {**clock(0), "timeout_policy": policy, "soft_timeout_color": soft_color}
            result = support.assess(board, chess.WHITE, c)
            self.assertEqual(result["recommendation"]["status"], "ready")
            self.assertEqual(result["recommendation"]["urgency"], "critical")

    def test_invalid_clock_values_are_json_safe_and_never_recommend_time(self):
        for key in ("white_ms","black_ms","turn_elapsed_ms","increment_ms"):
            for invalid in (float('nan'),float('inf'),True,"60000",None):
                c = clock();c[key] = invalid
                result = support.assess(chess.Board(), chess.WHITE, c)
                self.assertEqual(result["recommendation"]["status"], "inactive")
                json.dumps(result, allow_nan=False)

    def test_inputs_are_unchanged_and_no_move_advice_is_returned(self):
        board = chess.Board()
        for san in "e4 e5 Nf3 Nc6".split():board.push_san(san)
        before = (board.fen(),list(board.move_stack))
        c, ev, rating = clock(), evaluation(board), {"estimate":1200,"observations":50}
        saved = copy.deepcopy((c,ev,rating))
        result = support.assess(board,chess.WHITE,c,ev,rating)
        self.assertEqual(before, (board.fen(),list(board.move_stack)))
        self.assertEqual(saved,(c,ev,rating))
        self.assertFalse(result["recommendation"]["rating_used"])
        self.assertNotIn('best_move',result)
        self.assertNotIn('uci',result)
        json.dumps(result, allow_nan=False)

    def test_recommendations_are_bounded_across_clock_presets_and_late_game(self):
        boards = (chess.Board(), chess.Board(TACTICAL), chess.Board("8/5k2/8/3P4/p7/8/5K2/8 w - - 0 50"))
        for board in boards:
            for own in (0, 1, 10, 500, 1000, 9999, 10000, 30000, 30001, 600000, 900000):
                for inc in (0,5000,10000):
                    r = support.assess(board,chess.WHITE,clock(own,increment=inc))["recommendation"]
                    self.assertGreaterEqual(r["total_ms"],0)
                    self.assertLessEqual(r["remaining_ms"],own)
                    self.assertLessEqual(r["remaining_ms"],r["total_ms"])
                    self.assertLessEqual(r["total_ms"],90000)


if __name__ == "__main__":
    unittest.main()
