"""Independent audit perspective, strict coverage and receipt checks."""
from pathlib import Path
import contextlib
import copy
import io
import sys
import unittest
from unittest.mock import patch

import chess
import chess.engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import audit_sparring_validation as audit
from summarize_sparring_match_tests import game as game_fixture, long_sans


class AuditTests(unittest.TestCase):
    def test_black_scores_and_loss_use_sparring_perspective(self):
        before = {"kind": "cp", "cp_white": -200}
        after = {"kind": "cp", "cp_white": -30}
        self.assertEqual(audit.score_loss(before, after, False)["cp_loss"], 170)
        self.assertEqual(audit.score_loss(before, after, True)["cp_loss"], -170)

    def test_mate_and_unknown_never_count_as_balanced(self):
        scores = [{"cp_white": 0}, {"cp_white": 100}, {"cp_white": None, "mate_white": 5}, {"kind": "unknown"}]
        value = audit.close_stats(scores, list(range(4)))
        self.assertEqual((value["close"], value["known_cp"], value["positions"]), (2, 2, 4))
        self.assertEqual(value["fraction_all"], .5)

    def test_unknown_cannot_certify_new_or_lost_mate(self):
        for before, after in (({"kind": "unknown"}, {"kind": "mate", "mate_white": -3}),
                              ({"kind": "mate", "mate_white": 3}, {"kind": "unknown"})):
            result = audit.score_loss(before, after, True)
            self.assertIsNone(result["new_losing_mate"])
            self.assertIsNone(result["lost_winning_mate"])
            self.assertFalse(result["comparison_known"])
        self.assertTrue(audit.score_loss({"kind": "cp", "cp_white": 20},
                                        {"kind": "mate", "mate_white": -3}, True)["new_losing_mate"])

    def test_exact_packet_requires_same_root_and_legal_pv(self):
        board = chess.Board()
        move = chess.Move.from_uci("e2e4")
        info = {"score": chess.engine.PovScore(chess.engine.Cp(20), chess.WHITE), "depth": 8, "pv": [move]}
        self.assertEqual(audit.exact_packet(board, info, move)["cp_white"], 20)
        for changed in ({**info, "lowerbound": True}, {**info, "pv": [chess.Move.from_uci("e2e5")]},
                        {**info, "score": chess.engine.PovScore(chess.engine.Cp(20), chess.BLACK)}):
            self.assertIsNone(audit.exact_packet(board, changed, move))

    def decision(self, value=100):
        return {"selected_uci": "e2e4", "baseline_uci": "d2d4", "baseline": {"cp": 155},
                "candidate_compared": {"cp": value}, "candidate_final": {"cp": value},
                "postmove_verification": {"cp_own": value},
                "ramp_guard": {"floor_cp": 75, "max_loss_cp": 80}}

    def test_native_ramp_compared_final_and_child_are_all_checked(self):
        row = self.decision()
        self.assertTrue(audit.ramp_receipts(row, 30)[0]["compliant"])
        row["postmove_verification"]["cp_own"] = 70
        result = audit.ramp_receipts(row, 30)[0]
        self.assertEqual(result["violations"], ["child"])

    def test_prior_safety_selection_is_not_erased_by_reforged_baseline(self):
        row = {"selected_uci": "e2e4", "baseline_uci": "e2e4", "pre_safety_selection": self.decision(20)}
        values = audit.ramp_receipts(row, 30)
        self.assertEqual(len(values), 1)
        self.assertEqual(values[0]["path"], "final.pre_safety_selection")
        self.assertFalse(values[0]["played_final"])
        self.assertEqual(values[0]["execution"], "superseded_pre_safety")
        self.assertFalse(values[0]["compliant"])

    def test_incomplete_receipt_stays_unknown(self):
        row = self.decision()
        row["ramp_guard"] = {"floor_cp": None, "max_loss_cp": None}
        del row["postmove_verification"]
        result = audit.ramp_receipts(row, 30)[0]
        self.assertTrue(result["unknown"])
        self.assertFalse(result["recorded_guard_matches"])

    def test_quadratic_pressure_receipt_is_checked_independently(self):
        row = self.decision(140)
        row["ramp_guard"] = {"policy": "regicide-sparring-ramp-guard-v2",
                             "floor_cp": 130.888889, "max_loss_cp": 24.111111}
        result = audit.ramp_receipts(row, 30)[0]
        self.assertTrue(result["compliant"])
        self.assertEqual(result["expected_cap_cp"], 24.111111)
        row["postmove_verification"]["cp_own"] = 130
        self.assertEqual(audit.ramp_receipts(row, 30)[0]["violations"], ["child"])
        row["ramp_guard"]["policy"] = "invented-policy"
        self.assertFalse(audit.ramp_receipts(row, 30)[0]["recorded_guard_matches"])

    def test_v7_guard_checks_move_sixteen_without_reinterpreting_legacy_guards(self):
        row = self.decision(372)
        row.update(policy=audit.V7_POLICY, baseline={"cp": 400},
                   ramp_guard={"policy": audit.V7_GUARD, "floor_cp": 371.689796, "max_loss_cp": 28.310204})
        self.assertEqual(audit.ramp_receipts(row, 15), [])
        value = audit.ramp_receipts(row, 16)[0]
        self.assertTrue(value["compliant"])
        self.assertEqual(value["expected_cap_cp"], 28.310204)
        row["postmove_verification"]["cp_own"] = 371
        self.assertEqual(audit.ramp_receipts(row, 16)[0]["violations"], ["child"])
        self.assertEqual(audit.ramp_receipts(row, 50), [])
        old = self.decision()
        old["policy"] = "regicide-sparring-play-v6"
        self.assertEqual(audit.ramp_receipts(old, 16), [])
        self.assertTrue(audit.ramp_receipts(old, 30)[0]["compliant"])
        old["ramp_guard"]["policy"] = audit.V7_GUARD
        self.assertFalse(audit.ramp_receipts(old, 30)[0]["recorded_guard_matches"])
        row["ramp_guard"]["policy"] = "regicide-sparring-ramp-guard-v2"
        self.assertFalse(audit.ramp_receipts(row, 30)[0]["recorded_guard_matches"])

    def test_v7_advantage_fraction_and_referee_scores_use_the_same_independent_floor(self):
        row = self.decision(91)
        guard = {"policy": audit.V7_GUARD, "floor_cp": 90.563265, "max_loss_cp": 9.436735}
        row.update(policy=audit.V7_POLICY, baseline={"cp": 100}, ramp_guard=guard)
        self.assertTrue(audit.ramp_receipts(row, 16)[0]["compliant"])
        row["referee"] = {"applied": True, "accepted": True, "selected_uci": "e2e4",
                          "band": {"baseline_cp_own": 100}, "ramp_guard": copy.deepcopy(guard),
                          "candidates": [{"uci": "e2e4", "cp_own": 91}], "postmove": {"cp_own": 90}}
        value = audit.ramp_receipts(row, 16)[0]
        self.assertEqual(value["source"], "referee")
        self.assertEqual(value["violations"], ["child"])
        row = self.decision(-100)
        row.update(policy=audit.V7_POLICY, baseline={"cp": -100},
                   ramp_guard={"policy": audit.V7_GUARD, "floor_cp": -100, "max_loss_cp": 0})
        self.assertTrue(audit.ramp_receipts(row, 16)[0]["compliant"])

    def test_recorded_policy_ignores_only_marked_historical_continuation_prefix(self):
        game = {"continuation_start_ply": 1, "moves": [
            {"ply": 1, "recorded_prefix": True, "sparring_decision": {"policy": "regicide-sparring-play-v6"}},
            {"ply": 2, "sparring_decision": {"policy": audit.V7_POLICY}}]}
        self.assertEqual(audit.recorded_policy(game), audit.V7_POLICY)
        game["moves"][0]["recorded_prefix"] = False
        with self.assertRaisesRegex(ValueError, "Mixed or unsupported"):
            audit.recorded_policy(game)

    def test_v7_fifteen_move_pass_never_relabels_failed_twenty_move_observation(self):
        source = game_fixture(" ".join(long_sans()[:40]), policy=audit.V7_POLICY)
        board, boards = chess.Board(), [chess.Board()]
        for turn in source["moves"]:
            move = chess.Move.from_uci(turn["uci"])
            turn["san"] = board.san(move)
            board.push(move)
            boards.append(board.copy(stack=True))
        class ControlledAuditor:
            def evaluate(self, position, **_options):
                return {"kind": "cp", "cp_white": 200 if len(position.move_stack) > 30 else 0,
                        "mate_white": None, "bestmove": None}
        def run(value):
            with patch.object(audit, "replay_game", return_value=(value, boards)), \
                 patch.object(audit, "sha", return_value="controlled"), \
                 patch.object(audit, "write"), patch.object(audit, "exposure", return_value=[]), \
                 contextlib.redirect_stdout(io.StringIO()):
                return audit.audit_game(Path("controlled.json"), ControlledAuditor(), Path("unused.json"), 1, 1)
        result = run(source)
        self.assertEqual(result["target_fullmoves"], 15)
        self.assertTrue(result["strict15_pass"])
        self.assertTrue(result["policy_close_pass"])
        self.assertFalse(result["strict20_pass"])
        self.assertEqual(result["early_all_roots"]["positions"], 31)
        self.assertEqual(result["historical_first20_observation"]["all_roots"]["positions"], 41)
        self.assertFalse(result["historical_first20_observation"]["strict_pass"])
        self.assertEqual(result["ramp_own_moves"], 5)
        previous = copy.deepcopy(source)
        for turn in previous["moves"]:
            if turn["sparring_decision"]:
                turn["sparring_decision"]["policy"] = "regicide-sparring-play-v6"
        old = run(previous)
        self.assertEqual(old["target_fullmoves"], 20)
        self.assertFalse(old["policy_close_pass"])
        self.assertEqual(old["early_all_roots"], old["historical_first20_observation"]["all_roots"])
        self.assertEqual(old["ramp_own_moves"], 0)


if __name__ == "__main__":
    unittest.main()
