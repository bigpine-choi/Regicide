"""Acceptance accounting uses actual final choices and a shared per-root audit."""
import copy
import unittest

import gambit_creativity_validation as validation


def specimen():
    cases = [{"id": "root-seed" + str(seed), "root_id": "root", "seed_index": seed,
              "history_uci": [], "before_fen": validation.chess.STARTING_FEN, "color": "white"}
             for seed in range(1, 4)]
    rows = []
    for case in cases:
        creative = case["seed_index"] == 2
        move = "d2d4" if creative else "e2e4"
        evidence = {"motif": "pawn_break", "retained_after_reply": True,
                    "retained_motifs": {"pawn_break": 1}, "operations_evidence": {"settled": True},
                    "candidate_compared": {"depth": 6, "cp": 30, "pv_uci": [move, "d7d5"]},
                    "candidate_final": {"depth": 6, "cp": 30, "pv_uci": [move, "d7d5"]}, "loss_cp": 0}
        rows.append({"fixture": case, "arms": {
            "baseline": {"selected_uci": "e2e4", "receipt": {"selected_uci": "e2e4", "baseline_uci": "e2e4"}},
            "candidate": {"selected_uci": move, "fixture": case,
                "receipt": {"selected_uci": move, "baseline_uci": "e2e4",
                "creativity": {"applied": creative, "selected_uci": move, "evidence": evidence, "loss_cp": 0,
                    "baseline": {"depth": 6, "cp": 10, "pv_uci": ["e2e4", "e7e5"]}}}}}})
    report = {"passed": True, "rows": rows, "root_audits": {"root": {
        "status": "common_exact_depth", "depth": 18, "loss_cp": {"e2e4": 20, "d2d4": 0},
        "scores": {"e2e4": {"cp_root": 10, "mate_root": None},
                   "d2d4": {"cp_root": 30, "mate_root": None}}}}}
    return report, {"cases": cases}


def paired_specimen():
    report, fixture = specimen()
    detail = report["rows"][1]["arms"]["candidate"]["receipt"]["creativity"]
    evidence = detail["evidence"]
    detail.update(comparison_kind="paired_unrestricted_children", child_depth=5, root_loss_cp=6, paired_loss_cp=0)
    evidence.update(comparison_kind="paired_unrestricted_children", child_depth=5, root_loss_cp=6, paired_loss_cp=0)
    evidence["candidate_compared"]["cp"] = evidence["candidate_final"]["cp"] = 4
    for name, move, child_cp, pv in (("baseline", "e2e4", -10, ["e7e5", "g1f3"]),
                                    ("candidate", "d2d4", -12, ["d7d5", "b1c3"])):
        child = validation.chess.Board()
        child.push_uci(move)
        line = {"depth": 5, "cp": child_cp, "pv_uci": pv}
        evidence["paired_child_" + name] = {"fen": child.fen(en_passant="fen"), "history_uci": [move],
            "returned_uci": pv[0], "common": copy.deepcopy(line), "final": copy.deepcopy(line), "budget_ms": 100}
        parent_line = {"depth": 6, "cp": -child_cp, "pv_uci": [move, *pv], "child_depth": 5,
                       "depth_basis": "fixed_root_ply_plus_completed_child_depth"}
        evidence["baseline_compared" if name == "baseline" else "selection_compared"] = parent_line
        if name == "candidate":
            evidence["selection_final"] = copy.deepcopy(parent_line)
    return report, fixture


class CreativityValidationTests(unittest.TestCase):
    def test_accepted_sample_requires_quality_actual_variation_and_retained_motif(self):
        report, fixture = specimen()
        result = validation.summarize(report, fixture)
        self.assertTrue(result["sample_acceptance_passed"])
        self.assertEqual(result["changed_pairs"], 1)
        self.assertEqual(result["changed_baseline_loss_sum_cp"], 20)
        self.assertEqual(result["changed_candidate_loss_sum_cp"], 0)
        self.assertEqual(result["actually_selected_creativity"], 1)

    def test_final_safety_override_cannot_count_an_unplayed_creative_move(self):
        report, fixture = specimen()
        arm = report["rows"][1]["arms"]["candidate"]
        arm["selected_uci"] = arm["receipt"]["selected_uci"] = "e2e4"
        result = validation.summarize(report, fixture)
        self.assertFalse(result["sample_acceptance_passed"])
        self.assertEqual(result["actually_selected_creativity"], 0)
        self.assertEqual(result["selected_reply_retained_motifs"], 0)

    def test_budget_drift_is_not_attributed_to_creative_seed_selection(self):
        report, fixture = specimen()
        report["rows"][1]["arms"]["candidate"]["receipt"]["baseline_uci"] = "d2d4"
        result = validation.summarize(report, fixture)
        self.assertTrue(result["gates"]["at_least_one_root_with_increased_distinct_moves"])
        self.assertFalse(result["gates"]["at_least_one_stable_native_root_with_creative_variety"])
        self.assertFalse(result["sample_acceptance_passed"])

    def test_a_different_positive_motif_does_not_prove_the_selected_motif_survives(self):
        report, fixture = specimen()
        report["rows"][1]["arms"]["candidate"]["receipt"]["creativity"]["evidence"]["retained_motifs"] = {"rook_activity": 2}
        result = validation.summarize(report, fixture)
        self.assertEqual(result["actually_selected_creativity"], 1)
        self.assertEqual(result["selected_reply_retained_motifs"], 0)
        self.assertFalse(result["sample_acceptance_passed"])

    def test_new_noncreative_move_cannot_borrow_attribution_from_an_existing_creative_move(self):
        report, fixture = specimen()
        row = report["rows"][2]
        row["arms"]["baseline"]["selected_uci"] = "d2d4"
        row["arms"]["baseline"]["receipt"]["selected_uci"] = "d2d4"
        row["arms"]["candidate"]["selected_uci"] = "c2c4"
        row["arms"]["candidate"]["receipt"]["selected_uci"] = "c2c4"
        audit = report["root_audits"]["root"]
        audit["loss_cp"]["c2c4"] = 0
        audit["scores"]["c2c4"] = {"cp_root": 30, "mate_root": None}
        result = validation.summarize(report, fixture)
        self.assertTrue(result["gates"]["at_least_one_root_with_increased_distinct_moves"])
        self.assertEqual(result["selected_reply_retained_motifs"], 1)
        self.assertFalse(result["gates"]["at_least_one_stable_native_root_with_creative_variety"])
        self.assertFalse(result["sample_acceptance_passed"])

    def test_loss_threshold_aggregate_mate_and_partial_sample_each_block_acceptance(self):
        report, fixture = specimen()
        for loss in (21, 71):
            changed = copy.deepcopy(report)
            changed["root_audits"]["root"]["loss_cp"]["d2d4"] = loss
            result = validation.summarize(changed, fixture)
            self.assertFalse(result["sample_acceptance_passed"])
            self.assertFalse(result["gates"]["changed_pair_loss_sum_not_increased"])
            if loss == 71:
                self.assertFalse(result["gates"]["no_changed_loss_increase_over_50cp"])
        report["root_audits"]["root"]["scores"]["d2d4"] = {"cp_root": None, "mate_root": -4}
        self.assertFalse(validation.summarize(report, fixture)["gates"]["changed_choices_have_comparable_nonmate_audits"])
        report, fixture = specimen()
        report["rows"].pop()
        self.assertFalse(validation.summarize(report, fixture)["gates"]["complete_predeclared_sample"])

    def test_paired_child_evidence_preserves_root_loss_and_uses_parent_perspective(self):
        report, fixture = paired_specimen()
        result = validation.summarize(report, fixture)
        self.assertTrue(result["sample_acceptance_passed"])
        proof = result["comparisons"][1]["creativity"]["native_quality"]
        self.assertTrue(proof["verified"])
        self.assertEqual(proof["root_loss_cp"], 6)
        self.assertEqual(proof["paired_loss_cp"], 0)
        self.assertEqual((proof["baseline_parent_cp"], proof["candidate_parent_cp"]), (10, 12))

    def test_actual_controlled_helper_receipts_match_the_quality_schema_for_both_colors(self):
        import gambit_creativity_tests as controlled
        for black in (False, True):
            helper = controlled.CreativityTests()
            helper.setUp()
            try:
                if black:
                    helper.board = controlled.replay("d2d4 g8f6 c2c4 e7e5 d4e5 f6g4 g1f3")
                    helper.move = validation.chess.Move.from_uci("g7g6")
                    helper.plan = controlled.boss_play.plan(helper.board, 1000, helper.seed)
                detail, discarded = helper.run_pair(candidate="b8c6" if black else "f1c4")
                self.assertFalse(discarded)
                self.assertTrue(detail["applied"])
                arm = {"selected_uci": detail["selected_uci"], "fixture": {
                    "id": "controlled-native-schema", "history_uci": [m.uci() for m in helper.board.move_stack],
                    "before_fen": helper.board.fen(en_passant="fen"), "color": "black" if black else "white"},
                    "receipt": {"baseline_uci": helper.move.uci(), "selected_uci": detail["selected_uci"], "creativity": detail}}
                proof = validation.native_quality_evidence(arm)
                self.assertTrue(proof["verified"], proof)
                self.assertEqual(proof["root_loss_cp"], 5)
                self.assertEqual(proof["paired_loss_cp"], 0)
            finally:
                helper.doCleanups()

    def test_missing_misbound_shallower_or_wrong_sign_child_proof_cannot_pass(self):
        for mutation in ("missing", "wrong_history", "wrong_sign", "shallower", "bound", "root_loss_hidden",
                         "deeper_final", "same_score_different_final_pv", "deeper_root_warning"):
            report, fixture = paired_specimen()
            evidence = report["rows"][1]["arms"]["candidate"]["receipt"]["creativity"]["evidence"]
            if mutation == "missing":
                evidence.pop("paired_child_baseline")
            elif mutation == "wrong_history":
                evidence["paired_child_candidate"]["history_uci"] = ["e2e4"]
            elif mutation == "wrong_sign":
                evidence["selection_compared"]["cp"] = -12
            elif mutation == "shallower":
                evidence["paired_child_candidate"]["common"]["depth"] = 4
            elif mutation == "bound":
                evidence["paired_child_candidate"]["common"]["lowerbound"] = True
            elif mutation == "deeper_final":
                evidence["paired_child_candidate"]["final"]["depth"] = 6
                evidence["selection_final"].update(depth=7, child_depth=6)
            elif mutation == "same_score_different_final_pv":
                evidence["paired_child_candidate"]["final"]["pv_uci"] = ["d7d5", "g1f3"]
                evidence["selection_final"]["pv_uci"] = ["d2d4", "d7d5", "g1f3"]
            elif mutation == "deeper_root_warning":
                evidence["candidate_final"]["depth"] = 7
            else:
                evidence["root_loss_cp"] = 0
            with self.subTest(mutation=mutation):
                result = validation.summarize(report, fixture)
                self.assertFalse(result["sample_acceptance_passed"])
                self.assertFalse(result["gates"]["all_selected_creativity_has_native_quality_evidence"])


if __name__ == "__main__":
    unittest.main()
