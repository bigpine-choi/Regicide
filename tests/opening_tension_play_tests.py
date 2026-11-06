"""Prevent native early advantages drifting away before skill calibration."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import chess

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import sparring_play as policy
import sparring_play_tests as fixtures


HISTORY = ("e4 e5 Nf3 Nc6 Bc4 Bc5 d3 Nf6 Nc3 d6 O-O O-O h3 h6 a3 a6 "
           "Be3 Be6 Qd2 Qd7 Rae1 Rae8 Kh1 Kh8 Nh2 Nh7 f4 f5 exf5 Bxf5 "
           "fxe5 Nxe5 Bb3 c6 d4 Ng6 Bg1 d5 Qd1 Be6").split()


def position(ply):
    board = chess.Board()
    for san in HISTORY[:ply]:
        board.push_san(san)
    return board


class OpeningTensionPlayTests(unittest.TestCase):
    def setUp(self):
        self.pipeline = fixtures.PositionFeedbackTests()

    def run_case(self, ply=8, cp=80, candidate_cp=30, **options):
        board = position(ply)
        if ply == 8:
            baseline, alternative = "h2h3", "b1c3"
        elif ply == 9:
            baseline, alternative = "h7h6", "d7d6"
        else:
            natural = [move.uci() for move in sorted(board.legal_moves, key=lambda m: m.uci())
                       if policy.naturalness(board, move)["admitted"]]
            baseline, alternative = natural[:2]
        result, calls = self.pipeline.run_case(board, baseline, cp, {alternative: candidate_cp}, **options)
        return result, calls, baseline, alternative

    def test_both_colors_search_before_any_profile_observation(self):
        for ply in (8, 9):
            with self.subTest(ply=ply):
                result, calls, baseline, alternative = self.run_case(ply)
                self.assertEqual(result["profile"]["observations"], 0)
                self.assertEqual(result["profile"]["status"], "calibrating")
                self.assertEqual(result["profile"]["skill_target_loss_cp"], 0)
                self.assertEqual(result["selected_uci"], alternative)
                self.assertNotEqual(result["selected_uci"], baseline)
                self.assertTrue(result["postmove_verification"]["valid"])
                self.assertEqual(result["compared_depth"], 5)
                self.assertTrue(any(depth == 3 for _, depth in calls))
                self.assertTrue(any(depth == 5 for _, depth in calls))
                receipt = result["opening_tension"]
                self.assertEqual(receipt["policy"], policy.OPENING_TENSION_POLICY)
                self.assertEqual(receipt["trigger_cp"], 60)
                self.assertEqual(receipt["minimum_progress_cp"], 20)
                self.assertTrue(receipt["requested"])
                self.assertTrue(receipt["active"])
                self.assertTrue(receipt["applied"])
                self.assertEqual(receipt["improvement_cp"], 50)
                self.assertEqual(result["selected_cp_white"], 30 if ply == 8 else -30)
                self.assertLessEqual(result["extra_budget_ms"], 2000)

    def test_preventive_scope_starts_at_ply_eight_and_ends_at_forty(self):
        for ply, expected in ((7, False), (8, True), (39, True), (40, False)):
            with self.subTest(ply=ply):
                result, calls, _, _ = self.run_case(ply)
                self.assertEqual(result["opening_tension"]["requested"], expected)
                self.assertEqual(result["opening_tension"]["active"], expected)
                self.assertEqual(bool(calls), expected)

    def test_trigger_is_strict_and_only_positive_advantage_is_prevented(self):
        for cp, expected in ((-80, False), (0, False), (60, False), (61, True), (100, True)):
            with self.subTest(cp=cp):
                result, calls, baseline, alternative = self.run_case(cp=cp)
                self.assertEqual(result["opening_tension"]["requested"], expected)
                self.assertEqual(result["selected_uci"], alternative if expected else baseline)
                self.assertEqual(bool(calls), expected)

    def test_small_root_or_deeper_improvement_does_not_replace_baseline(self):
        for options in ({"candidate_cp": 61}, {"candidate_cp": 30, "deeper": {"b1c3": 61}}):
            with self.subTest(options=options):
                result, _, baseline, _ = self.run_case(**options)
                self.assertEqual(result["selected_uci"], baseline)
                self.assertFalse(result["opening_tension"]["applied"])
                self.assertTrue(any(p["reason"] == "opening_tension_no_clear_progress" for p in result["probes"]))
        result, _, _, alternative = self.run_case(candidate_cp=60)
        self.assertEqual(result["selected_uci"], alternative)
        self.assertEqual(result["opening_tension"]["improvement_cp"], 20)

    def test_postmove_must_keep_twenty_cp_target_progress(self):
        result, _, baseline, _ = self.run_case(child_cp=61)
        self.assertTrue(result["postmove_verification"]["valid"])
        self.assertEqual(result["reason"], "opening_tension_no_clear_postmove_progress")
        self.assertEqual(result["selected_uci"], baseline)
        self.assertFalse(result["opening_tension"]["applied"])

    def test_existing_material_recovery_delay_keeps_its_target(self):
        paced = {"active": True, "stage": 1, "target_cp": 90, "upper_cp": 130,
                 "allow_material_return": False, "return_cap_cp": 0}
        result, calls, baseline, _ = self.run_case(pacing=paced)
        self.assertTrue(result["opening_tension"]["requested"])
        self.assertFalse(result["opening_tension"]["active"])
        self.assertEqual(result["opening_tension"]["reason"], "pacing_preserved")
        self.assertEqual(result["band_control"]["target_cp"], 90)
        self.assertEqual(result["band_control"]["upper_cp"], 130)
        self.assertEqual(result["selected_uci"], baseline)
        self.assertEqual(calls, [])

    def test_forced_short_and_resource_protection_still_block_prevention(self):
        for options, reason in (({"forced": True}, "user_forced_game"),
                                ({"movetime_ms": 500}, "short_budget")):
            result, calls, baseline, _ = self.run_case(**options)
            self.assertEqual(result["reason"], reason)
            self.assertEqual(result["selected_uci"], baseline)
            self.assertFalse(result["opening_tension"]["active"])
            self.assertEqual(calls, [])
        with patch.object(policy.sparring_pacing, "resource_guard", return_value={"active": True}):
            result, calls, baseline, _ = self.run_case()
        self.assertEqual(result["reason"], "winning_resources_guard")
        self.assertEqual(result["selected_uci"], baseline)
        self.assertEqual(calls, [])

    def test_v6_archive_still_uses_quadratic_release(self):
        board = position(40)
        for version in ("regicide-sparring-play-v6", "regicide-sparring-play-v7"):
            self.assertIn(version, policy.SUPPORTED_POLICIES)
            self.assertAlmostEqual(policy._phase_factor(board, policy=version), (29 / 30) ** 2)
        self.assertAlmostEqual(policy._phase_factor(board, policy="regicide-sparring-play-v5"), 29 / 30)


if __name__ == "__main__":
    unittest.main()
