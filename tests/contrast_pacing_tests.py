"""Instant decisions and rare long thought, using authored timing fixtures.

These tests validate budgets and clock safety, not measured human behavior or
the wall-clock latency of the native engine (covered by a separate diagnostic).
"""
from pathlib import Path
import sys
import unittest

import chess
import chess.engine

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import decision_support
import engine_pacing as pacing

FORCED = "8/8/8/8/8/k7/8/Kr6 w - - 0 1"
FREE_QUEEN = "6k1/5ppp/8/8/3q4/8/3R4/5K2 w - - 0 30"


def clock(board, remaining=600000, elapsed=0):
    return {"enabled": True, "status": "running",
            "running_color": "white" if board.turn else "black",
            "white_ms": remaining, "black_ms": remaining,
            "turn_elapsed_ms": elapsed, "increment_ms": 0}


def packets(board, moves):
    return {depth: {"depth": depth, "score": chess.engine.PovScore(chess.engine.Cp(20), board.turn),
                    "pv": [chess.Move.from_uci(move) for move in moves.split()]}
            for depth in (4, 5, 6)}


def deliberation(seed, remaining=600000, basis=10000, elapsed=0):
    board = chess.Board()
    board.push_uci("e2e4")
    board.push_uci("c7c5")
    current = clock(board, remaining, elapsed)
    plan = pacing.plan(board, current, seed=seed)
    # A controlled complexity/budget isolates the cadence decision from the
    # recommendation model. Legal current-position packets remain mandatory.
    plan.update(clarity_kind="unclassified", complexity_score=75, opening_budget=None,
                recommended_total_ms=basis, variation_factor=1.,
                target_total_ms=basis - 1000,
                allocation={"baseline_ms": 600, "total_search_ms": 1800})
    raw = packets(board, "g1f3 d7d6 d2d4")
    result = pacing.refine(board, plan, current, selected_move=chess.Move.from_uci("g1f3"),
                           baseline_packets=raw, selected_info=raw[6], seed=seed)
    return board, plan, result


class ContrastPacingTests(unittest.TestCase):
    def test_single_legal_reply_has_an_initial_100ms_budget(self):
        board = chess.Board(FORCED)
        self.assertEqual(board.legal_moves.count(), 1)
        for seed in range(32):
            current = clock(board)
            plan = pacing.plan(board, current, seed=str(seed))
            budget = pacing.search_budget(plan, current, 5000, automatic=True)
            self.assertEqual(plan["clarity_kind"], "single_reply")
            self.assertEqual(plan["target_total_ms"], 100)
            self.assertEqual(budget["baseline_ms"], 90)
            self.assertEqual(budget["total_search_ms"], 90)
            self.assertEqual(budget["completion_margin_ms"], 10)

    def test_instant_search_budget_accounts_for_time_already_spent(self):
        board = chess.Board(FORCED)
        current = clock(board, elapsed=40)
        plan = pacing.plan(board, current, seed="instant")
        budget = pacing.search_budget(plan, current, 5000, automatic=True)
        self.assertEqual(plan["target_total_ms"], 100)
        self.assertEqual(budget["total_search_ms"], 54)
        self.assertEqual(pacing.remaining_wait_ms(plan, clock(board, elapsed=75)), 25)
        self.assertEqual(pacing.remaining_wait_ms(plan, clock(board, elapsed=150)), 0)

    def test_opening_and_queen_grab_have_both_instant_and_short_checking_turns(self):
        for board in (chess.Board(), chess.Board(FREE_QUEEN)):
            with self.subTest(fen=board.fen()):
                values = [pacing.plan(board, clock(board), seed=str(seed))["target_total_ms"]
                          for seed in range(128)]
                self.assertIn(100, values)
                self.assertGreater(max(values), 500)
                self.assertGreater(len(set(values)), 20)

    def test_human_recommendation_and_normal_targets_keep_the_one_second_reduction(self):
        board = chess.Board()
        current = clock(board)
        shared = decision_support.assess(board, board.turn, current)["recommendation"]
        plan = pacing.plan(board, current)
        self.assertEqual(shared["total_ms"], 2000)
        self.assertEqual(plan["recommended_total_ms"], shared["total_ms"])
        self.assertEqual(plan["target_total_ms"], shared["total_ms"] - 1000)
        self.assertEqual(pacing._shorter_target(20000, 600000), 19000)

    def test_long_thought_is_rare_but_substantially_longer_than_a_normal_turn(self):
        results = [deliberation(str(seed)) for seed in range(128)]
        long = [(board, plan, result) for board, plan, result in results
                if result["refinement"]["kind"] == "long_deliberation"]
        self.assertGreater(len(long), 3)
        self.assertLess(len(long), 26)
        for _board, plan, result in long:
            self.assertGreaterEqual(result["target_total_ms"], 27000)
            self.assertLessEqual(result["target_total_ms"], 33000)
            self.assertGreaterEqual(result["target_total_ms"], 3 * plan["target_total_ms"])
            self.assertEqual(result["allocation"], plan["allocation"])
        ordinary = [result for _board, _plan, result in results
                    if result["refinement"]["kind"] == "ordinary_decision"]
        self.assertTrue(ordinary)
        self.assertTrue(all(result["target_total_ms"] == 9000 for result in ordinary))

    def test_long_thought_reacts_continuously_at_six_minutes_and_shrinks_under_pressure(self):
        seed = next(str(seed) for seed in range(128)
                    if deliberation(str(seed))[2]["refinement"]["kind"] == "long_deliberation")
        results = [deliberation(seed, remaining)[2]
                   for remaining in (600000, 360001, 360000, 359999, 240000, 180000, 120000, 60000, 10000)]
        targets = [result["target_total_ms"] for result in results]
        self.assertEqual(targets, sorted(targets, reverse=True))
        self.assertEqual(targets[0], targets[1])
        self.assertEqual(targets[1], targets[2])
        self.assertLessEqual(targets[2] - targets[3], 1)
        self.assertLess(targets[-2], targets[2])
        self.assertFalse(results[-1]["enabled"])

    def test_completed_capped_long_thought_qualifies_for_cooldown(self):
        row = {"refinement": {"kind": "long_deliberation", "original_target_total_ms": 40000},
               "completion_reason": "target_reached", "spent_before_wait_ms": 3000,
               "waited_ms": 41000, "target_total_ms": 44000}
        self.assertTrue(pacing._completed_long_think(row))
        for update in ({"completion_reason": "client_closing"}, {"waited_ms": 1000},
                       {"completion_reason": "low_clock"}):
            with self.subTest(update=update):
                self.assertFalse(pacing._completed_long_think({**row, **update}))

    def test_long_refinement_preserves_allocations_inputs_and_the_44_second_cap(self):
        seed = next(str(seed) for seed in range(128)
                    if deliberation(str(seed))[2]["refinement"]["kind"] == "long_deliberation")
        board, plan, result = deliberation(seed, basis=25000)
        self.assertEqual(result["target_total_ms"], 44000)
        self.assertEqual(result["allocation"], plan["allocation"])
        self.assertIsNot(result["allocation"], plan["allocation"])
        self.assertEqual(plan["target_total_ms"], 24000)
        self.assertNotIn("refinement", plan)
        self.assertEqual(pacing.remaining_wait_ms(result, clock(board, elapsed=45000)), 0)

    def test_no_selected_evidence_does_not_fabricate_deliberation_or_a_forecast(self):
        board, plan, _result = deliberation("bounded")
        info = packets(board, "g1f3 d7d6")[6]
        info["lowerbound"] = True
        result = pacing.refine(board, plan, clock(board), selected_move=chess.Move.from_uci("g1f3"),
                               selected_info=info, baseline_packets={6: info}, seed="bounded")
        self.assertFalse(result["refinement"]["applied"])
        self.assertIsNone(result["forecast"])
        self.assertEqual(result["target_total_ms"], plan["target_total_ms"])


if __name__ == "__main__":
    unittest.main()
