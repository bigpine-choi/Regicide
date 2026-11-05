"""Recovery-only budgets and safe retries; deterministic evidence, no engines."""
import unittest
from unittest.mock import patch

import chess

import sparring_play_tests as fixtures
import sparring_pacing as pacing

policy = fixtures.policy


class RecoveryPolicyTests(unittest.TestCase):
    run_case = fixtures.PositionFeedbackTests.run_case
    archived_position = fixtures.PositionFeedbackTests.archived_position

    def test_explicit_recovery_works_below_ordinary_baseline_minimum(self):
        board = self.archived_position()
        ordinary, _ = self.run_case(board, "d4e5", 203, {"c1e3": 40}, movetime_ms=100)
        recovered, _ = self.run_case(board, "d4e5", 203, {"c1e3": 40},
                                     movetime_ms=100, recovery_budget_ms=8000)
        self.assertEqual(ordinary["reason"], "short_budget")
        self.assertEqual(recovered["selected_uci"], "c1e3")
        self.assertTrue(recovered["band_control"]["recovered"])
        self.assertTrue(recovered["recovery"]["enabled"])
        self.assertEqual(recovered["extra_budget_ms"], 8000)
        self.assertGreater(len(recovered["discovery"]), policy.MAX_DISCOVERY)
        self.assertGreater(len(recovered["probes"]), policy.MAX_PROBES)
        self.assertLessEqual(recovered["quality_budget_ms"], 500)
        self.assertEqual(recovered["compared_depth"], 5)

    def test_failed_best_postmove_candidate_tries_next_verified_candidate(self):
        board = self.archived_position()
        result, _ = self.run_case(board, "d4e5", 400, {"d1d3": 30, "c1e3": 40},
            child_values={"d1d3": -150, "c1e3": 45}, recovery_budget_ms=8000)
        self.assertEqual(result["selected_uci"], "c1e3")
        self.assertEqual(result["recovery"]["postmove_retries"], 1)
        self.assertEqual([row["candidate_uci"] for row in result["postmove_attempts"]], ["d1d3", "c1e3"])
        self.assertEqual(result["postmove_attempts"][0]["reason"], "postmove_below_band_floor")
        self.assertTrue(result["postmove_verification"]["valid"])
        self.assertEqual(result["postmove_verification"]["cp_own"], 45)

    def test_every_candidate_failing_still_preserves_original_baseline(self):
        result, _ = self.run_case(self.archived_position(), "d4e5", 400,
            {"d1d3": 30, "c1e3": 40}, child_cp=-150, recovery_budget_ms=8000)
        self.assertEqual(result["selected_uci"], "d4e5")
        self.assertEqual(result["reason"], "postmove_below_band_floor")
        self.assertFalse(result["band_control"]["recovered"])
        self.assertGreaterEqual(len(result["postmove_attempts"]), 2)
        self.assertLessEqual(len(result["postmove_attempts"]), policy.RECOVERY_PROBES)
        self.assertTrue(all(not row["valid"] for row in result["postmove_attempts"]))

    def test_retry_searches_fit_total_allowance_when_every_request_uses_its_budget(self):
        clock = [0.0]
        with patch.object(policy.time, "monotonic", side_effect=lambda: clock[0]):
            result, _ = self.run_case(self.archived_position(), "d4e5", 400,
                {"d1d3": 30, "c1e3": 40}, child_values={"d1d3": -150, "c1e3": 45},
                recovery_budget_ms=8000, clock=clock)
        self.assertEqual(result["selected_uci"], "c1e3")
        self.assertEqual(len(result["postmove_attempts"]), 2)
        self.assertLessEqual(result["extra_elapsed_ms"], 8000.001)
        self.assertGreater(result["postmove_reserved_ms"], policy.POSTMOVE_MAX_MS)
        self.assertTrue(all(row["required_depth"] >= 4 for row in result["postmove_attempts"]))

    def test_budget_is_bounded_and_rejects_invalid_types(self):
        result, _ = self.run_case(self.archived_position(), "d4e5", 203,
                                  {"c1e3": 40}, recovery_budget_ms=999999)
        self.assertEqual(result["extra_budget_ms"], policy.MAX_RECOVERY_EXTRA_MS)
        for budget in (-1, True, 2.5, float("inf"), "8000"):
            with self.subTest(budget=budget), self.assertRaises(ValueError):
                self.run_case(self.archived_position(), "d4e5", 203, {}, recovery_budget_ms=budget)

    def test_first_exact_probes_get_depth_budget_instead_of_ten_equal_slices(self):
        clock = [0.0]
        with patch.object(policy.time, "monotonic", side_effect=lambda: clock[0]):
            result, _ = self.run_case(self.archived_position(), "d4e5", 400,
                {"d1d3": 30, "c1e3": 40}, recovery_budget_ms=8000, clock=clock)
        budgets = [probe["budget_ms"] for probe in result["probes"]]
        self.assertGreaterEqual(len(budgets), 5)
        self.assertGreater(budgets[0], 2 * sum(budgets) / len(budgets))
        self.assertGreater(budgets[0], budgets[-1])
        self.assertTrue(all(probe["compared_depth"] == 5 for probe in result["probes"]))
        self.assertEqual(result["selected_uci"], "d1d3")
        self.assertLessEqual(result["extra_elapsed_ms"], 8000.001)

    def test_in_band_and_losing_positions_do_not_open_concession_recovery(self):
        for score in (100, 30, -100, -101, -400):
            result, _ = self.run_case(self.archived_position(), "d4e5", score,
                                      {"c1e3": 20}, recovery_budget_ms=8000)
            self.assertFalse(result["recovery"]["enabled"], score)
            self.assertEqual(result["selected_uci"], "d4e5")
            if score < -100:
                self.assertEqual(result["reason"], "position_below_band_strongest")

    def test_shallow_reference_or_changed_exact_reference_does_not_gain_trust(self):
        shallow, _ = self.run_case(self.archived_position(), "d4e5", 203,
                                   {"c1e3": 40}, depth=3, recovery_budget_ms=8000)
        self.assertFalse(shallow["recovery"]["enabled"])
        self.assertEqual(shallow["selected_uci"], "d4e5")
        changed, _ = self.run_case(self.archived_position(), "d4e5", 203,
            {"c1e3": 40}, reference_cp=400, stable=False, recovery_budget_ms=8000)
        self.assertEqual(changed["reason"], "reference_revalidation_failed")
        self.assertEqual(changed["selected_uci"], "d4e5")

    def test_tiny_allowance_cannot_skip_required_postmove_evidence(self):
        result, calls = self.run_case(self.archived_position(), "d4e5", 203,
            {"c1e3": 40}, movetime_ms=100, recovery_budget_ms=25)
        self.assertEqual(result["reason"], "postmove_verification_budget")
        self.assertEqual(result["selected_uci"], "d4e5")
        self.assertEqual(calls, [])
        self.assertEqual(result["postmove_attempts"], [])

    def test_winning_mate_recovery_keeps_mate_evidence_and_verifies_finite_depth_four(self):
        board = chess.Board()
        for san in "f3 e5 g4".split():
            board.push_san(san)
        result, _ = self.run_case(board, "d8h4", None, {"d7d5": 35}, depth=1,
            mate=(1, ["d8h4"]), movetime_ms=100, recovery_budget_ms=8000)
        self.assertTrue(result["recovery"]["enabled"])
        self.assertEqual(result["selected_uci"], "d7d5")
        self.assertIsNone(result["loss_cp"])
        self.assertEqual(result["baseline"]["mate"], 1)
        self.assertEqual(result["candidate_compared"]["depth"], 4)
        self.assertGreaterEqual(result["postmove_verification"]["required_depth"], 4)

    def test_explicit_forced_recovery_retains_provenance_and_never_observes_rating(self):
        board = self.archived_position()
        original = board.copy(stack=True)
        ordinary, _ = self.run_case(board, "d4e5", 203, {"c1e3": 40}, forced=True)
        self.assertEqual(ordinary["reason"], "user_forced_game")
        result, _ = self.run_case(board, "d4e5", 203, {"c1e3": 40},
            forced=True, actual_observation=True, movetime_ms=100, recovery_budget_ms=8000)
        self.assertEqual(result["selected_uci"], "c1e3")
        self.assertTrue(result["recovery"]["forced_recovery"])
        self.assertTrue(result["recovery"]["forced_provenance"])
        self.assertFalse(result["opponent_observation"]["valid"])
        self.assertTrue(result["opponent_observation"]["forced"])
        self.assertEqual(result["opponent_observation"]["reason"], "forced_game")
        self.assertEqual(result["profile"]["observations"], 0)
        self.assertEqual(board.fen(), original.fen())
        self.assertEqual(board.move_stack, original.move_stack)

    def test_custom_root_and_resource_guard_are_not_bypassed(self):
        board = chess.Board(self.archived_position().fen())
        result, _ = self.run_case(board, "d4e5", 203, {"c1e3": 40},
                                  forced=True, recovery_budget_ms=8000)
        self.assertFalse(result["recovery"]["enabled"])
        self.assertEqual(result["selected_uci"], "d4e5")
        with patch.object(policy.sparring_pacing, "resource_guard", return_value={"active": True}):
            result, _ = self.run_case(self.archived_position(), "d4e5", 203,
                                      {"c1e3": 40}, recovery_budget_ms=8000)
        self.assertFalse(result["recovery"]["enabled"])
        self.assertEqual(result["reason"], "winning_resources_guard")

    def test_explicit_recovery_stops_at_sixteen_and_v5_replay_keeps_twenty(self):
        from summarize_sparring_match_tests import long_sans
        sans = long_sans()
        for length in (28, 29, 30, 31, 39, 40, 41, 97, 98, 99):
            board = chess.Board()
            for san in sans[:length]:
                board.push_san(san)
            if 30 <= length < 40:
                self.assertEqual(policy.phase(board), "ramp")
                self.assertEqual(policy.phase(board, policy="regicide-sparring-play-v5"), "sparring")
            else:
                self.assertEqual(policy.phase(board), policy.phase(board, policy="regicide-sparring-play-v5"))
            result, _ = self.run_case(board, next(iter(board.legal_moves)).uci(), 400,
                                      {}, recovery_budget_ms=8000)
            self.assertEqual(result["recovery"]["enabled"], length < 30)


class PersistentRecoveryTests(unittest.TestCase):
    def board_at(self, root_ply):
        board = chess.Board()
        for san in "e4 e5 Qh5 Nc6 Qxf7+ Kxf7 Nc3 Nf6 Nf3 Be7 Be2 Rf8 O-O Kg8 d3".split():
            board.push_san(san)
        if root_ply < 15:
            while len(board.move_stack) > root_ply:
                board.pop()
        else:
            while len(board.move_stack) < root_ply:
                board.push_uci(("f6h5", "c3b1", "h5f6", "b1c3")[(len(board.move_stack) - 15) % 4])
        return board

    def test_first_two_turns_stay_concealed_then_failed_recovery_can_continue(self):
        for root, expected_stage in ((7, 1), (9, 2), (11, 3), (13, 4), (15, 4), (29, 4)):
            plan = pacing.derive(self.board_at(root), [], 800)
            self.assertTrue(plan["active"], plan)
            self.assertEqual(plan["stage"], expected_stage)
            self.assertEqual(plan["allow_material_return"], root >= 11)
            self.assertEqual(plan["persistent"], root >= 15)
            if root >= 13:
                self.assertEqual((plan["target_cp"], plan["upper_cp"]), (30, 100))
        legacy = pacing.derive(self.board_at(15), [], 800, persist_recovery=False)
        self.assertEqual(legacy["reason"], "recovery_window_finished")
        ended = pacing.derive(self.board_at(31), [], 800)
        self.assertEqual(ended["reason"], "close_phase_finished")

    def test_active_material_return_stops_at_move16_for_both_colors(self):
        for root in (29, 30, 31, 39, 41, 97):
            original = self.board_at(root)
            mirrored = original.root().mirror()
            for move in original.move_stack:
                mirrored.push(chess.Move(chess.square_mirror(move.from_square),
                                         chess.square_mirror(move.to_square)))
            for board in (original, mirrored):
                before = (board.fen(), list(board.move_stack))
                plan = pacing.derive(board, [], 800)
                if root == 29:
                    self.assertTrue(plan["active"])
                    self.assertTrue(plan["persistent"])
                    self.assertTrue(plan["allow_material_return"])
                else:
                    self.assertEqual(plan["reason"], "close_phase_finished")
                    self.assertFalse(plan["active"])
                    self.assertFalse(plan["allow_material_return"])
                    self.assertEqual(plan["return_cap_cp"], 0)
                self.assertEqual((board.fen(), board.move_stack), before)

    def test_strict_band_stops_debt_and_color_mirror_has_same_schedule(self):
        original = self.board_at(15)
        for cp in (-100, 0, 100):
            self.assertEqual(pacing.derive(original, [], cp)["reason"], "strict_band_recovered")
        mirrored = original.root().mirror()
        for move in original.move_stack:
            mirrored.push(chess.Move(chess.square_mirror(move.from_square), chess.square_mirror(move.to_square)))
        white = pacing.derive(mirrored, [], 800)
        black = pacing.derive(original, [], 800)
        for key in ("stage", "elapsed_stage", "persistent", "target_cp", "upper_cp", "debt_cp", "allow_material_return"):
            self.assertEqual(white[key], black[key], key)


if __name__ == "__main__":
    unittest.main()
