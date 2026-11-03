"""Common timing reserves preserve fast replies and bounded policy analysis."""
import copy
import json
from pathlib import Path
import sys
import unittest

import chess
import chess.engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import engine_pacing as pacing
from game_clock import GameClock


class AnalysisReserveTests(unittest.TestCase):
    def setUp(self):
        self.board = chess.Board()
        self.clock = GameClock("10+0", monotonic=lambda: 0).snapshot()

    def at(self, remaining, elapsed=0):
        return {**self.clock, "white_ms": remaining, "turn_elapsed_ms": elapsed}

    def reserve(self, clock=None, minimum=500, optional=1000, **changes):
        clock = clock or self.clock
        plan = pacing.plan(self.board, clock, seed="common-analysis")
        plan.update(changes)
        return pacing.reserve_analysis(plan, clock,
            minimum_baseline_ms=minimum, optional_ms=optional)

    def test_shared_five_minute_boundary_and_compatibility_alias(self):
        self.assertEqual(pacing.PRESSURE_START_MS, 300000)
        self.assertEqual(pacing.SPARRING_PRESSURE_START_MS, pacing.PRESSURE_START_MS)
        at = pacing.plan(self.board, self.at(300000))
        below = pacing.plan(self.board, self.at(299999))
        self.assertEqual(at["pressure_factor"], 1)
        self.assertLess(below["pressure_factor"], 1)
        self.assertLessEqual(at["target_total_ms"] - below["target_total_ms"], 1)
        self.assertAlmostEqual(pacing.plan(self.board, self.at(180000))["pressure_factor"], .8)

    def test_short_opening_budget_reserves_baseline_and_optional_work_once(self):
        original = pacing.plan(self.board, self.clock, seed="common-analysis")
        before = copy.deepcopy(original)
        initial = pacing.search_budget(original, self.clock, 5000, automatic=True)
        self.assertLess(initial["baseline_ms"], 500)
        reserved = pacing.reserve_analysis(original, self.clock,
            minimum_baseline_ms=500, optional_ms=1000)
        self.assertEqual(original, before)
        self.assertTrue(reserved["analysis_reserve"]["active"])
        self.assertEqual(reserved["target_total_ms"], 1650)
        allocation = pacing.search_budget(reserved, self.clock, 5000, automatic=True)
        self.assertEqual(allocation["baseline_ms"], 500)
        self.assertEqual(allocation["optional_ms"], 1000)
        self.assertEqual(allocation["total_search_ms"], 1500)
        self.assertEqual(allocation["completion_margin_ms"], 150)

    def test_existing_long_target_does_not_receive_an_additional_reserve_on_top(self):
        reserved = self.reserve(minimum=600, optional=2000, target_total_ms=12000)
        self.assertEqual(reserved["target_total_ms"], 12000)
        allocation = pacing.search_budget(reserved, self.clock, 100, automatic=True)
        self.assertGreaterEqual(allocation["baseline_ms"], 600)
        self.assertEqual(allocation["optional_ms"], 2000)
        self.assertLessEqual(allocation["baseline_ms"] + allocation["optional_ms"], allocation["total_search_ms"])

    def test_obvious_replies_keep_their_fast_path_instead_of_reserving_analysis(self):
        for clarity in ("single_reply", "mate_in_one", "clear_queen_gain"):
            with self.subTest(clarity=clarity):
                reserved = self.reserve(clarity_kind=clarity, target_total_ms=100)
                self.assertFalse(reserved["analysis_reserve"]["active"])
                self.assertEqual(reserved["analysis_reserve"]["reason"], "obvious_move")
                self.assertEqual(reserved["target_total_ms"], 100)
                budget = pacing.search_budget(reserved, self.clock, 5000, automatic=True)
                self.assertLessEqual(budget["baseline_ms"], 100)
                self.assertEqual(budget["optional_ms"], 0)

    def test_optional_work_fades_quadratically_without_cutting_the_required_reference(self):
        for remaining, expected in ((600000, 1000), (300000, 1000), (299999, 999),
                                    (180000, 360), (60000, 40)):
            with self.subTest(remaining=remaining):
                clock = self.at(remaining)
                reserved = self.reserve(clock)
                self.assertEqual(reserved["analysis_reserve"]["optional_ms"], expected)
                allocation = pacing.search_budget(reserved, clock, 100, automatic=True)
                self.assertGreaterEqual(allocation["baseline_ms"], 500)
                self.assertEqual(allocation["optional_ms"], expected)
                self.assertLessEqual(allocation["baseline_ms"] + expected, allocation["total_search_ms"])

    def test_live_balance_and_completion_margin_take_priority_over_reservation(self):
        no_room = self.reserve(self.at(10649))
        self.assertFalse(no_room["analysis_reserve"]["active"])
        self.assertEqual(no_room["analysis_reserve"]["reason"], "insufficient_baseline_reserve")
        clock = self.at(10650)
        reserved = self.reserve(clock)
        self.assertTrue(reserved["analysis_reserve"]["active"])
        self.assertEqual(reserved["analysis_reserve"]["optional_ms"], 0)
        self.assertEqual(reserved["target_total_ms"], 650)
        allocation = pacing.search_budget(reserved, clock, 1000, automatic=True)
        self.assertEqual(allocation["baseline_ms"], 500)
        self.assertEqual(allocation["total_search_ms"], 500)

    def test_future_increment_never_funds_analysis(self):
        clock = self.at(10650)
        a = self.reserve(clock)
        b = self.reserve({**clock, "increment_ms": 600000})
        for result in (a, b):
            allocation = pacing.search_budget(result, clock, 5000, automatic=True)
            self.assertEqual(allocation["baseline_ms"], 500)
            self.assertEqual(allocation["optional_ms"], 0)
            self.assertEqual(result["target_total_ms"], 650)

    def test_reduced_clock_after_reservation_still_bounds_the_actual_search(self):
        reserved = self.reserve()
        later = self.at(250, 1000)
        allocation = pacing.search_budget(reserved, later, 5000, automatic=True)
        self.assertLessEqual(allocation["total_search_ms"], 250)
        self.assertLess(allocation["baseline_ms"], 500)
        self.assertEqual(allocation["optional_ms"], 0)
        self.assertFalse(allocation["analysis_reserve_active"])

    def test_low_clock_or_paused_clock_cannot_reserve_optional_work(self):
        for remaining in (10000, 1000, 1):
            clock = self.at(remaining)
            reserved = self.reserve(clock)
            self.assertFalse(reserved["analysis_reserve"]["active"])
            self.assertEqual(reserved["analysis_reserve"]["reason"], "low_clock")
            self.assertEqual(pacing.search_budget(reserved, clock, 5000, automatic=True)["optional_ms"], 0)
        paused = {**self.clock, "status": "paused", "running_color": None}
        self.assertFalse(self.reserve(paused)["analysis_reserve"]["active"])

    def test_untimed_research_contract_remains_fixed_and_has_no_optional_cap(self):
        clock = GameClock(None, monotonic=lambda: 0).snapshot()
        reserved = self.reserve(clock)
        self.assertFalse(reserved["enabled"])
        allocation = pacing.search_budget(reserved, clock, 3000, automatic=True)
        self.assertEqual(allocation["baseline_ms"], 3000)
        self.assertIsNone(allocation["total_search_ms"])
        self.assertIsNone(allocation["optional_ms"])

    def test_no_mode_label_changes_the_common_analysis_allocation(self):
        allocations = []
        for mode in ("normal", "gambit", "sparring"):
            reserved = self.reserve(mode=mode)
            allocations.append(pacing.search_budget(reserved, self.clock, 1000, automatic=True))
        self.assertEqual(allocations, [allocations[0]] * 3)

    def test_invalid_requirements_are_rejected_without_nonfinite_json(self):
        for minimum, optional in ((float("nan"), 1000), (500, float("inf")), (-1, 1000), (True, 1000)):
            reserved = self.reserve(minimum=minimum, optional=optional)
            self.assertFalse(reserved["analysis_reserve"]["active"])
            json.dumps(reserved, allow_nan=False)

    def test_completed_fast_choice_does_not_keep_a_reserve_as_artificial_hold(self):
        reserved = self.reserve()
        original = copy.deepcopy(reserved)
        packets = {depth: {"depth": depth,
            "score": chess.engine.PovScore(chess.engine.Cp(20), chess.WHITE),
            "pv": [chess.Move.from_uci(uci) for uci in ("e2e4", "e7e5")]}
            for depth in (4, 5, 6)}
        clock = self.at(599000, 1000)
        refined = pacing.refine(self.board, reserved, clock, selected_move=chess.Move.from_uci("e2e4"),
            baseline_packets=packets, selected_info=packets[6], seed="common-analysis")
        self.assertEqual(reserved, original)
        self.assertEqual(refined["analysis_reserve"], reserved["analysis_reserve"])
        self.assertEqual(pacing.remaining_wait_ms(refined, clock), 0)


if __name__ == "__main__":
    unittest.main()
