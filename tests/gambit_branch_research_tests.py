"""Bounded branch scheduling and restart equivalence with a deterministic teacher."""
import json
import unittest

import gambit_research_tests as base

FakeTeacher, semantic = base.FakeTeacher, base.semantic


class BranchResearchTests(unittest.TestCase):
    setUp = base.ResearchTests.setUp
    run_research = base.ResearchTests.run_research
    read = base.ResearchTests.read

    def test_one_branch_cap_preserves_original_semantic_results(self):
        self.run_research("original")
        FakeTeacher.reset()
        self.run_research("one", opponent_replies=3, max_branches=1)
        self.assertEqual(len(FakeTeacher.calls), 4)
        for file in ("records.json", "practice.json", "queries.json"):
            self.assertEqual(semantic(self.read("original", file)), semantic(self.read("one", file)))

    def test_two_replies_have_distinct_full_histories_and_continuations(self):
        result = self.run_research("two", opponent_replies=2)
        branches = self.read("two", "practice.json")
        self.assertEqual(len(branches), 2)
        self.assertEqual([len(branch["turns"]) for branch in branches], [2, 1])
        primary, alternative = branches
        self.assertNotEqual(primary["turns"][0]["opponent_move"], alternative["fork"]["opponent_move"])
        history = alternative["initial_history_uci"]
        self.assertEqual(history[:-2], primary["initial_history_uci"])
        self.assertEqual(history[-2], primary["turns"][0]["owner_move"])
        self.assertEqual(history[-1], alternative["fork"]["opponent_move"])
        self.assertEqual(result["counts"]["native_queries"], 7)
        self.assertEqual(result["counts"]["alternative_reply_queries"], 1)
        self.assertEqual(len({row[0] for row in FakeTeacher.calls}), 7)
        alternative_queries = [row for row in self.read("two", "queries.json") if row["role"] == "opponent_alternative"]
        self.assertNotIn(primary["turns"][0]["opponent_move"], alternative_queries[0]["root_moves_uci"])
        restricted_ids = {row["query_id"] for row in alternative_queries}
        self.assertFalse(any(item["query_id"] in restricted_ids for row in result["records"] for item in row["candidates"]))

    def test_branch_pause_resume_replays_each_completed_query_once(self):
        args = dict(opponent_replies=2, max_branches=3, owner_turns=3)
        continuous = self.run_research("full", **args)
        expected = len(FakeTeacher.calls)
        FakeTeacher.reset()
        for attempt in range(expected + 1):
            value = self.run_research("chunks", resume=attempt > 0, max_new_queries=1, **args)
            if value["research_status"] == "complete":
                break
        self.assertEqual(value["research_status"], "complete")
        self.assertEqual(len(FakeTeacher.calls), expected)
        self.assertEqual(len({row[0] for row in FakeTeacher.calls}), expected)
        self.assertEqual(value["records"], continuous["records"])
        for file in ("records.json", "practice.json", "queries.json"):
            self.assertEqual(semantic(self.read("full", file)), semantic(self.read("chunks", file)))

    def test_total_node_cap_counts_reused_queries_and_finishes_bounded_plan(self):
        args = dict(opponent_replies=2, owner_turns=4, max_research_nodes=900000)
        continuous = self.run_research("bounded", **args)
        self.assertTrue(continuous["counts"]["coverage_budget_limited"])
        self.assertEqual(continuous["counts"]["requested_nodes_total"], 900000)
        branches = self.read("bounded", "practice.json")
        self.assertTrue(all(branch["turns"] for branch in branches))
        FakeTeacher.reset()
        self.run_research("bounded-resume", max_new_queries=3, **args)
        resumed = self.run_research("bounded-resume", resume=True, **args)
        self.assertEqual(len(FakeTeacher.calls), 9)
        self.assertEqual(resumed["records"], continuous["records"])
        self.assertEqual(resumed["counts"], continuous["counts"])

    def test_minimum_cap_spends_on_complete_turn_before_fork(self):
        result = self.run_research("small", opponent_replies=3, max_research_nodes=200000)
        self.assertEqual(result["counts"]["native_queries"], 2)
        self.assertEqual(result["counts"]["alternative_branches"], 0)
        self.assertTrue(result["counts"]["coverage_budget_limited"])

    def test_small_cap_continues_alternative_before_deepening_main_branch(self):
        result = self.run_research("five", opponent_replies=2, max_research_nodes=500000)
        branches = self.read("five", "practice.json")
        self.assertEqual([len(row["turns"]) for row in branches], [1, 1])
        self.assertEqual(result["counts"]["requested_nodes_total"], 500000)
        self.assertEqual(result["counts"]["alternative_branches_continued"], 1)

    def test_branch_count_is_bounded_and_holdout_branches_stay_unexported(self):
        result = self.run_research("heldout", families=["evans", "from"], opponent_replies=3,
                                   owner_turns=4, max_branches=3)
        branches = self.read("heldout", "practice.json")
        for family in ("evans", "from"):
            self.assertLessEqual(sum(row["family_id"] == family for row in branches), 3)
        self.assertTrue(all(row["family_id"] != "from" for row in result["records"]))
        self.assertGreater(result["counts"]["heldout_practice_records"], 2)

    def test_branch_limits_are_immutable_and_bad_values_rejected(self):
        self.run_research("pause", opponent_replies=2, max_new_queries=1)
        for kwargs in ({"opponent_replies": 1}, {"opponent_replies": 2, "max_branches": 3},
                       {"opponent_replies": 2, "max_research_nodes": 900000}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.run_research("pause", resume=True, **kwargs)
        for kwargs in ({"opponent_replies": 0}, {"opponent_replies": 4}, {"max_branches": 0},
                       {"max_branches": 17}, {"max_research_nodes": 1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.run_research("invalid", **kwargs)

    def test_focused_curriculum_inherits_prior_reserved_descendant_positions(self):
        first = self.run_research("reference")
        reserved = first["records"][1]["position_key"]
        self.document["reserved_holdout_position_keys"] = [reserved]
        self.curriculum.write_text(json.dumps(self.document), encoding="utf-8")
        result = self.run_research("inherited")
        self.assertIn(reserved, result["heldout_position_keys"])
        self.assertTrue(all(row["position_key"] != reserved for row in result["records"]))
        self.assertGreaterEqual(result["counts"]["overlap_excluded"], 1)
        self.document["reserved_holdout_position_keys"] = reserved
        self.curriculum.write_text(json.dumps(self.document), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.run_research("bad-inherited")


if __name__ == "__main__":
    unittest.main()
