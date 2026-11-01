"""Opening-v2 policy evidence using synthetic packets, not strength claims.

No engine is launched. Catalogue positions and all score-line moves are real
legal chess; injected scores control admission and fail-closed boundaries.
"""
import copy
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import chess
import chess.engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import opening_variety as variety
from opening_guide import position_key


SEED = "01" * 16


def after(*moves):
    board = chess.Board()
    for uci in moves:
        board.push_uci(uci)
    return board


def packet(board, uci, cp=100, depth=6, **extra):
    return {"depth": depth, "score": chess.engine.PovScore(chess.engine.Cp(cp), board.turn),
            "pv": [chess.Move.from_uci(uci)], **extra}


def cases():
    return ((chess.Board(), "e2e4", "d2d4"), (after("e2e4"), "c7c5", "e7e5"))


def assess(board, baseline, proposed, compared, final=None, *, returned=None, error=None, baseline_info=None,
           budget_ms=200, elapsed_ms=35):
    return variety.assess(board, chess.Move.from_uci(baseline),
                          baseline_info if baseline_info is not None else packet(board, baseline),
                          proposed, compared, final, chess.Move.from_uci(returned or proposed),
                          candidate_error=error, budget_ms=budget_ms, elapsed_ms=elapsed_ms)


class OpeningVarietyV2Tests(unittest.TestCase):
    def test_full_baseline_budget_and_bounded_extra_are_independent(self):
        self.assertEqual(variety.POLICY, "regicide-opening-variety-v3")
        self.assertEqual((variety.MAX_PLIES, variety.MIN_DEPTH, variety.TOLERANCE_CP, variety.MAX_PROBES),
                         (16, 4, 25, 6))
        for budget, extra, enabled in ((100, 0, False), (499, 0, False), (500, 500, True),
                                       (1000, 1000, True), (5000, 2000, True)):
            with self.subTest(budget=budget):
                plan = variety.plan(chess.Board(), budget, SEED)
                self.assertEqual(plan["baseline_budget_ms"], budget)
                self.assertEqual(plan["extra_budget_ms"], extra)
                self.assertEqual(plan["eligible"], enabled)

    def test_position_transposition_expands_pool_but_does_not_merge_history_memos(self):
        first = after("g1f3", "g8f6", "d2d4", "d7d5")
        second = after("d2d4", "d7d5", "g1f3", "g8f6")
        self.assertEqual(position_key(first), position_key(second))
        self.assertEqual(variety.candidate_moves(first), variety.candidate_moves(second))
        self.assertTrue({"c1f4", "c1g5", "c2c4", "e2e3", "g2g3"}.issubset(variety.candidate_moves(first)))
        for board in (first, second):
            self.assertTrue(variety.plan(board, 1000, SEED)["eligible"])
        keys = [variety.memo_key(board, 1000, "binary", "model", seed=SEED, catalog_id="catalog")
                for board in (first, second)]
        self.assertNotEqual(*keys)

    def test_first_move_pool_is_not_limited_to_the_previous_four_moves(self):
        first = variety.candidate_moves(chess.Board())
        replies = variety.candidate_moves(after("e2e4"))
        self.assertTrue({"e2e4", "d2d4", "c2c4", "g1f3", "b2b3", "g2g3", "b1c3"}.issubset(first))
        self.assertTrue({"e7e5", "c7c5", "e7e6", "c7c6", "d7d5", "g8f6", "g7g6"}.issubset(replies))
        for board, pool in ((chess.Board(), first), (after("e2e4"), replies)):
            self.assertEqual(pool, sorted(set(pool)))
            self.assertTrue(all(chess.Move.from_uci(move) in board.legal_moves for move in pool))

    def test_duplicate_catalogue_rows_do_not_weight_proposals_or_add_illegal_moves(self):
        board = after("g1f3", "g8f6", "g2g3", "g7g6", "f1g2", "f8g7")
        openings = [SimpleNamespace(moves=(move,)) for move in ("e2e4", "d2d4", "e2e5")]
        small = SimpleNamespace(openings=openings, positions={position_key(board): ((0, 0), (1, 0), (2, 0))},
                                source={"commit": "fixture-catalogue"})
        repeated = SimpleNamespace(openings=openings,
                                   positions={position_key(board): ((0, 0),) * 100 + ((1, 0), (2, 0))},
                                   source=small.source)
        with patch.object(variety, "_catalog", return_value=small):
            expected = variety.plan(board, 1000, SEED)
        with patch.object(variety, "_catalog", return_value=repeated):
            actual = variety.plan(board, 1000, SEED)
        self.assertEqual(actual["candidates"], ["d2d4", "e2e4"])
        self.assertEqual(actual["candidate_order"], expected["candidate_order"])
        self.assertEqual(variety.proposals(actual, chess.Move.from_uci("e2e4")), ["d2d4"])
        value = packet(board, "d2d4", 90)
        probe = assess(board, "e2e4", "d2d4", {6: value}, value)
        receipts = [variety.receipt(board, plan, chess.Move.from_uci("e2e4"), packet(board, "e2e4"),
                                    [probe], SEED) for plan in (expected, actual)]
        self.assertEqual(receipts[0]["selection_weights"], {"e2e4": 1, "d2d4": 1})
        self.assertEqual(receipts[0]["selection_weights"], receipts[1]["selection_weights"])
        self.assertEqual(receipts[0]["selected_uci"], receipts[1]["selected_uci"])

    def test_single_catalogue_alternative_is_eligible_after_baseline_exclusion(self):
        board = after("g1f3", "g8f6", "g2g3", "g7g6", "f1g2", "f8g7")
        catalogue = SimpleNamespace(openings=[SimpleNamespace(moves=("d2d4",))],
                                    positions={position_key(board): ((0, 0),)},
                                    source={"commit": "single-candidate"})
        with patch.object(variety, "_catalog", return_value=catalogue):
            plan = variety.plan(board, 1000, SEED)
        self.assertTrue(plan["eligible"])
        self.assertEqual(variety.proposals(plan, chess.Move.from_uci("e2e4")), ["d2d4"])
        self.assertEqual(variety.proposals(plan, chess.Move.from_uci("d2d4")), [])

    def test_seed_order_is_budget_independent_and_excludes_baseline_before_early_cap(self):
        board = chess.Board()
        plans = [variety.plan(board, budget, SEED) for budget in (500, 1000, 5000)]
        self.assertEqual(plans[0]["candidate_order"], plans[1]["candidate_order"])
        self.assertEqual(plans[1]["candidate_order"], plans[2]["candidate_order"])
        self.assertEqual(set(plans[0]["candidate_order"]), set(plans[0]["candidates"]))
        baseline = plans[0]["candidate_order"][0]
        proposals = variety.proposals(plans[0], chess.Move.from_uci(baseline))
        self.assertEqual(len(proposals), 10)
        self.assertNotIn(baseline, proposals)
        self.assertEqual(proposals, plans[0]["candidate_order"][1:11])
        self.assertEqual(proposals, variety.proposals(copy.deepcopy(plans[0]), chess.Move.from_uci(baseline)))

    def test_twofold_repetition_and_a_previous_repetition_disable_variety(self):
        repeated = after("g1f3", "g8f6", "f3g1", "f6g8")
        self.assertTrue(repeated.is_repetition(2))
        self.assertFalse(repeated.can_claim_threefold_repetition())
        continued = repeated.copy(stack=True); continued.push_uci("e2e4")
        self.assertFalse(continued.is_repetition(2))
        for board in (repeated, continued):
            original = board.fen(), tuple(board.move_stack)
            plan = variety.plan(board, 1000, SEED)
            self.assertFalse(plan["eligible"])
            self.assertEqual(plan["extra_budget_ms"], 0)
            self.assertEqual(plan["baseline_budget_ms"], 1000)
            self.assertEqual((board.fen(), tuple(board.move_stack)), original)

    def test_longer_opening_horizon_stops_at_exactly_sixteen_plies(self):
        moves = "e2e4 e7e5 g1f3 b8c6 f1b5 a7a6 b5a4 g8f6 e1g1 f8e7 f1e1 b7b5 a4b3 d7d6 c2c3 e8g8".split()
        for length in (6, 14, 15):
            board = after(*moves[:length])
            self.assertTrue(board.is_valid())
            self.assertTrue(variety.plan(board, 1000, SEED)["eligible"], length)
        final = variety.plan(after(*moves), 1000, SEED)
        self.assertFalse(final["eligible"])
        self.assertEqual(final["extra_budget_ms"], 0)

    def test_same_depth_25cp_boundary_and_white_normalization_for_both_sides(self):
        for board, baseline, proposed in cases():
            for cp, admitted in ((75, True), (74, False)):
                value = packet(board, proposed, cp)
                result = assess(board, baseline, proposed, {6: value}, value)
                self.assertEqual(result["admitted"], admitted)
                self.assertEqual(result["loss_cp"], 100 - cp)
                self.assertEqual(result["candidate_compared"]["cp"], cp)
                self.assertEqual(result["candidate_compared"]["cp_white"], cp if board.turn else -cp)

    def test_inconsistent_large_positive_restricted_score_is_not_admitted(self):
        for board, baseline, proposed in cases():
            value = packet(board, proposed, 126)
            result = assess(board, baseline, proposed, {6: value}, value)
            self.assertFalse(result["admitted"])
            self.assertNotEqual(result["reason"], "outside_tolerance")

    def test_deeper_good_score_does_not_replace_missing_or_bad_baseline_depth(self):
        for board, baseline, proposed in cases():
            bad, better = packet(board, proposed, 50, 6), packet(board, proposed, 100, 8)
            for packets in ({8: better}, {6: bad, 8: better}):
                result = assess(board, baseline, proposed, packets, better)
                self.assertFalse(result["admitted"])
            good, adverse = packet(board, proposed, 90, 6), packet(board, proposed, -300, 8)
            result = assess(board, baseline, proposed, {6: good, 8: adverse}, adverse)
            self.assertFalse(result["admitted"])
            self.assertIn("deeper", result["reason"])

    def test_invalid_or_nonexact_packets_never_supply_admission_proof(self):
        for board, baseline, proposed in cases():
            good = packet(board, proposed, 95)
            bad_values = [dict(good, lowerbound=True), dict(good, upperbound=True), dict(good, pv=[]),
                          dict(good, depth=True), dict(good, score=chess.engine.PovScore(chess.engine.Mate(2), board.turn)),
                          {"depth": 6}, {"score": good["score"], "pv": good["pv"]},
                          dict(good, pv=[chess.Move.from_uci(baseline)]),
                          dict(good, pv=[chess.Move.from_uci(proposed), chess.Move.from_uci(proposed)])]
            for bad in bad_values:
                with self.subTest(color=board.turn, bad=bad):
                    self.assertFalse(assess(board, baseline, proposed, {6: bad}, bad)["admitted"])
                    self.assertFalse(assess(board, baseline, proposed, {6: bad}, good)["admitted"])
            result = assess(board, baseline, proposed, {6: good}, good, returned=baseline)
            self.assertFalse(result["admitted"])
            self.assertFalse(assess(board, baseline, proposed, {6: good}, good, error="TimeoutError")["admitted"])

    def test_shallow_or_unreliable_baseline_cannot_admit_any_alternative(self):
        board, baseline, proposed = cases()[0]
        candidate = packet(board, proposed, 100, 3)
        baseline_values = [packet(board, baseline, 100, 3), packet(board, baseline, 100, 6, lowerbound=True),
                           {"depth": 6}, packet(board, baseline, 100, 6, pv=[chess.Move.from_uci(proposed)])]
        for value in baseline_values:
            self.assertFalse(assess(board, baseline, proposed, {3: candidate}, candidate, baseline_info=value)["admitted"])

    def test_uniform_seeded_selection_uses_unique_pool_and_is_repeatable(self):
        board = after("e2e4")
        pool = ["c7c5", "e7e5", "e7e6", "g8f6"]
        observed = set()
        for number in range(128):
            seed = f"{number:032x}"
            selected = variety.selected_move(board, seed, pool)
            observed.add(selected)
            self.assertEqual(selected, variety.selected_move(board.copy(stack=True), seed, list(reversed(pool))))
            self.assertEqual(selected, variety.selected_move(board, seed, pool + ["e7e5"] * 25))
        self.assertEqual(observed, set(pool))

    def test_receipt_records_rejected_probes_but_selects_only_baseline_and_qualified_moves(self):
        board = after("e2e4")
        baseline = "c7c5"
        plan = variety.plan(board, 1000, SEED)
        good_uci, bad_uci = variety.proposals(plan, chess.Move.from_uci(baseline))[:2]
        good, bad = packet(board, good_uci, 90), packet(board, bad_uci, 50)
        probes = [assess(board, baseline, good_uci, {6: good}, good),
                  assess(board, baseline, bad_uci, {6: bad}, bad)]
        before = copy.deepcopy(probes), board.fen(), tuple(board.move_stack)
        result = variety.receipt(board, plan, chess.Move.from_uci(baseline), packet(board, baseline), probes, SEED,
                                 baseline_elapsed_ms=1001.5, extra_elapsed_ms=72)
        self.assertEqual(set(result["selection_pool"]), {baseline, good_uci})
        self.assertIn(result["selected_uci"], {baseline, good_uci})
        self.assertNotIn(bad_uci, result["selection_pool"])
        self.assertEqual(result["probes"], probes)
        self.assertEqual(result["baseline_budget_ms"], 1000)
        self.assertEqual(result["extra_budget_ms"], 1000)
        self.assertEqual(result["candidate_budget_ms"], 1000)
        self.assertEqual(result["probe_budget_sum_ms"], 400)
        self.assertEqual(result["extra_elapsed_ms"], 72)
        self.assertEqual(result["loss_cp"], 10 if result["selected_uci"] == good_uci else 0)
        self.assertEqual((probes, board.fen(), tuple(board.move_stack)), before)

    def test_late_failure_or_score_conflict_falls_back_even_after_an_admitted_probe(self):
        board = after("e2e4")
        baseline = "c7c5"
        plan = variety.plan(board, 1000, SEED)
        first, second = variety.proposals(plan, chess.Move.from_uci(baseline))[:2]
        good, conflict = packet(board, first, 90), packet(board, second, 999)
        admitted = assess(board, baseline, first, {6: good}, good)
        for rejected in (assess(board, baseline, second, {6: conflict}, conflict),
                         assess(board, baseline, second, {}, None, error="TimeoutError")):
            result = variety.receipt(board, plan, chess.Move.from_uci(baseline), packet(board, baseline),
                                     [admitted, rejected], SEED)
            self.assertEqual(result["selected_uci"], baseline)
            self.assertEqual(result["selection_pool"], [baseline])
            self.assertEqual(result["reason"], rejected["reason"])
            self.assertEqual(len(result["probes"]), 2)

    def test_sampling_weights_use_full_proposal_count_and_all_completed_comparisons(self):
        board, baseline = after("g1f3", "g8f6", "g2g3", "g7g6", "f1g2", "f8g7"), "e2e4"
        # A broad synthetic catalogue isolates the later-stage weighting rule.
        with patch.object(variety, "candidate_moves", return_value=sorted(move.uci() for move in board.legal_moves)):
            plan = variety.plan(board, 1000, SEED)
        proposed = variety.proposals(plan, chess.Move.from_uci(baseline))
        full_count = len(set(plan["candidate_order"]) - {baseline})
        self.assertGreater(full_count, 6)
        self.assertEqual(len(proposed), 6)
        probes = []
        for uci, score in zip(proposed[:3], (90, 50, 74)):
            value = packet(board, uci, score)
            probes.append(assess(board, baseline, uci, {6: value}, value, budget_ms=350, elapsed_ms=50))
        incomplete = packet(board, proposed[3], 90, depth=5)
        probes.append(assess(board, baseline, proposed[3], {5: incomplete}, incomplete,
                            budget_ms=350, elapsed_ms=50))
        result = variety.receipt(board, plan, chess.Move.from_uci(baseline), packet(board, baseline),
                                 probes, SEED, extra_elapsed_ms=200)
        self.assertEqual(result["proposal_count"], full_count)
        self.assertEqual(result["tested_count"], 3)  # Two quality rejections still completed at the common depth.
        self.assertEqual(result["selection_pool"], sorted((baseline, proposed[0])))
        self.assertEqual(result["selection_weights"], {baseline: 3, proposed[0]: full_count})
        self.assertEqual(result["candidate_budget_ms"], result["extra_budget_ms"])
        self.assertEqual(result["probe_budget_sum_ms"], 1400)
        self.assertGreater(result["probe_budget_sum_ms"], result["extra_budget_ms"])
        self.assertEqual(result["extra_elapsed_ms"], 200)  # Caps are not additive spent time.
        reversed_result = variety.receipt(board, plan, chess.Move.from_uci(baseline), packet(board, baseline),
                                          list(reversed(probes)), SEED, extra_elapsed_ms=200)
        self.assertEqual(result["selection_weights"], reversed_result["selection_weights"])
        self.assertEqual(result["selected_uci"], reversed_result["selected_uci"])

    def test_weighted_selection_is_repeatable_order_independent_and_ignores_duplicate_entries(self):
        board = after("e2e4")
        pool = ["c7c5", "e7e5", "g8f6"]
        weights = {"c7c5": 2, "e7e5": 18, "g8f6": 18}
        original = copy.deepcopy(weights)
        observed = set()
        for number in range(128):
            seed = f"{number:032x}"
            result = variety.selected_move(board, seed, pool, weights)
            observed.add(result)
            self.assertEqual(result, variety.selected_move(board.copy(stack=True), seed, list(reversed(pool)), weights))
            self.assertEqual(result, variety.selected_move(board, seed, pool + ["e7e5"] * 100, weights))
        self.assertEqual(observed, set(pool))
        self.assertEqual(weights, original)
        for invalid in (0, -1, True, 1.5):
            with self.assertRaises(ValueError):
                variety.selected_move(board, SEED, pool, {**weights, "c7c5": invalid})

    def test_legacy_v1_and_v2_pgn_headers_survive_without_accepting_unknown_policy(self):
        for policy in ("regicide-opening-variety-v1", "regicide-opening-variety-v2"):
            self.assertEqual(variety.pgn_headers({"enabled": True, "policy": policy, "seed": SEED}),
                             {"OpeningVarietySeed": SEED, "OpeningVarietyPolicy": policy})
        for policy in ("unknown", "regicide-opening-variety-v99", "v2\nInjected", None, [], {}):
            self.assertEqual(variety.pgn_headers({"enabled": True, "policy": policy, "seed": SEED}), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
