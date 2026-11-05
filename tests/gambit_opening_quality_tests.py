"""Selection gates for opening knowledge; controlled replies are not strength evidence."""
import json
import unittest
from unittest.mock import patch

import gambit_operations_tests as support
from gambit_repertoire_play_tests import packet

boss = support.boss_play
operations = support.operations
knowledge = support.gambit_knowledge
policy = support.verifier


class OpeningQualityTests(unittest.TestCase):
    controlled_choice = support.OperationsTests.controlled_choice

    def setUp(self):
        cases = json.loads(operations.CURRICULUM.read_text(encoding="utf-8"))["cases"]
        self.board = support.replay(next(row for row in cases if row["family_id"] == "evans")["moves"])
        self.plan = boss.plan(self.board, 1000, "safety")
        self.plan["knowledge_candidates"] = knowledge.proposals(self.board, "safety")[:1]

    def test_completed_native_gain_can_outweigh_weaker_geometric_plan(self):
        with patch.object(operations, "continuation_evidence", side_effect=[{"points": 20}, {"points": -20}]):
            result, discarded, calls = self.controlled_choice(self.plan, 10)
        self.assertTrue(calls)
        self.assertFalse(discarded)
        self.assertTrue(result["applied"])
        self.assertEqual(result["evidence"]["admission_basis"], "native_quality_improvement")
        self.assertEqual(result["evidence"]["native_gain_cp"], 10)

    def test_small_native_difference_does_not_override_worse_operations(self):
        for cp in (-1, 0, 9):
            with self.subTest(cp=cp), patch.object(operations, "continuation_evidence",
                                                  side_effect=[{"points": 20}, {"points": -20}]):
                result, _, _ = self.controlled_choice(self.plan, cp)
            self.assertFalse(result["applied"])
            self.assertEqual(result["attempts"][0]["reason"], "no_reply_supported_operations_gain")

    def test_native_gain_cannot_authorize_an_unresolved_exchange(self):
        with patch.object(operations, "continuation_evidence", side_effect=[{"points": 20}, None]):
            result, _, _ = self.controlled_choice(self.plan, 500)
        self.assertFalse(result["applied"])
        self.assertEqual(result["selected_uci"], "h2h3")

    def study_choice(self, *, selected="c2c3", depth=7, cp=-50, changes=None, error=None,
                     unsettled=False, host=False, enabled=True):
        self.plan["knowledge_candidates"] = [{"uci": "c2c3", "source": "native_gambit_continuation",
            "offline_evidence": {"cp": 99999}, "loss_cap_cp": 99999}]
        base = support.chess.Move.from_uci("h2h3")
        original = packet(self.board, base.uci(), 0)
        calls, closed = [], []
        def search(amount, **kwargs):
            calls.append((amount, kwargs))
            if error:
                raise error
            move = support.chess.Move.from_uci(selected)
            info = packet(self.board, selected, cp, depth=depth, **(changes or {}))
            return support.chess.engine.BestMove(move, None), info, {depth: info}
        with patch.object(policy, "ENABLE_STUDIED_FINALISTS", enabled), patch.object(
                knowledge, "valid_proposal", return_value=True), patch.object(
                operations, "continuation_evidence", side_effect=[{"points": 100}, None if unsettled else {"points": -100}]):
            chooser = boss.explore_initiative if host else policy.choose
            result, discarded = chooser(self.board, self.plan, base, original, {6: original},
                                        "study", search, lambda: closed.append(True))
        return result, discarded, calls

    def test_training_scores_do_not_override_current_deeper_baseline_winner(self):
        result, discarded, calls = self.study_choice(selected="h2h3")
        self.assertTrue(calls)
        self.assertFalse(discarded)
        self.assertFalse(result["applied"])
        self.assertEqual(result["selected_uci"], "h2h3")
        self.assertEqual(result["attempts"][0]["loss_cap_cp"], 0)
        self.assertEqual(result["reason"], "baseline_wins_studied_comparison")

    def test_unproven_deeper_comparison_is_off_and_default_keeps_same_depth_gate(self):
        self.assertFalse(policy.ENABLE_STUDIED_FINALISTS)
        result, discarded, calls = self.study_choice(enabled=False, depth=6, cp=10)
        self.assertFalse(discarded)
        self.assertTrue(result["applied"])
        self.assertEqual(result["loss_cp"], 0)
        self.assertEqual(calls[0][1]["depth"], 6)
        self.assertEqual(calls[0][1]["root_move"].uci(), "c2c3")
        self.assertNotIn("root_moves", calls[0][1])

    def test_study_search_includes_baseline_and_requires_one_more_completed_depth(self):
        result, discarded, calls = self.study_choice()
        self.assertFalse(discarded)
        self.assertTrue(result["applied"])
        self.assertEqual(result["selected_uci"], "c2c3")
        self.assertIsNone(result["loss_cp"])
        self.assertEqual(result["selected_vs_finalist_best_loss_cp"], 0)
        self.assertEqual(len(calls), 1)
        self.assertLessEqual(calls[0][0], self.plan["extra_budget_ms"])
        kwargs = calls[0][1]
        self.assertEqual({move.uci() for move in kwargs["root_moves"]}, {"h2h3", "c2c3"})
        self.assertEqual(kwargs["depth"], 7)
        self.assertTrue(kwargs["fresh"])
        self.assertFalse(kwargs["publish"])

    def test_incomplete_wrong_root_bound_and_unsettled_study_keep_baseline(self):
        for options in ({"depth": 6}, {"selected": "a2a3"}, {"changes": {"lowerbound": True}},
                        {"unsettled": True}):
            with self.subTest(options=options):
                result, discarded, _ = self.study_choice(**options)
            self.assertFalse(result["applied"])
            self.assertEqual(result["selected_uci"], "h2h3")
            self.assertFalse(discarded)

    def test_failed_study_discards_engine_and_keeps_baseline(self):
        result, discarded, _ = self.study_choice(error=RuntimeError("controlled"))
        self.assertTrue(discarded)
        self.assertEqual(result["selected_uci"], "h2h3")
        self.assertFalse(result["applied"])

    def test_host_does_not_subtract_scores_from_different_depths(self):
        result, _, _ = self.study_choice(host=True)
        self.assertEqual(result["selected_uci"], "c2c3")
        self.assertIsNone(result["loss_cp"])
        self.assertIsNone(result["reference_delta_cp"])
        self.assertEqual(result["compared_depth"], 7)
        self.assertEqual(result["baseline"]["depth"], 6)
        self.assertEqual(result["selected_vs_finalist_best_loss_cp"], 0)

    def test_memory_key_changes_with_continuation_data_and_selection_policy(self):
        metadata = knowledge.metadata()
        def key():
            return boss.memo_key(self.board, 1000, "binary", "evaluation")
        with patch.object(knowledge, "metadata", return_value={**metadata, "extension_manifest_sha256": "A"}):
            first = key()
        with patch.object(knowledge, "metadata", return_value={**metadata, "extension_manifest_sha256": "B"}):
            self.assertNotEqual(first, key())
        first = key()
        with patch.object(policy, "POLICY", "changed-policy"):
            self.assertNotEqual(first, key())
        with patch.object(policy, "ENABLE_STUDIED_FINALISTS", True):
            self.assertNotEqual(first, key())
        with patch.object(operations, "POLICY", "changed-operations"):
            self.assertNotEqual(first, key())
        with patch.object(operations, "CONTINUATION_POLICY", "changed-continuation"):
            self.assertNotEqual(first, key())

    def test_previous_game_policy_and_current_signatures_remain_recognizable(self):
        self.assertIn("regicide-boss-play-v9", boss.SUPPORTED_POLICIES)
        self.assertIn(boss.POLICY, boss.gambit_initiative.SOURCE_POLICIES)

    def test_four_old_memory_rows_do_not_hide_new_opening_study(self):
        studied = {"uci": "c2c3", "source": "native_gambit_continuation", "catalog_sha256": "test"}
        old = [{"uci": move, "source": "native_practice_memory"}
               for move in ("h2h3", "a2a3", "d2d3", "b1c3")]
        with patch.object(boss.gambit_practice_memory, "proposals", return_value=old), \
                patch.object(knowledge, "runtime_proposals", return_value=[studied]), \
                patch.object(boss.gambit_learning, "rank_with_metadata", return_value=([studied], {})):
            plan = boss.plan(self.board, 1000, "safety")
        rows = plan["knowledge_candidates"]
        self.assertEqual(rows[0], old[0])
        self.assertEqual(rows[1], studied)
        self.assertEqual(len({row["uci"] for row in rows}), len(rows))

    def test_duplicate_study_preserves_independently_valid_practiced_evidence(self):
        studied = {"uci": "c2c3", "source": "native_gambit_continuation", "catalog_sha256": "test"}
        old = [{"uci": "c2c3", "source": "native_practice_memory"}]
        with patch.object(boss.gambit_practice_memory, "proposals", return_value=old), \
                patch.object(knowledge, "runtime_proposals", return_value=[studied]), \
                patch.object(boss.gambit_learning, "rank_with_metadata", return_value=([studied], {})):
            plan = boss.plan(self.board, 1000, "safety")
        self.assertEqual(plan["knowledge_candidates"][0], old[0])
        self.assertEqual(sum(row["uci"] == "c2c3" for row in plan["knowledge_candidates"]), 1)


if __name__ == "__main__":
    unittest.main()
