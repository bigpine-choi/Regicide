"""Independent audit perspective, strict coverage and receipt checks."""
from pathlib import Path
import sys
import unittest

import chess
import chess.engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import audit_sparring_validation as audit


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


if __name__ == "__main__":
    unittest.main()
