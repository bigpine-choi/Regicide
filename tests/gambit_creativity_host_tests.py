"""Shared-budget and receipt boundaries; controlled replies are not strength evidence."""
import copy
import time
import unittest
from unittest.mock import patch

from gambit_repertoire_play_tests import boss_play as boss, chess, packet


class CreativityHostTests(unittest.TestCase):
    def setUp(self):
        enabled = patch.object(boss.gambit_creativity, "ENABLED", True)
        enabled.start()
        self.addCleanup(enabled.stop)
        self.board = chess.Board()
        for uci in "d2d4 d7d5 e2e4 d5e4 b1c3 g8f6 f2f3 e4f3 g1f3 c8g4".split():
            self.board.push_uci(uci)
        self.plan = boss.plan(self.board, 1000, "creative-host")
        self.base = chess.Move.from_uci("h2h3")
        self.info = packet(self.board, "h2h3", 0, depth=5)
        self.old = {"policy": boss.gambit_repertoire_play.POLICY,
                    "selected_uci": "h2h3", "applied": False, "loss_cp": 0,
                    "reason": "knowledge_move_is_baseline", "evidence": None,
                    "extra_elapsed_ms": 0, "attempts": [{"uci": "f3e5", "admitted": False}]}
        candidate = boss.gambit_initiative._line(self.board,
            packet(self.board, "f1c4", 10, depth=5), chess.Move.from_uci("f1c4"))
        self.creative = {"policy": boss.gambit_creativity.POLICY,
            "selected_uci": "f1c4", "applied": True, "reason": "controlled_creativity",
            "loss_cp": 0, "extra_elapsed_ms": 0,
            "evidence": {"candidate_compared": candidate, "candidate_final": candidate,
                         "motif": "piece_coordination", "retained_after_reply": True}}

    def run_host(self, *, old=None, discard=False, creative=None, failed=False, delay=0):
        saved = copy.deepcopy(old or self.old)
        def ordinary(*_args, **_kwargs):
            if delay:
                time.sleep(delay)
            return saved, discard
        with patch.object(boss.gambit_repertoire_play, "choose", side_effect=ordinary), patch.object(
                boss.gambit_creativity, "choose", return_value=(creative or self.creative, failed)) as helper:
            result, discarded = boss.explore_initiative(self.board, self.plan, self.base,
                self.info, {5: self.info}, "creative-host", lambda *_a, **_k: None, lambda: None)
        self.assertEqual(saved, old or self.old)
        return result, discarded, helper

    def test_existing_verified_memory_choice_keeps_priority_and_original_allowance(self):
        old = {**copy.deepcopy(self.old), "applied": True, "selected_uci": "f1c4",
               "loss_cp": 1, "reason": "native_verified_knowledge_continuation",
               "evidence": self.creative["evidence"]}
        result, discarded, helper = self.run_host(old=old)
        helper.assert_not_called()
        self.assertFalse(discarded)
        self.assertEqual(result["selected_uci"], "f1c4")
        self.assertEqual(result["loss_cp"], 1)
        self.assertNotIn("creativity", result)

    def test_discarded_or_time_exhausted_ordinary_search_cannot_start_creativity(self):
        result, discarded, helper = self.run_host(discard=True)
        self.assertTrue(discarded)
        helper.assert_not_called()
        self.assertEqual(result["selected_uci"], "h2h3")
        self.plan["extra_budget_ms"] = 1
        _, _, helper = self.run_host()
        helper.assert_not_called()

    def test_fallback_uses_only_actual_remaining_budget_and_excludes_failed_proposals(self):
        self.plan["extra_budget_ms"] = 250
        before = self.board.fen(), list(self.board.move_stack)
        result, discarded, helper = self.run_host(delay=.02)
        self.assertFalse(discarded)
        helper.assert_called_once()
        args, kwargs = helper.call_args
        self.assertGreaterEqual(kwargs["budget_ms"], boss.gambit_creativity.MIN_PROBE_MS)
        self.assertLess(kwargs["budget_ms"], 230)
        self.assertEqual(kwargs["excluded_uci"], ["f3e5"])
        self.assertEqual(kwargs["seed"], "creative-host")
        self.assertIs(args[0], self.board)
        self.assertEqual((self.board.fen(), self.board.move_stack), before)
        self.assertGreaterEqual(result["extra_elapsed_ms"], 20)

    def test_receipt_keeps_original_evidence_separate_from_actual_creative_selection(self):
        result, discarded, _ = self.run_host()
        self.assertFalse(discarded)
        self.assertEqual(result["selected_uci"], "f1c4")
        self.assertEqual(result["candidate_source"], "native_gambit_creativity")
        self.assertEqual(result["verification_policy"], boss.gambit_creativity.POLICY)
        self.assertEqual(result["best_defense_cp"], 10)
        self.assertEqual(result["reference_delta_cp"], -10)
        self.assertEqual(result["compared_depth"], 5)
        self.assertEqual(result["knowledge"], self.old)
        self.assertEqual(result["operations"], self.old)
        self.assertEqual(result["creativity"], self.creative)

    def test_failed_optional_search_cannot_publish_its_alternative(self):
        result, discarded, _ = self.run_host(failed=True)
        self.assertTrue(discarded)
        self.assertEqual(result["selected_uci"], "h2h3")
        self.assertNotEqual(result["verification_policy"], boss.gambit_creativity.POLICY)

    def test_child_comparison_does_not_claim_zero_loss_against_original_root(self):
        creative = copy.deepcopy(self.creative)
        root_candidate = creative["evidence"]["candidate_compared"]
        root_candidate["cp"] = -7
        paired_baseline = {"cp": -15, "depth": 5, "child_depth": 4,
                           "depth_basis": "fixed_root_ply_plus_completed_child_depth"}
        selected = {**root_candidate, "cp": -12, "child_depth": 4,
                    "depth_basis": "fixed_root_ply_plus_completed_child_depth"}
        child = self.board.copy(stack=True)
        child.push_uci("f1c4")
        new_reply = next(move.uci() for move in child.legal_moves
                         if move.uci() != root_candidate["pv_uci"][1])
        selected["pv_uci"] = ["f1c4", new_reply]
        creative.update(comparison_kind="paired_unrestricted_children", child_depth=4,
                        root_loss_cp=7, paired_loss_cp=0, baseline_compared=paired_baseline)
        creative["evidence"].update(selection_compared=selected, selection_final=selected)
        result, discarded, _ = self.run_host(creative=creative)
        self.assertFalse(discarded)
        self.assertEqual(result["baseline"]["cp"], 0)
        self.assertEqual(result["comparison_baseline"], paired_baseline)
        self.assertEqual(result["best_defense_cp"], -12)
        self.assertEqual(result["best_defense_pv_uci"], selected["pv_uci"])
        self.assertNotEqual(result["best_defense_pv_uci"], root_candidate["pv_uci"])
        self.assertIsNone(result["compared_depth"])
        self.assertIsNone(result["reference_delta_cp"])
        self.assertEqual(result["root_reference_delta_cp"], 7)
        self.assertEqual(result["root_loss_cp"], 7)
        self.assertEqual(result["paired_loss_cp"], 0)
        self.assertEqual(result["verification_child_depth"], 4)
        self.assertEqual(result["loss_basis"], "paired_completed_child_searches")
        self.assertEqual(result["knowledge"], self.old)

    def test_disabled_mode_and_policy_changes_cannot_reuse_selection_cache(self):
        def key():
            return boss.memo_key(self.board, 1000, "engine", "evaluation", seed="creative-host")
        original = key()
        with patch.object(boss.gambit_creativity, "ENABLED", False):
            result, _, helper = self.run_host()
            helper.assert_not_called()
            self.assertEqual(result["selected_uci"], "h2h3")
            self.assertNotEqual(original, key())
        with patch.object(boss.gambit_creativity, "POLICY", "different-creativity-policy"):
            self.assertNotEqual(original, key())
        self.assertIn("regicide-boss-play-v10", boss.SUPPORTED_POLICIES)
        self.assertIn(boss.POLICY, boss.gambit_initiative.SOURCE_POLICIES)


if __name__ == "__main__":
    unittest.main()
