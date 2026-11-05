"""Shared ramp loss limits; deterministic policy and fake referee evidence."""

from functools import lru_cache
import unittest
from unittest.mock import patch

import chess
import chess.engine

import sparring_play_tests as native_support
import sparring_referee_tests as external_support

native = native_support.policy
referee = external_support.referee


def schedule_board(ply):
    board = chess.Board()
    for index in range(ply):
        board.push_uci(("g1f3", "g8f6", "f3g1", "f6g8")[index % 4])
    return board


@lru_cache(maxsize=None)
def live_board(ply):
    return external_support.long_board(ply)


class NativeRampGuardTests(unittest.TestCase):
    def test_guard_schedule_matches_both_colors_and_finishes_at_fifty(self):
        for fullmove, expected in ((15, None), (16, 28.310204), (30, 9.795918), (40, 2.44898), (49, .02449), (50, 0)):
            for color_offset in (0, 1):
                with self.subTest(fullmove=fullmove, color=color_offset):
                    board = schedule_board((fullmove - 1) * 2 + color_offset)
                    guard = native.ramp_guard(board, 500)
                    self.assertEqual(guard["active"], fullmove >= 16)
                    self.assertEqual(guard["max_loss_cp"], expected)
                    self.assertEqual(guard["floor_cp"], None if expected is None else round(500 - expected, 6))

    def test_positive_advantage_never_crosses_zero_and_losing_never_worsens(self):
        board = schedule_board(58)
        for baseline, floor in ((155, 149.938776), (20, 19.346939), (0, 0), (-30, -30), (-400, -400)):
            with self.subTest(baseline=baseline):
                target = native._target(board, {}, baseline)
                self.assertEqual(target["lower_cp"], floor)
                self.assertGreaterEqual(target["target_cp"], floor)
                self.assertGreaterEqual(target["upper_cp"], target["target_cp"])

    def choose_fixture(self, ply, baseline, compared, final=None, *, legacy_control=False):
        board = schedule_board(ply)
        best, alternative = list(board.legal_moves)[:2]
        control = native._target(board, {}, baseline)
        if legacy_control:
            control.update(lower_cp=-100, target_cp=484.333, upper_cp=486.667)
        probe = {"admitted": True, "uci": alternative.uci(),
                 "candidate_compared": {"cp": compared},
                 "candidate_final": {"cp": compared if final is None else final},
                 "loss_cp": baseline - compared, "naturalness": {"style_points": 0}}
        with patch.object(native, "_candidate_pressure", return_value={"weight": 0, "points": 0}):
            chosen = native._choose_verified(board, best, {"cp": baseline}, [probe], control, "ramp-guard-test")
        return chosen, best, alternative, control

    def test_recorded_move30_style_155_to15_is_rejected(self):
        chosen, best, _, control = self.choose_fixture(58, 155, 15)
        self.assertEqual(control["lower_cp"], 149.938776)
        self.assertEqual(chosen["uci"], best.uci())

    def test_through_fifteen_close_band_choice_is_retained(self):
        chosen, _, alternative, control = self.choose_fixture(28, 155, 15)
        self.assertEqual(control["lower_cp"], -100)
        self.assertEqual(chosen["uci"], alternative.uci())

    def test_small_verified_concession_remains_available(self):
        chosen, _, alternative, _ = self.choose_fixture(30, 155, 145)
        self.assertEqual(chosen["uci"], alternative.uci())

    def test_move16_floor_rejects_a_return_of_more_than_ten_percent(self):
        for ply in (30, 31):
            chosen, best, _, _ = self.choose_fixture(ply, 155, 140)
            self.assertEqual(chosen["uci"], best.uci())

    def test_replayed_admission_cannot_bypass_compared_or_final_floor(self):
        for compared, final in ((149, 150), (150, 149), (-1, -1)):
            chosen, best, _, _ = self.choose_fixture(58, 155, compared, final)
            self.assertEqual(chosen["uci"], best.uci())

    def test_ramp_distance_includes_baseline_even_with_legacy_negative_floor(self):
        # Historical failure: a lone -90 inside [-100,486.667] displaced +500
        # at move49 merely because the baseline sat just outside that band.
        chosen, best, _, _ = self.choose_fixture(96, 500, -90, legacy_control=True)
        self.assertEqual(chosen["uci"], best.uci())

    def test_paced_target_cannot_relax_ramp_floor(self):
        control = native._target(schedule_board(58), {}, 155)
        native._paced_control(control, {"active": True, "target_cp": 15, "upper_cp": 100})
        self.assertEqual(control["lower_cp"], 149.938776)
        self.assertGreaterEqual(control["target_cp"], 149.938776)

    def test_native_postmove_check_uses_the_raised_floor(self):
        board = live_board(58).copy(stack=True)
        candidate = next(move for move in board.legal_moves if not external_support._terminal_child(board, move))
        child = board.copy(stack=True)
        child.push(candidate)
        response = next(iter(child.legal_moves))
        info = native_support.packet(child, response.uci(), -70, 6)
        def search(*args, **kwargs):
            return chess.engine.BestMove(response, None), info, {6: info}
        checked = native._postmove_verification(board, candidate,
            {"candidate_compared": {"cp": 100}, "candidate_final": {"cp": 100}}, search,
            depth=6, budget_ms=300, floor_cp=native.ramp_guard(board, 155)["floor_cp"])
        self.assertFalse(checked["valid"])
        self.assertEqual(checked["reason"], "postmove_below_band_floor")


class RefereeRampGuardTests(unittest.TestCase):
    setUp = external_support.SparringRefereeTests.setUp
    peer = external_support.SparringRefereeTests.peer
    screen = external_support.SparringRefereeTests.screen

    def run_case(self, ply, baseline_cp, candidate_cp, child_cp=None):
        board = live_board(ply).copy(stack=True)
        moves = [move for move in board.legal_moves if not external_support._terminal_child(board, move)][:2]
        original_sign = 1 if board.turn else -1
        def plan(position, limit, options, index):
            allowed = options.get("root_moves")
            if allowed:
                return {"packets": [external_support.packet(position, allowed[0], baseline_cp * original_sign, rank=1),
                    external_support.packet(position, allowed[1], candidate_cp * original_sign, rank=2)], "best": allowed[0]}
            move = next(iter(position.legal_moves))
            cp = candidate_cp if child_cp is None else child_cp
            return {"packets": [external_support.packet(position, move, cp * original_sign)], "best": move}
        service, factory = self.peer(external_support.FakeFactory(plan))
        receipt = self.screen(service, board, moves[0], moves[1])
        return receipt, moves, factory

    def test_move16_both_colors_use_identical_fullmove_fraction_and_floor(self):
        for ply in (30, 31):
            receipt, _, _ = self.run_case(ply, 300, 200)
            self.assertAlmostEqual(receipt["band"]["ramp"], 1 / 35)
            self.assertEqual(receipt["ramp_guard"]["max_loss_cp"], 28.310204)
            self.assertEqual(receipt["band"]["safety_floor_cp_own"], 271.689796)

    def test_excessive_loss_and_sign_reversal_rejected_on_both_sides(self):
        for ply in (58, 59):
            for baseline, candidate in ((155, 15), (20, -1), (-50, -60)):
                receipt, moves, factory = self.run_case(ply, baseline, candidate)
                self.assertEqual(receipt["selected_uci"], moves[0].uci())
                self.assertFalse(receipt["accepted"])
                self.assertEqual(len(factory.searches), 1)

    def test_small_loss_still_accepted_and_child_below_floor_rejected(self):
        receipt, moves, _ = self.run_case(30, 155, 145)
        self.assertTrue(receipt["accepted"])
        self.assertEqual(receipt["selected_uci"], moves[1].uci())
        receipt, moves, _ = self.run_case(30, 155, 145, child_cp=140)
        self.assertFalse(receipt["accepted"])
        self.assertEqual(receipt["reason"], "postmove_outside_reference_band")
        self.assertEqual(receipt["selected_uci"], moves[0].uci())

    def test_through_fifteen_reference_behavior_unchanged(self):
        receipt, moves, _ = self.run_case(28, 155, 15)
        self.assertTrue(receipt["accepted"])
        self.assertEqual(receipt["selected_uci"], moves[1].uci())
        self.assertFalse(receipt["ramp_guard"]["active"])

    def test_move_fifty_keeps_baseline_without_external_search(self):
        receipt, moves, factory = self.run_case(98, 155, 150)
        self.assertEqual(receipt["selected_uci"], moves[0].uci())
        self.assertEqual(receipt["reason"], "full_strength_regicide_baseline")
        self.assertEqual(factory.searches, [])


if __name__ == "__main__":
    unittest.main()
