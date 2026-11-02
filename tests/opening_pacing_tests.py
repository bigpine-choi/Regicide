"""Opening-time caps on authored positions, independent of human book memory.

These are functional timing/budget checks. They are not playing-strength,
whole-game latency, or measured human behavior comparisons.
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


def board_after(line=""):
    board = chess.Board()
    for san in line.split():
        board.push_san(san)
    return board


def clock(board, left=600000, elapsed=0):
    return {"enabled": True, "status": "running", "running_color": "white" if board.turn else "black",
            "white_ms": left, "black_ms": left, "turn_elapsed_ms": elapsed, "increment_ms": 0}


def packets(board, moves):
    return {depth: {"depth": depth, "score": chess.engine.PovScore(chess.engine.Cp(20), board.turn),
                    "pv": [chess.Move.from_uci(move) for move in moves.split()]}
            for depth in (4, 5, 6)}


def refine(board, plan, moves="b1c3 d7d5", *, seed="audit", left=600000, elapsed=0, raw=None):
    values = raw if raw is not None else packets(board, moves)
    return pacing.refine(board, plan, clock(board, left, elapsed),
                         selected_move=chess.Move.from_uci(moves.split()[0]),
                         selected_info=values.get(6), baseline_packets=values, seed=seed)


class OpeningPacingTests(unittest.TestCase):
    def test_quiet_unlisted_openings_get_a_short_initial_search_budget(self):
        for line in ("Nc3", "Nf3", "e4 c5 Nc3 Nc6 g3 g6 Bg2 Bg7"):
            board = board_after(line)
            with self.subTest(line=line):
                plan = pacing.plan(board, clock(board), seed="audit")
                self.assertEqual(plan["clarity_kind"], "unclassified")
                cap = plan["opening_budget"]["unpressured_cap_ms"]
                self.assertGreaterEqual(cap, 350)
                self.assertLessEqual(cap, 1200)
                self.assertLessEqual(plan["target_total_ms"], cap)
                budget = pacing.search_budget(plan, clock(board), 5000, automatic=True)
                self.assertLessEqual(budget["total_search_ms"], cap)
                self.assertLessEqual(budget["baseline_ms"], budget["total_search_ms"])

    def test_london_and_italian_normal_contacts_do_not_trigger_multi_second_waits(self):
        for line in ("d4 d5 Bf4 Nf6 e3 e6 Nf3 c5",
                     "e4 e5 Nf3 Nc6 Bc4 Nf6 d3 Bc5 O-O O-O"):
            board = board_after(line)
            plan = pacing.plan(board, clock(board), seed="audit")
            with self.subTest(line=line):
                cap = plan["opening_budget"]["unpressured_cap_ms"]
                self.assertGreaterEqual(cap, 900)
                self.assertLessEqual(cap, 2000)
                self.assertLessEqual(plan["target_total_ms"], cap)
                self.assertGreater(plan["recommended_total_ms"], cap)

    def test_human_guidance_is_not_relabelled_as_memorized(self):
        board = board_after("Nf3")
        shared = decision_support.assess(board, board.turn, clock(board))
        before = copy.deepcopy(shared)
        plan = pacing.plan(board, clock(board), seed="audit")
        self.assertEqual(plan["recommended_total_ms"], shared["recommendation"]["total_ms"])
        self.assertEqual(shared["complexity"]["clarity"]["kind"], "unclassified")
        self.assertFalse(shared["complexity"]["clarity"]["familiarity_assumed"])
        self.assertEqual(shared, before)

    def test_actual_off_book_choice_cannot_restore_a_long_wait(self):
        board = chess.Board()
        plan = pacing.plan(board, clock(board), seed="audit")
        plan["allocation"] = pacing.search_budget(plan, clock(board), 5000, automatic=True)
        original = copy.deepcopy(plan)
        result = refine(board, plan)
        self.assertEqual(result["refinement"]["kind"], "unfamiliar_continuation")
        self.assertGreater(result["refinement"]["time_basis_ms"], plan["recommended_total_ms"])
        self.assertLessEqual(result["target_total_ms"], plan["opening_budget"]["unpressured_cap_ms"])
        self.assertEqual(result["allocation"], original["allocation"])
        self.assertEqual(plan, original)
        self.assertEqual(board, chess.Board())

    def test_reconsidering_opening_candidates_does_not_release_the_cap(self):
        board = chess.Board()
        raw = packets(board, "b1c3 d7d5")
        raw[4] = packets(board, "e2e4 e7e5")[4]
        plan = pacing.plan(board, clock(board), seed="audit")
        result = refine(board, plan, raw=raw)
        self.assertTrue(result["refinement"]["search_evidence"]["unsettled"])
        self.assertEqual(result["refinement"]["kind"], "reconsidering_choices")
        self.assertLessEqual(result["target_total_ms"], plan["opening_budget"]["unpressured_cap_ms"])

    def test_a_capped_opening_cannot_gain_an_artificial_long_deliberation(self):
        board = chess.Board()
        for number in range(64):
            seed = str(number)
            plan = pacing.plan(board, clock(board), seed=seed)
            # Force a large complexity score to exercise long-think eligibility
            # independently of the separately recognized development budget.
            plan.update(complexity_score=75, clarity_kind="unclassified")
            result = refine(board, plan, seed=seed)
            self.assertNotEqual(result["refinement"]["kind"], "long_deliberation")
            self.assertLessEqual(result["target_total_ms"], plan["opening_budget"]["unpressured_cap_ms"])

    def test_tactical_warning_thresholds_keep_normal_search_allowance(self):
        board = chess.Board()
        features = pacing.plan(board, clock(board), seed="audit")["position_features"]
        hazards = {"in_check": True, "own_loose_pieces": 1, "enemy_loose_pieces": 1,
                   "overloaded_defenders": 1, "attacked_king_neighbors": 3,
                   "promotion_pawns": 1, "own_advanced_passers": 1, "enemy_advanced_passers": 1,
                   "capture_choices": 4, "checking_choices": 2, "own_pinned_pieces": 2}
        for name, value in hazards.items():
            with self.subTest(hazard=name):
                self.assertIsNone(pacing._opening_budget(board, {**features, name: value}, "audit"))
        for line in ("e4 d5 exd5 Qxd5 Nc3", "e4 e5 Nf3 Nc6 Bb5 d6 Bxc6+"):
            position = board_after(line)
            with self.subTest(line=line):
                self.assertIsNone(pacing.plan(position, clock(position), seed="audit")["opening_budget"])

    def test_cap_requires_development_material_and_ends_after_move_twelve(self):
        board = board_after("Nc3")
        board.fullmove_number = 12
        self.assertIsNotNone(pacing.plan(board, clock(board), seed="audit")["opening_budget"])
        board.fullmove_number = 13
        self.assertIsNone(pacing.plan(board, clock(board), seed="audit")["opening_budget"])
        sparse = chess.Board("6k1/5ppp/8/8/3q4/8/3R4/5K2 w - - 0 1")
        self.assertIsNone(pacing.plan(sparse, clock(sparse), seed="audit")["opening_budget"])
        features = pacing.plan(chess.Board(), clock(chess.Board()), seed="audit")["position_features"]
        for update in ({"nonpawn_pieces": 9}, {"pawns": 11}):
            self.assertIsNone(pacing._opening_budget(chess.Board(), {**features, **update}, "audit"))

    def test_four_minute_pressure_shrinks_the_cap_continuously(self):
        board = board_after("Nf3")
        plans = [pacing.plan(board, clock(board, left), seed="audit")
                 for left in (600000, 240001, 240000, 239999, 180000, 60000)]
        targets = [plan["target_total_ms"] for plan in plans]
        self.assertEqual(targets, sorted(targets, reverse=True))
        self.assertEqual(targets[:3], [targets[0]] * 3)
        self.assertLessEqual(targets[2] - targets[3], 1)
        self.assertLess(targets[-1], targets[0])
        for plan in plans:
            self.assertLessEqual(plan["target_total_ms"],
                                 plan["opening_budget"]["unpressured_cap_ms"] * plan["pressure_factor"])

    def test_urgent_clock_and_already_spent_time_take_precedence(self):
        board = chess.Board()
        plan = pacing.plan(board, clock(board), seed="audit")
        self.assertEqual(pacing.remaining_wait_ms(plan, clock(board, elapsed=2000)), 0)
        urgent = pacing.plan(board, clock(board, 1000), seed="audit")
        self.assertFalse(urgent["enabled"])
        self.assertEqual(pacing.remaining_wait_ms(urgent, clock(board, 1000)), 0)
        budget = pacing.search_budget(urgent, clock(board, 1000), 5000, automatic=True)
        self.assertGreater(budget["total_search_ms"], 1)
        self.assertLess(budget["total_search_ms"], 1000)
        fresh = refine(board, plan, left=10010)
        self.assertLessEqual(fresh["target_total_ms"], 10)

    def test_seed_none_and_untimed_games_keep_existing_contract(self):
        board = chess.Board()
        neutral = pacing.plan(board, clock(board))
        self.assertIsNone(neutral["opening_budget"])
        self.assertEqual(neutral["target_total_ms"], neutral["recommended_total_ms"] - 1000)
        untimed = pacing.plan(board, {"enabled": False}, seed="audit")
        self.assertFalse(untimed["enabled"])
        self.assertEqual(pacing.search_budget(untimed, {"enabled": False}, 5000, automatic=True)["baseline_ms"], 5000)

    def test_seeded_cap_is_reproducible_but_does_not_extend_instant_moves(self):
        board = chess.Board()
        values = [pacing.plan(board, clock(board), seed=str(number)) for number in range(128)]
        self.assertEqual(values[0], pacing.plan(board, clock(board), seed="0"))
        self.assertGreater(len({plan["opening_budget"]["unpressured_cap_ms"] for plan in values}), 20)
        self.assertIn(100, [plan["target_total_ms"] for plan in values])

    def test_missing_selected_evidence_keeps_the_cap_without_inventing_a_forecast(self):
        board = chess.Board()
        plan = pacing.plan(board, clock(board), seed="audit")
        result = refine(board, plan, raw={})
        self.assertIsNone(result["forecast"])
        self.assertFalse(result["refinement"]["applied"])
        self.assertEqual(result["target_total_ms"], plan["target_total_ms"])


if __name__ == "__main__":
    unittest.main()
