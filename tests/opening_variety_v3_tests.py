"""Early-opening exploration coverage and unchanged quality gates.

Legal positions are checked by python-chess. Scores are deliberately synthetic;
these tests establish policy behavior, not playing strength or measured diversity.
"""

from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import chess
import chess.engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import opening_variety as variety


SEED = "42" * 16


def after(*moves):
    board = chess.Board()
    for move in moves:
        board.push_uci(move)
    return board


def legal(board):
    return {move.uci() for move in board.legal_moves}


def packet(board, uci, cp=100, depth=6, **extra):
    return {"depth": depth, "score": chess.engine.PovScore(chess.engine.Cp(cp), board.turn),
            "pv": [chess.Move.from_uci(uci)], **extra}


def empty_catalog():
    return SimpleNamespace(openings=[], positions={}, source={"commit": "empty-test-catalog"})


class EarlyOpeningExplorationTests(unittest.TestCase):
    def test_version_and_quality_limits_are_explicit(self):
        self.assertEqual(variety.POLICY, "regicide-opening-variety-v3")
        self.assertEqual((variety.EARLY_PLIES, variety.MAX_EARLY_PROBES), (6, 10))
        self.assertEqual((variety.MIN_DEPTH, variety.TOLERANCE_CP, variety.MAX_PROBES), (4, 25, 6))

    def test_narrow_van_geet_catalogue_does_not_hide_normal_development(self):
        board = after("b1c3", "g8f6")
        candidates = variety.candidate_moves(board)
        self.assertEqual(set(candidates), legal(board))
        self.assertEqual(candidates, sorted(set(candidates)))
        self.assertTrue({"e2e4", "d2d4", "g1f3", "g2g4"}.issubset(candidates))
        plan = variety.plan(board, 1000, SEED)
        self.assertTrue(plan["eligible"])
        self.assertTrue(plan["early_exploration"])
        self.assertTrue(plan["candidate_source"])

    def test_third_move_explores_legal_moves_outside_the_catalogue(self):
        # The v2 catalogue has no continuation for the white position.
        for moves in [("b1c3", "g8f6", "g1f3", "d7d6"),
                      ("b1c3", "g8f6", "g1f3", "d7d6", "e2e3")]:
            board = after(*moves)
            with self.subTest(ply=len(moves)):
                plan = variety.plan(board, 1000, SEED)
                self.assertTrue(plan["eligible"])
                self.assertTrue(plan["early_exploration"])
                self.assertEqual(set(plan["candidates"]), legal(board))

    def test_six_ply_boundary_restores_catalogue_only_selection(self):
        fifth = after("e2e4", "e7e5", "g1f3", "b8c6", "f1b5")
        sixth = after("e2e4", "e7e5", "g1f3", "b8c6", "f1b5", "a7a6")
        with patch.object(variety, "_catalog", return_value=empty_catalog()):
            early = variety.plan(fifth, 1000, SEED)
            later = variety.plan(sixth, 1000, SEED)
        self.assertTrue(early["eligible"])
        self.assertEqual(set(early["candidates"]), legal(fifth))
        self.assertTrue(early["early_exploration"])
        self.assertFalse(later["early_exploration"])
        self.assertFalse(later["eligible"])
        self.assertEqual(later["reason"], "insufficient_catalog_candidates")
        self.assertEqual(later["candidates"], [])

    def test_missing_catalogue_does_not_prevent_early_engine_verification(self):
        later = after("e2e4", "e7e5", "g1f3", "b8c6", "f1b5", "a7a6")
        with patch.object(variety, "_catalog", side_effect=variety.CatalogError("missing fixture")):
            early = variety.plan(chess.Board(), 1000, SEED)
            old_scope = variety.plan(later, 1000, SEED)
        self.assertTrue(early["eligible"])
        self.assertEqual(set(early["candidates"]), legal(chess.Board()))
        self.assertFalse(old_scope["eligible"])
        self.assertEqual(old_scope["reason"], "catalog_unavailable")

    def test_same_history_seed_and_polling_keep_order_across_budgets(self):
        for board in (chess.Board(), after("e2e4"), after("b1c3", "g8f6")):
            plans = [variety.plan(board.copy(stack=True), budget, SEED) for budget in (500, 1000, 3000, 5000)]
            self.assertTrue(all(row["candidate_order"] == plans[0]["candidate_order"] for row in plans))
            self.assertEqual(set(plans[0]["candidate_order"]), legal(board))
            self.assertEqual(len(plans[0]["candidate_order"]), len(legal(board)))
            for budget, row in zip((500, 1000, 3000, 5000), plans):
                self.assertEqual(row["baseline_budget_ms"], budget)
                self.assertEqual(row["extra_budget_ms"], min(budget, 2000))

    def test_new_seeds_change_proposal_order_without_removing_any_move(self):
        board = chess.Board()
        plans = [variety.plan(board, 1000, f"{seed:032x}") for seed in range(32)]
        self.assertGreater(len({tuple(row["candidate_order"]) for row in plans}), 16)
        self.assertGreater(len({row["candidate_order"][0] for row in plans}), 4)
        self.assertTrue(all(set(row["candidate_order"]) == legal(board) for row in plans))

    def test_early_probe_limit_expands_but_later_limit_is_unchanged(self):
        early = variety.plan(chess.Board(), 1000, SEED)
        base = chess.Move.from_uci("g1f3")
        early_probes = variety.proposals(early, base)
        self.assertEqual(len(early_probes), 10)
        self.assertNotIn(base.uci(), early_probes)
        self.assertEqual(len(early_probes), len(set(early_probes)))
        # A real late plan retains its metadata; a longer list isolates its cap.
        later_board = after("e2e4", "e7e5", "g1f3", "b8c6", "f1b5", "a7a6")
        later = variety.plan(later_board, 1000, SEED)
        later["candidate_order"] = early["candidate_order"]
        self.assertEqual(len(variety.proposals(later, base)), 6)

    def test_all_legal_moves_can_lead_but_development_is_more_frequent(self):
        board = chess.Board()
        developing = {"b1c3", "g1f3", "b2b3", "g2g3", "c2c3", "c2c4",
                      "d2d3", "d2d4", "e2e3", "e2e4"}
        first_moves = [variety.plan(board, 1000, f"{seed:032x}")["candidate_order"][0]
                       for seed in range(512)]
        self.assertEqual(set(first_moves), legal(board))
        developing_share = sum(move in developing for move in first_moves) / len(first_moves)
        self.assertGreater(developing_share, 0.6)
        self.assertLess(developing_share, 0.95)

    def test_early_priority_never_bypasses_score_depth_or_bound_checks(self):
        board = chess.Board()
        base = chess.Move.from_uci("g1f3")
        baseline = packet(board, base.uci())
        for cp, depth, extra, admitted in [(75, 6, {}, True), (74, 6, {}, False),
                                           (90, 5, {}, False), (90, 6, {"lowerbound": True}, False)]:
            with self.subTest(cp=cp, depth=depth, extra=extra):
                value = packet(board, "e2e4", cp, depth, **extra)
                result = variety.assess(board, base, baseline, "e2e4", {depth: value}, value,
                                        chess.Move.from_uci("e2e4"))
                self.assertEqual(result["admitted"], admitted)
        no_evidence = variety.receipt(board, variety.plan(board, 1000, SEED), base, baseline, [], SEED)
        self.assertEqual(no_evidence["selected_uci"], base.uci())
        self.assertEqual(no_evidence["selection_pool"], [base.uci()])
        fringe = packet(board, "a2a3", 74)
        self.assertFalse(variety.assess(board, base, baseline, "a2a3", {6: fringe}, fringe,
                                        chess.Move.from_uci("a2a3"))["admitted"])

    def test_early_exploration_keeps_short_budget_forced_and_repetition_guards(self):
        cases = [(chess.Board(), 100, False, "short_budget"),
                 (chess.Board(), 1000, True, "user_forced_game"),
                 (after("g1f3", "g8f6", "f3g1", "f6g8"), 1000, False, "repetition"),
                 (after("e2e4", "d7d5", "f1b5"), 1000, False, "in_check")]
        for board, budget, forced, reason in cases:
            with self.subTest(reason=reason):
                plan = variety.plan(board, budget, SEED, forced=forced)
                self.assertFalse(plan["eligible"])
                self.assertEqual(plan["reason"], reason)
                self.assertEqual(plan["baseline_budget_ms"], budget)
                self.assertEqual(plan["extra_budget_ms"], 0)

    def test_early_weights_favor_only_alternatives_with_completed_evidence(self):
        board = chess.Board()
        base = chess.Move.from_uci("g1f3")
        baseline = packet(board, base.uci())
        plan = variety.plan(board, 1000, SEED)
        proposed = variety.proposals(plan, base)[:3]
        probes = []
        for uci, cp in zip(proposed, (90, 95, 74)):
            value = packet(board, uci, cp)
            probes.append(variety.assess(board, base, baseline, uci, {6: value}, value,
                                         chess.Move.from_uci(uci)))
        row = variety.receipt(board, plan, base, baseline, probes, SEED)
        self.assertEqual(row["selection_rule"], "early_verified_alternatives")
        self.assertEqual(row["selection_weights"], {base.uci(): 1, proposed[0]: 2, proposed[1]: 2})
        self.assertEqual(set(row["selection_pool"]), {base.uci(), proposed[0], proposed[1]})
        self.assertNotIn(proposed[2], row["selection_pool"])

    def test_existing_pgn_policy_tags_survive_the_version_upgrade(self):
        for version in (1, 2, 3):
            policy = f"regicide-opening-variety-v{version}"
            self.assertIn(policy, variety.KNOWN_POLICIES)
            self.assertEqual(variety.pgn_headers({"enabled": True, "policy": policy, "seed": SEED}),
                             {"OpeningVarietySeed": SEED, "OpeningVarietyPolicy": policy})
        self.assertEqual(variety.pgn_headers({"enabled": True, "policy": "unknown", "seed": SEED}), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
