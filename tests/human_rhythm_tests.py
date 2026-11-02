"""Deterministic timing scenarios, not human-response accuracy measurements."""
import copy
from pathlib import Path
import sys
import unittest

import chess
import chess.engine

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import engine_pacing as policy


def board_after(*moves):
    board = chess.Board()
    for move in moves:
        board.push_uci(move)
    return board


def clock(board, left=600000, elapsed=0):
    return {"enabled": True, "status": "running", "running_color": "white" if board.turn else "black",
            "white_ms": left, "black_ms": left, "turn_elapsed_ms": elapsed, "increment_ms": 5000}


def raw(board, moves, cp=30, depth=6, **extra):
    return {"depth": depth, "score": chess.engine.PovScore(chess.engine.Cp(cp), board.turn),
            "pv": [chess.Move.from_uci(move) for move in moves.split()], **extra}


def stable(board, moves, cp=30):
    return {depth: raw(board, moves, cp=cp, depth=depth) for depth in (4, 5, 6)}


def neutral_context(board, seed="repeat", *, complexity=35, left=600000, elapsed=0):
    # Control classification separately from chess legality so timing branches
    # can be compared without a familiar-opening discount masking the effect.
    result = policy.plan(board, clock(board, left, elapsed), seed=seed)
    result.update(clarity_kind="unclassified", complexity_score=complexity, opening_budget=None,
                  position_features={"capture_choices": 1, "checking_choices": 0,
                                     "attacked_king_neighbors": 0, "own_loose_pieces": 0})
    result["allocation"] = {"baseline_ms": 600, "total_search_ms": 1800, "automatic": True}
    return result


def previous(seed="repeat"):
    board = chess.Board()
    info = raw(board, "e2e4 e7e5 g1f3 b8c6")
    return policy.refine(board, neutral_context(board, seed), clock(board),
                         selected_move=chess.Move.from_uci("e2e4"), selected_info=info,
                         baseline_packets=stable(board, "e2e4 e7e5 g1f3 b8c6"), seed=seed)


def refine_current(board, moves="g1f3 b8c6 f1b5", *, seed="repeat", history=None,
                   packets=None, plan=None, left=600000, elapsed=0):
    plan = plan if plan is not None else neutral_context(board, seed, left=left, elapsed=elapsed)
    return policy.refine(board, plan, clock(board, left, elapsed), selected_move=chess.Move.from_uci(moves.split()[0]),
                         selected_info=raw(board, moves), baseline_packets=packets or stable(board, moves),
                         history=history, seed=seed)


class HumanRhythmTests(unittest.TestCase):
    def test_seed_none_remains_neutral_before_and_after_search(self):
        board = chess.Board()
        plan = policy.plan(board, clock(board))
        result = policy.refine(board, plan, clock(board), selected_move=chess.Move.from_uci("e2e4"),
                               selected_info=raw(board, "e2e4 e7e5"), baseline_packets=stable(board, "e2e4 e7e5"))
        self.assertEqual(result["target_total_ms"], plan["recommended_total_ms"] - 1000)
        self.assertEqual(result["refinement"]["kind"], "neutral")

    def test_tempo_is_game_consistent_and_local_beats_vary_reproducibly(self):
        board = chess.Board()
        plans = []
        for _ in range(3):
            for move in ("g1f3", "g8f6", "f3g1", "f6g8"):
                plans.append(policy._rhythm(board, {"complexity": {"score": 35}}, "seed"))
                board.push_uci(move)
        self.assertEqual(len({row[2] for row in plans}), 1)
        self.assertGreater(len({row[3] for row in plans}), 5)
        self.assertEqual(plans[0], policy._rhythm(chess.Board(), {"complexity": {"score": 35}}, "seed"))
        self.assertNotEqual(plans[0][2], policy._rhythm(chess.Board(), {"complexity": {"score": 35}}, "another")[2])
        self.assertTrue(all(.88 <= row[2] <= 1.12 and .88 <= row[3] <= 1.12 for row in plans))

    def test_expected_full_continuation_is_faster_without_mutation(self):
        board = board_after("e2e4", "e7e5")
        plan = neutral_context(board)
        # Exercise a real deliberation allowance, above the fast-turn floor.
        plan.update(recommended_total_ms=10000, variation_factor=1., target_total_ms=9000)
        history = {1: previous()}
        originals = copy.deepcopy((plan, history)), board.fen(), list(board.move_stack)
        result = refine_current(board, plan=plan, history=history)
        self.assertEqual(result["refinement"]["kind"], "expected_continuation")
        self.assertTrue(result["refinement"]["expected_reply"])
        self.assertLess(result["target_total_ms"], plan["target_total_ms"])
        self.assertEqual(result["allocation"], plan["allocation"])
        self.assertIsNot(result["allocation"], plan["allocation"])
        self.assertEqual((plan, history), originals[0])
        self.assertEqual((board.fen(), list(board.move_stack)), originals[1:])

    def test_matching_reply_but_different_selected_move_is_not_a_fast_continuation(self):
        board = board_after("e2e4", "e7e5")
        result = refine_current(board, "f1c4 g8f6 d2d3", history=[previous()])
        self.assertTrue(result["refinement"]["expected_reply"])
        self.assertFalse(result["refinement"]["expected_continuation"])
        self.assertNotEqual(result["refinement"]["kind"], "expected_continuation")

    def test_unexpected_reply_adds_deliberation_but_is_not_a_probability(self):
        board = board_after("e2e4", "c7c5")
        plan = neutral_context(board)
        result = refine_current(board, "g1f3 d7d6 d2d4", plan=plan, history=[previous()])
        self.assertTrue(result["refinement"]["surprise"])
        self.assertGreater(result["target_total_ms"], plan["target_total_ms"])
        self.assertEqual(result["refinement"]["kind"], "unexpected_reply")

    def test_wrong_history_root_branch_own_move_and_legacy_do_not_create_expectation(self):
        board = board_after("e2e4", "e7e5")
        original = previous()
        for key, value in [("policy", "regicide-engine-pacing-v2"), ("root_ply", 2),
                           ("root_fen", "wrong root"), ("before_fen", "wrong position"),
                           ("history_uci", ["d2d4"]), ("selected_uci", "d2d4")]:
            row = copy.deepcopy(original); row[key] = value
            with self.subTest(key=key):
                result = refine_current(board, history=[row])
                self.assertFalse(result["refinement"]["expected_reply"])
                self.assertFalse(result["refinement"]["surprise"])

    def test_illegal_or_incomplete_forecast_is_not_reused(self):
        board = board_after("e2e4", "e7e5")
        for update in ({"pv_uci": ["e2e4", "e7e6", "e2e3"]}, {"depth": 2},
                       {"cp_white": None, "mate_white": 1}, {"pv_uci": ["a9b9"]}):
            row = previous(); row["forecast"].update(update)
            result = refine_current(board, history=[row])
            self.assertFalse(result["refinement"]["expected_reply"])
            self.assertFalse(result["refinement"]["surprise"])

    def test_current_selected_info_must_match_actual_move_and_be_exact(self):
        board = board_after("e2e4", "e7e5")
        plan = neutral_context(board)
        for info in (None, raw(board, "f1c4 g8f6"), raw(board, "g1f3 b8c6", lowerbound=True),
                     raw(board, "g1f3 b8c6", depth=3), raw(board, "g1f3 b8c6", multipv=2)):
            result = policy.refine(board, plan, clock(board), selected_move=chess.Move.from_uci("g1f3"),
                                  selected_info=info, baseline_packets=stable(board, "g1f3 b8c6"), history=[previous()], seed="repeat")
            self.assertIsNone(result["forecast"])
            self.assertEqual(result["target_total_ms"], plan["target_total_ms"])
            self.assertEqual(result["refinement"]["kind"], "no_selected_evidence")

    def test_identical_history_from_another_seed_cannot_create_expectation_or_cooldown(self):
        board = board_after("e2e4", "e7e5")
        old = previous("different-game")
        old["refinement"]["kind"] = "long_deliberation"
        result = refine_current(board, history=[old])
        self.assertFalse(result["refinement"]["expected_reply"])
        self.assertFalse(result["refinement"]["long_think_cooldown"])
        self.assertNotEqual(old["rhythm_identity"], result["rhythm_identity"])

    def test_selected_off_book_move_restores_structural_time_without_reallocating_search(self):
        board = chess.Board()
        plan = policy.plan(board, clock(board), seed="repeat")
        # Isolate the structural-time fallback. Real opening caps are covered
        # separately in opening_pacing_tests.py, including off-book choices.
        plan["opening_budget"] = None
        plan["allocation"] = {"baseline_ms": 400, "total_search_ms": 1200}
        book = refine_current(board, "e2e4 e7e5 g1f3", plan=plan)
        off_book = refine_current(board, "h2h4 h7h6 a2a3", plan=plan)
        self.assertEqual(book["refinement"]["kind"], "quick_familiar_opening")
        self.assertEqual(off_book["refinement"]["kind"], "unfamiliar_continuation")
        self.assertGreater(off_book["refinement"]["time_basis_ms"], plan["recommended_total_ms"])
        self.assertGreater(off_book["target_total_ms"], book["target_total_ms"])
        self.assertEqual(off_book["recommended_total_ms"], plan["recommended_total_ms"])
        self.assertEqual(off_book["allocation"], plan["allocation"])

    def test_even_known_selected_opening_is_reconsidered_when_latest_search_is_unsettled(self):
        board = chess.Board()
        plan = policy.plan(board, clock(board), seed="repeat")
        packets = stable(board, "e2e4 e7e5"); packets[7] = raw(board, "e2e4 e7e5", depth=7, lowerbound=True)
        result = refine_current(board, "e2e4 e7e5", plan=plan, packets=packets)
        self.assertEqual(result["refinement"]["kind"], "reconsidering_choices")
        self.assertGreater(result["refinement"]["time_basis_ms"], plan["recommended_total_ms"])

    def test_missing_selected_evidence_does_not_claim_a_nonselected_obvious_tactic(self):
        for board, move, expected in [(board_after("f2f3", "e7e5", "g2g4"), "d7d6", "mate_in_one"),
                                     (chess.Board("6k1/5ppp/8/8/3q4/8/3R4/5K2 w - - 0 30"), "f1g2", "clear_queen_gain")]:
            with self.subTest(expected=expected):
                plan = policy.plan(board, clock(board), seed="repeat")
                self.assertEqual(plan["clarity_kind"], expected)
                result = policy.refine(board, plan, clock(board), selected_move=chess.Move.from_uci(move), seed="repeat")
                self.assertEqual(result["rhythm_profile"], "unverified_alternative")
                self.assertEqual(result["target_total_ms"], plan["target_total_ms"])
                self.assertIsNone(result["forecast"])

    def test_queen_declining_alternative_does_not_keep_the_obvious_capture_cap(self):
        board = chess.Board("6k1/5ppp/8/8/3q4/8/3R4/5K2 w - - 0 30")
        plan = policy.plan(board, clock(board), seed="repeat")
        result = refine_current(board, "f1g2 d4e4", plan=plan)
        self.assertEqual(result["refinement"]["kind"], "alternative_to_obvious_move")
        self.assertGreater(result["refinement"]["time_basis_ms"], plan["recommended_total_ms"])
        self.assertGreater(result["target_total_ms"], plan["target_total_ms"])

    def test_changed_or_bounded_latest_search_prevents_premature_fast_continuation(self):
        board = board_after("e2e4", "e7e5")
        for newest in (raw(board, "g1f3 b8c6", depth=7, lowerbound=True), raw(board, "f1c4 g8f6", depth=7)):
            packets = stable(board, "g1f3 b8c6"); packets[7] = newest
            result = refine_current(board, history=[previous()], packets=packets)
            self.assertTrue(result["refinement"]["expected_continuation"])
            self.assertTrue(result["refinement"]["search_evidence"]["unsettled"])
            self.assertNotEqual(result["refinement"]["kind"], "expected_continuation")
            self.assertGreater(result["refinement"]["factor"], 1)

    def test_alternative_cannot_borrow_the_baselines_stability(self):
        board = board_after("e2e4", "e7e5")
        result = refine_current(board, history=[previous()], packets=stable(board, "f1c4 g8f6"))
        self.assertFalse(result["refinement"]["search_evidence"]["stable_selected_root"])
        self.assertNotEqual(result["refinement"]["kind"], "expected_continuation")

    def test_simple_stable_recapture_is_quick_but_danger_or_reconsideration_is_not(self):
        board = board_after("e2e4", "d7d5", "e4d5")
        moves = "d8d5 b1c3 d5d8"
        plan = neutral_context(board)
        result = refine_current(board, moves, plan=plan)
        self.assertEqual(result["refinement"]["kind"], "simple_recapture")
        dangerous = copy.deepcopy(plan); dangerous["position_features"]["own_pinned_pieces"] = 1
        self.assertNotEqual(refine_current(board, moves, plan=dangerous)["refinement"]["kind"], "simple_recapture")
        packets = stable(board, moves); packets[7] = raw(board, moves, cp=-250, depth=7)
        self.assertNotEqual(refine_current(board, moves, plan=plan, packets=packets)["refinement"]["kind"], "simple_recapture")

    def test_obvious_mate_available_but_different_selected_move_is_not_called_quick_mate(self):
        board = board_after("f2f3", "e7e5", "g2g4")
        plan = policy.plan(board, clock(board), seed="repeat")
        self.assertEqual(plan["clarity_kind"], "mate_in_one")
        result = refine_current(board, "d7d6 b1c3 g8f6", plan=plan)
        self.assertEqual(result["refinement"]["kind"], "alternative_to_obvious_move")
        self.assertGreater(result["refinement"]["factor"], 1)
        self.assertGreater(result["refinement"]["time_basis_ms"], plan["recommended_total_ms"])
        self.assertGreater(result["target_total_ms"], plan["target_total_ms"])

    def test_actual_mate_keeps_obvious_quick_profile_and_signed_forecast(self):
        board = board_after("f2f3", "e7e5", "g2g4")
        plan = policy.plan(board, clock(board), seed="repeat")
        info = raw(board, "d8h4", score=chess.engine.PovScore(chess.engine.Mate(1), chess.BLACK))
        result = policy.refine(board, plan, clock(board), selected_move=chess.Move.from_uci("d8h4"),
                               selected_info=info, baseline_packets={4: {**info,"depth":4}, 5:{**info,"depth":5}, 6:info}, seed="repeat")
        self.assertEqual(result["refinement"]["kind"], "quick_mate_in_one")
        self.assertEqual(result["forecast"]["mate_white"], -1)

    def test_occasional_long_decisions_have_branch_validated_cooldown(self):
        board = board_after("e2e4", "c7c5")
        events = []
        for number in range(100):
            seed = str(number)
            plan = neutral_context(board, seed, complexity=75)
            result = refine_current(board, "g1f3 d7d6 d2d4", seed=seed, plan=plan, history=[previous(seed)])
            if result["refinement"]["kind"] == "long_deliberation":
                events.append((seed, plan, result))
        self.assertGreater(len(events), 0); self.assertLess(len(events), 40)
        seed, plan, result = events[0]
        row = previous(seed); row["refinement"]["kind"] = "long_deliberation"
        row.update(completion_reason="target_reached", spent_before_wait_ms=1000, waited_ms=9000,
                   target_total_ms=10000)
        cooled = refine_current(board, "g1f3 d7d6 d2d4", seed=seed, plan=plan, history=[row])
        self.assertTrue(cooled["refinement"]["long_think_cooldown"])
        self.assertNotEqual(cooled["refinement"]["kind"], "long_deliberation")
        self.assertLess(cooled["target_total_ms"], result["target_total_ms"])
        row["history_uci"] = ["d2d4"]
        self.assertFalse(refine_current(board, "g1f3 d7d6 d2d4", seed=seed, plan=plan, history=[row])["refinement"]["long_think_cooldown"])

    def test_only_completed_and_actually_long_receipts_trigger_cooldown(self):
        board = board_after("e2e4", "c7c5")
        seed = "23"
        plan = neutral_context(board, seed, complexity=75)
        for completion, spent, waited in [("client_closing", 200, 0), ("target_reached", 200, 0),
                                           ("low_clock", 10000, 0), ("target_reached", 1000, 0)]:
            row = previous(seed); row["refinement"]["kind"] = "long_deliberation"
            row.update(completion_reason=completion, spent_before_wait_ms=spent, waited_ms=waited,
                       target_total_ms=10000)
            result = refine_current(board, "g1f3 d7d6 d2d4", seed=seed, plan=plan, history=[row])
            self.assertFalse(result["refinement"]["long_think_cooldown"])
        row.update(completion_reason="target_reached", spent_before_wait_ms=10000, waited_ms=0)
        result = refine_current(board, "g1f3 d7d6 d2d4", seed=seed, plan=plan, history=[row])
        self.assertTrue(result["refinement"]["long_think_cooldown"])

    def test_six_minute_pressure_shrinks_premiums_and_total_targets_monotonically(self):
        board = board_after("e2e4", "c7c5")
        values = []
        for left in (360001, 360000, 359999, 240000, 180000, 120000, 60000, 30000, 10000):
            plan = neutral_context(board, left=left)
            result = refine_current(board, "g1f3 d7d6 d2d4", plan=plan, left=left, history=[previous()])
            values.append(result)
            self.assertLessEqual(result["target_total_ms"], 45000)
            self.assertLessEqual(result["target_total_ms"], max(0,left-result["reserve_ms"]))
            self.assertEqual(result["allocation"], plan["allocation"])
        targets = [row["target_total_ms"] for row in values]
        factors = [row["refinement"]["factor"] for row in values]
        self.assertEqual(targets, sorted(targets, reverse=True))
        self.assertEqual(factors, sorted(factors, reverse=True))
        self.assertFalse(values[-1]["enabled"])
        self.assertEqual(policy.remaining_wait_ms(values[-1], clock(board, 10000)), 0)

    def test_fresh_clock_and_elapsed_are_limits_not_extra_delay(self):
        board = board_after("e2e4", "c7c5")
        plan = neutral_context(board)
        result = refine_current(board, "g1f3 d7d6 d2d4", plan=plan, left=31000, elapsed=20000, history=[previous()])
        self.assertLessEqual(result["target_total_ms"], 20000 + max(0,31000-result["reserve_ms"]))
        self.assertEqual(policy.remaining_wait_ms(result, clock(board,31000,20000)), 0)
        self.assertEqual(result["allocation"], plan["allocation"])

    def test_wrong_position_or_illegal_selected_move_cannot_refine(self):
        board = board_after("e2e4", "e7e5")
        plan = neutral_context(board)
        wrong = copy.deepcopy(plan); wrong["before_fen"] = "wrong"
        self.assertEqual(refine_current(board, plan=wrong), wrong)
        result = policy.refine(board, plan, clock(board), selected_move=chess.Move.from_uci("e2e5"), seed="repeat")
        self.assertEqual(result["refinement"]["kind"], "invalid_selected_move")
        self.assertEqual(result["target_total_ms"], plan["target_total_ms"])

    def test_black_expected_continuation_uses_actual_branch_and_white_score(self):
        earlier = board_after("e2e4")
        old = policy.refine(earlier, neutral_context(earlier), clock(earlier), selected_move=chess.Move.from_uci("e7e5"),
            selected_info=raw(earlier,"e7e5 g1f3 b8c6"), baseline_packets=stable(earlier,"e7e5 g1f3 b8c6"),seed="repeat")
        board = board_after("e2e4", "e7e5", "g1f3")
        result = refine_current(board,"b8c6 f1b5 a7a6",history=[old])
        self.assertEqual(result["refinement"]["kind"],"expected_continuation")
        self.assertEqual(result["forecast"]["cp_white"],-30)


if __name__ == "__main__":
    unittest.main()
