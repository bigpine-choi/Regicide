"""Retained v4 reduction/pressure contract with the v5 instant-move floor.

Authored positions and deterministic packets, not a measured human cadence.
Golden rhythm values were read from pinned v3 commit 79d0587; these unit tests
do not require Git history, an engine executable, or personal game records.
"""
import copy
from pathlib import Path
import sys
import unittest

import chess
import chess.engine

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import decision_support
import engine_pacing as pacing

SEED = "pacing-v4-golden"
TACTICAL = "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 12"


def clock(board, left=600000, elapsed=0):
    return {"enabled": True, "status": "running", "running_color": "white" if board.turn else "black",
            "white_ms": left, "black_ms": left, "turn_elapsed_ms": elapsed, "increment_ms": 0}


def packet(board, moves, depth=6, cp=20):
    return {"depth": depth, "score": chess.engine.PovScore(chess.engine.Cp(cp), board.turn),
            "pv": [chess.Move.from_uci(move) for move in moves.split()]}


def refine(board, plan, current_clock, moves="e2e4 e7e5 g1f3", packets=None):
    selected = chess.Move.from_uci(moves.split()[0])
    packets = packets or {depth: packet(board, moves, depth) for depth in (4, 5, 6)}
    return pacing.refine(board, plan, current_clock, selected_move=selected,
                         selected_info=packet(board, moves), baseline_packets=packets, seed=SEED)


class PacingV4Tests(unittest.TestCase):
    def test_version_and_pressure_contract(self):
        self.assertEqual(pacing.POLICY, "regicide-engine-pacing-v6")
        self.assertEqual(pacing.PRESSURE_START_MS, 240000)
        self.assertEqual(pacing.TURN_REDUCTION_MS, 1000)
        self.assertEqual(pacing.MIN_TURN_TARGET_MS, 100)

    def test_one_second_adjustment_has_a_quick_move_floor_without_lengthening(self):
        cases = ((0, 0), (99, 99), (100, 100), (249, 100), (250, 100), (600, 100),
                 (1099, 100), (1100, 100), (1249, 249), (1250, 250),
                 (2000, 1000), (9000, 8000))
        for original, expected in cases:
            with self.subTest(original=original):
                self.assertEqual(pacing._shorter_target(original, 600000), expected)
                self.assertLessEqual(expected, original)

    def test_maximum_target_is_shortened_before_clock_capping(self):
        self.assertEqual(pacing._shorter_target(45000, 600000), 44000)
        self.assertEqual(pacing._shorter_target(90000, 600000), 44000)

    def test_urgent_search_target_is_not_collapsed_by_presentation_reduction(self):
        for remaining in (10000, 1000, 1):
            self.assertEqual(pacing._shorter_target(900, remaining), 900)
        board = chess.Board()
        plan = pacing.plan(board, clock(board, 1000), seed=SEED)
        budget = pacing.search_budget(plan, clock(board, 1000), 5000, automatic=True)
        self.assertGreater(budget["total_search_ms"], 1)
        self.assertLess(budget["total_search_ms"], 1000)
        self.assertEqual(pacing.remaining_wait_ms(plan, clock(board, 1000)), 0)

    def test_human_recommendation_is_unchanged_while_engine_target_is_shorter(self):
        board = chess.Board()
        current = clock(board)
        support = decision_support.assess(board, board.turn, current)["recommendation"]
        plan = pacing.plan(board, current)
        self.assertEqual(support["total_ms"], 2000)
        self.assertEqual(plan["recommended_total_ms"], support["total_ms"])
        self.assertEqual(plan["target_total_ms"], 1000)

    def test_same_seed_preserves_v3_rhythm_golden_values(self):
        examples = ((chess.Board(), .6336934997340373, .9985792697491473, 267),
                    (chess.Board(TACTICAL), 1.1668724591249813, .9985792697491473, 17134))
        for board, variation, beat, target in examples:
            plan = pacing.plan(board, clock(board), seed=SEED)
            self.assertAlmostEqual(plan["game_tempo_factor"], .9904642711465494, places=14)
            self.assertAlmostEqual(plan["variation_factor"], variation, places=14)
            self.assertAlmostEqual(plan["local_beat_factor"], beat, places=14)
            self.assertEqual(plan["target_total_ms"], target)

    def test_four_minute_boundary_is_continuous_and_pressure_is_monotonic(self):
        board = chess.Board(TACTICAL)
        remaining = (600000, 240001, 240000, 239999, 180000, 120000, 60000)
        plans = [pacing.plan(board, clock(board, left), seed=SEED) for left in remaining]
        self.assertEqual([p["pressure_factor"] for p in plans[:3]], [1., 1., 1.])
        self.assertLess(plans[3]["pressure_factor"], 1.)
        self.assertLessEqual(plans[2]["target_total_ms"] - plans[3]["target_total_ms"], 1)
        self.assertEqual(plans[4]["pressure_factor"], .875)
        self.assertEqual(plans[5]["pressure_factor"], .75)
        self.assertEqual(plans[6]["pressure_factor"], .625)
        targets = [p["target_total_ms"] for p in plans]
        self.assertEqual(targets, sorted(targets, reverse=True))

    def test_refinement_does_not_subtract_the_second_twice(self):
        board = chess.Board()
        board.push_uci("e2e4"); board.push_uci("e7e5")
        plan = pacing.plan(board, clock(board), seed=SEED)
        self.assertEqual(plan["target_total_ms"], 509)  # v3 golden target was 1509 ms.
        result = refine(board, plan, clock(board), "g1f3 b8c6 f1b5")
        self.assertTrue(result["refinement"]["applied"])
        self.assertEqual(result["refinement"]["kind"], "quick_familiar_opening")
        self.assertEqual(result["target_total_ms"], 509)
        self.assertEqual(plan["target_total_ms"], 509)

    def test_missing_selected_evidence_keeps_the_already_adjusted_plan(self):
        board = chess.Board()
        plan = pacing.plan(board, clock(board), seed=SEED)
        result = pacing.refine(board, plan, clock(board), selected_move=chess.Move.from_uci("e2e4"), seed=SEED)
        self.assertEqual(result["target_total_ms"], plan["target_total_ms"])
        self.assertFalse(result["refinement"]["applied"])

    def test_fresh_clock_caps_take_precedence_over_the_quick_floor(self):
        board = chess.Board()
        plan = pacing.plan(board, clock(board), seed=SEED)
        result = refine(board, plan, clock(board, 10010))
        self.assertLessEqual(result["target_total_ms"], 10)
        self.assertLessEqual(pacing.remaining_wait_ms(result, clock(board, 10010)), 10)

    def test_elapsed_time_and_search_allocation_are_not_added_or_rewritten(self):
        board = chess.Board()
        plan = pacing.plan(board, clock(board), seed=SEED)
        plan["allocation"] = pacing.search_budget(plan, clock(board), 5000, automatic=True)
        original = copy.deepcopy(plan)
        result = refine(board, plan, clock(board, 599000, 1000))
        self.assertEqual(result["allocation"], original["allocation"])
        self.assertEqual(plan, original)
        self.assertEqual(pacing.remaining_wait_ms(result, clock(board, 599000, 1000)), 0)

    def test_reconsideration_premium_also_uses_four_minute_pressure(self):
        board = chess.Board()
        packets = {4: packet(board, "d2d4 d7d5", 4), 5: packet(board, "e2e4 e7e5", 5),
                   6: packet(board, "g1f3 d7d5", 6)}
        plan = pacing.plan(board, clock(board), seed=SEED)
        result = refine(board, plan, clock(board, 180000), "g1f3 d7d5", packets)
        detail = result["refinement"]
        self.assertEqual(detail["kind"], "reconsidering_choices")
        self.assertEqual(detail["premium_pressure_fraction"], .75)
        self.assertAlmostEqual(detail["factor"], 1 + .75 * (detail["unpressured_factor"] - 1), places=5)

    def test_untimed_game_keeps_manual_search_budget(self):
        board = chess.Board()
        state = {"enabled": False}
        plan = pacing.plan(board, state, seed=SEED)
        self.assertFalse(plan["enabled"])
        self.assertEqual(pacing.search_budget(plan, state, 2500, automatic=True)["baseline_ms"], 2500)


if __name__ == "__main__":
    unittest.main()
