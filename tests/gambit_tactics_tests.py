"""Own-search tactical selection contracts; these are not site grade tests."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import chess
import chess.engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import gambit_tactics as tactics


def packet(board, uci, cp=20, depth=4, mate=None, **extra):
    move = chess.Move.from_uci(uci)
    after = board.copy(stack=True)
    after.push(move)
    pv = [move]
    if after.legal_moves.count():
        pv.append(next(iter(after.legal_moves)))
    score = chess.engine.Mate(mate) if mate is not None else chess.engine.Cp(cp)
    return {"depth": depth, "score": chess.engine.PovScore(score, board.turn), "pv": pv, **extra}


class PassiveTimer:
    def __init__(self, *_): pass
    def start(self): pass
    def cancel(self): pass
    def join(self): pass


class Clock:
    def __init__(self): self.now = 100.0
    def read(self): return self.now
    def advance(self, milliseconds): self.now += milliseconds / 1000


class TacticalTests(unittest.TestCase):
    def setUp(self):
        self.board = chess.Board("k5r1/5p2/8/8/5N2/1B6/8/7K w - - 0 1")
        self.base = chess.Move.from_uci("f4d5")
        self.candidate = chess.Move.from_uci("f4g6")
        self.base_info = packet(self.board, self.base.uci())
        self.calls, self.closed = [], []

    def run_choice(self, *, candidate=None, geometry=True, candidate_cp=20, final_cp=20,
                   pair_winner=None, pair_common_move=None, pair_common_cp=None, mutate=None,
                   budget_ms=775, baseline_info=None, baseline_packets=None):
        selected = candidate or self.candidate
        winner = pair_winner or selected
        common_move = pair_common_move or winner
        def evidence(board, move, **kwargs):
            eligible = geometry and move == selected
            return {"eligible": eligible, "complete": True, "offers": [], "investment_cp": 300 if eligible else 0}
        def search(amount, **kwargs):
            self.calls.append((amount, kwargs))
            index = len(self.calls)
            if index == 1:
                self.assertIn(selected, kwargs["root_moves"])
                line = packet(self.board, selected.uci(), candidate_cp)
                response = (chess.engine.BestMove(selected, None), line, {4: line})
            elif index == 2:
                self.assertEqual(kwargs["root_moves"], list(dict.fromkeys([selected, self.base])))
                self.assertNotIn("root_move", kwargs)
                common = packet(self.board, common_move.uci(), candidate_cp if pair_common_cp is None else pair_common_cp)
                final = packet(self.board, winner.uci(), final_cp, depth=5)
                response = (chess.engine.BestMove(winner, None), final, {4: common, 5: final})
            else:
                self.fail("v4 must not launch an all-other-roots third search")
            return mutate(index, response) if mutate else response
        base_info = self.base_info if baseline_info is None else baseline_info
        packets = {4: base_info} if baseline_packets is None else baseline_packets
        with patch.object(tactics.sound_sacrifice, "evidence", side_effect=evidence):
            return tactics.choose(self.board, self.base, base_info, packets, search, lambda: self.closed.append(True),
                                  budget_ms=budget_ms)

    def assert_preserved(self, result):
        self.assertFalse(result["qualified"] or result["quality_qualified"] or result["applied"])
        self.assertEqual(result["selected_uci"], self.base.uci())

    def test_two_searches_require_candidate_to_win_the_deeper_finalist_comparison(self):
        before = self.board.fen(), list(self.board.move_stack)
        result, discard = self.run_choice()
        self.assertFalse(discard)
        self.assertTrue(result["applied"] and result["qualified"] and result["quality_qualified"])
        self.assertEqual(result["selected_uci"], self.candidate.uci())
        self.assertEqual(result["kind"], "sound_sacrifice_candidate")
        self.assertEqual(result["kinds"], ["sound_sacrifice_candidate"])
        self.assertEqual(result["policy"], "regicide-gambit-tactics-v4")
        self.assertEqual(result["classification_scope"], "local_native_verification_not_chesscom_grade")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual([options["depth"] for _, options in self.calls], [4, 5])
        self.assertTrue(all(options["fresh"] and not options["publish"] for _, options in self.calls))
        self.assertTrue(all(row["requested_ms"] <= row["deadline_remaining_before_ms"] for row in result["attempts"]))
        evidence = result["evidence"]
        self.assertEqual(evidence["candidate_final"]["depth"], 5)
        self.assertEqual(evidence["candidate_compared"]["depth"], 4)
        self.assertEqual(evidence["paired_verification"], {
            "depth": 5, "roots_uci": [self.candidate.uci(), self.base.uci()], "winner_uci": self.candidate.uci(),
            "baseline_included": True, "comparison": "native_best_of_finalists", "global_best_proven": False})
        self.assertEqual(before, (self.board.fen(), list(self.board.move_stack)))

    def test_baseline_can_qualify_without_replacement_or_duplicate_finalists(self):
        result, discard = self.run_choice(candidate=self.base)
        self.assertFalse(discard)
        self.assertTrue(result["qualified"] and result["quality_qualified"])
        self.assertFalse(result["applied"])
        self.assertEqual(result["selected_uci"], self.base.uci())
        self.assertEqual(self.calls[1][1]["root_moves"], [self.base])
        self.assertEqual(result["evidence"]["paired_verification"]["roots_uci"], [self.base.uci()])

    def test_quiet_baseline_does_not_hide_a_near_best_sacrifice_in_the_first_search(self):
        baseline = packet(self.board, self.base.uci(), cp=30)
        result, discard = self.run_choice(baseline_info=baseline, candidate_cp=10, final_cp=5)
        self.assertFalse(discard)
        self.assertTrue(result["applied"] and result["qualified"])
        self.assertEqual(result["candidate_group"], "sacrifice")
        self.assertEqual(self.calls[0][1]["root_moves"], [self.candidate])
        self.assertNotIn(self.base, self.calls[0][1]["root_moves"])
        self.assertEqual(result["loss_cp"], 20)

    def test_nonsacrifice_forcing_candidates_need_no_critical_alternative_claim(self):
        candidate = chess.Move.from_uci("b3f7")
        result, discard = self.run_choice(candidate=candidate, geometry=False)
        self.assertFalse(discard)
        self.assertTrue(result["qualified"] and result["quality_qualified"])
        self.assertEqual(result["kind"], "verified_forcing_candidate")
        self.assertEqual(result["kinds"], ["verified_forcing_candidate"])
        self.assertNotIn(self.base, self.calls[0][1]["root_moves"])
        self.assertTrue(all(self.board.is_capture(move) or self.board.gives_check(move)
                            for move in self.calls[0][1]["root_moves"]))
        self.assertEqual(len(self.calls), 2)
        self.assertNotIn("alternative", result["evidence"])

    def test_already_winning_position_can_receive_local_quality_verification(self):
        result, _ = self.run_choice(baseline_info=packet(self.board, self.base.uci(), cp=500),
                                    candidate_cp=490, final_cp=450)
        self.assertTrue(result["qualified"] and result["applied"])
        self.assertEqual(len(self.calls), 2)
        self.assertNotIn("already_winning_alternative_cp", result["thresholds"])
        self.assertNotIn("critical_alternative_cp_max", result["thresholds"])
        self.assertFalse(result["evidence"]["paired_verification"]["global_best_proven"])
        self.assertEqual(result["classification_scope"], "local_native_verification_not_chesscom_grade")

    def test_deeper_preference_reversal_preserves_baseline_even_at_an_equal_positive_score(self):
        for final_cp in (20, 80):
            with self.subTest(final_cp=final_cp):
                self.calls = []
                result, discard = self.run_choice(pair_winner=self.base, final_cp=final_cp)
                self.assertFalse(discard)
                self.assert_preserved(result)
                self.assertEqual(result["reason"], "paired_verification_prefers_baseline")
                self.assertEqual(len(self.calls), 2)

    def test_pair_common_depth_winner_may_be_baseline_when_candidate_wins_deeper(self):
        result, discard = self.run_choice(pair_common_move=self.base, pair_common_cp=400, final_cp=15)
        self.assertFalse(discard)
        self.assertTrue(result["qualified"])
        self.assertEqual(result["evidence"]["candidate_compared"]["uci"], self.candidate.uci())
        self.assertEqual(result["evidence"]["candidate_compared"]["cp"], 20)
        self.assertEqual(result["evidence"]["deeper_delta_from_common"], -5)

    def test_pair_common_depth_packet_is_diagnostic_not_a_second_quality_gate(self):
        for common in (None, "bound", "lower_score"):
            with self.subTest(common=common):
                self.calls = []
                def mutate(index, response):
                    if index != 2: return response
                    reply, line, packets = response
                    packets = dict(packets)
                    if common is None: packets.pop(4)
                    elif common == "bound": packets[4] = {**packets[4], "lowerbound": True}
                    else: packets[4] = packet(self.board, self.base.uci(), cp=-500)
                    return reply, line, packets
                result, discard = self.run_choice(mutate=mutate)
                self.assertFalse(discard)
                self.assertTrue(result["qualified"])
                self.assertEqual(result["evidence"]["candidate_compared"]["cp"], 20)

    def test_first_search_nonnegative_floor_and_25cp_concession_cap_still_apply(self):
        for cp in (-1, 4):
            with self.subTest(cp=cp):
                self.calls = []
                result, discard = self.run_choice(baseline_info=packet(self.board, self.base.uci(), cp=30), candidate_cp=cp)
                self.assertFalse(discard)
                self.assert_preserved(result)
                self.assertEqual(len(self.calls), 1)
        self.calls = []
        result, _ = self.run_choice(baseline_info=packet(self.board, self.base.uci(), cp=30), candidate_cp=5, final_cp=0)
        self.assertTrue(result["qualified"])
        self.assertEqual(result["loss_cp"], 25)

    def test_deeper_candidate_winner_must_remain_nonnegative(self):
        result, discard = self.run_choice(final_cp=-1)
        self.assertFalse(discard)
        self.assert_preserved(result)
        self.assertEqual(result["reason"], "paired_verification_outside_floor")

    def test_deeper_score_is_not_compared_with_a_shallower_baseline(self):
        result, discard = self.run_choice(candidate=self.base, baseline_info=packet(self.board, self.base.uci(), cp=162),
                                          candidate_cp=162, final_cp=122)
        self.assertFalse(discard)
        self.assertTrue(result["qualified"])
        self.assertEqual(result["loss_cp"], 0)
        self.assertEqual(result["evidence"]["deeper_delta_from_common"], -40)

    def test_loss_receipt_uses_candidate_group_score_not_the_pair_lower_depth_winner(self):
        result, _ = self.run_choice(baseline_info=packet(self.board, self.base.uci(), cp=50), candidate_cp=40,
                                    pair_common_move=self.base, pair_common_cp=30, final_cp=0)
        self.assertTrue(result["qualified"])
        self.assertEqual(result["loss_cp"], 10)
        self.assertEqual(result["evidence"]["candidate_compared"]["cp"], 40)
        self.assertEqual(result["evidence"]["candidate_compared"]["depth"], 4)
        self.assertEqual(result["evidence"]["deeper_delta_from_common"], -40)

    def test_better_discovered_score_is_allowed_after_paired_confirmation(self):
        result, _ = self.run_choice(candidate_cp=400, final_cp=300)
        self.assertTrue(result["qualified"])
        self.assertEqual(result["loss_cp"], 0)

    def test_first_search_unreliable_evidence_fails_closed(self):
        changes = [lambda line: {**line, "lowerbound": True}, lambda line: {**line, "upperbound": True},
                   lambda line: {**line, "multipv": 2},
                   lambda line: {**line, "score": chess.engine.PovScore(chess.engine.Cp(20), chess.BLACK)},
                   lambda line: {**line, "pv": line["pv"][:1]}, lambda line: {**line, "depth": 3}]
        for change in changes:
            self.calls = []
            def mutate(index, response):
                if index != 1: return response
                reply, line, _ = response
                changed = change(line)
                return reply, changed, {4: changed}
            result, _ = self.run_choice(mutate=mutate)
            self.assert_preserved(result)
            self.assertEqual(len(self.calls), 1)

    def test_pair_requires_exact_legal_matching_d_plus_one_evidence(self):
        changes = [lambda line: {**line, "lowerbound": True}, lambda line: {**line, "upperbound": True},
                   lambda line: {**line, "multipv": 2},
                   lambda line: {**line, "score": chess.engine.PovScore(chess.engine.Cp(20), chess.BLACK)},
                   lambda line: {**line, "pv": line["pv"][:1]},
                   lambda line: {**line, "pv": [line["pv"][0], chess.Move.from_uci("a1a8")]},
                   lambda line: {**line, "depth": 4}, lambda line: {**line, "depth": 6}]
        for index, change in enumerate(changes):
            with self.subTest(change=index):
                self.calls = []
                before = self.board.fen(), list(self.board.move_stack)
                def mutate(stage, response):
                    if stage != 2: return response
                    reply, line, packets = response
                    changed = change(line)
                    return reply, changed, {**packets, 5: changed}
                result, discard = self.run_choice(mutate=mutate)
                self.assertFalse(discard)
                self.assert_preserved(result)
                self.assertEqual(result["reason"], "paired_verification_incomplete_or_unreliable")
                self.assertEqual(before, (self.board.fen(), list(self.board.move_stack)))

    def test_deeper_mate_scores_remain_on_the_baseline_path_without_cp_conversion(self):
        for mate in (-2, 2):
            with self.subTest(mate=mate):
                self.calls = []
                before = self.board.fen(), list(self.board.move_stack)
                def mutate(stage, response):
                    if stage != 2: return response
                    reply, line, packets = response
                    changed = {**line, "score": chess.engine.PovScore(chess.engine.Mate(mate), self.board.turn)}
                    return reply, changed, {**packets, 5: changed}
                result, discard = self.run_choice(mutate=mutate)
                self.assertFalse(discard)
                self.assert_preserved(result)
                self.assertEqual(result["reason"], "paired_verification_outside_floor")
                self.assertEqual(before, (self.board.fen(), list(self.board.move_stack)))

    def test_pair_missing_conflicting_or_outside_root_packets_preserve_baseline(self):
        for error in ("missing", "conflicting_score", "conflicting_root", "outside_pair", "reply_mismatch"):
            with self.subTest(error=error):
                self.calls = []
                def mutate(index, response):
                    if index != 2: return response
                    reply, line, packets = response
                    if error == "missing": return reply, line, {4: packets[4]}
                    if error == "conflicting_score":
                        return reply, line, {**packets, 5: packet(self.board, self.candidate.uci(), cp=21, depth=5)}
                    if error == "conflicting_root":
                        return reply, line, {**packets, 5: packet(self.board, self.base.uci(), depth=5)}
                    if error == "reply_mismatch": return chess.engine.BestMove(self.base, None), line, packets
                    outside = chess.Move.from_uci("b3f7")
                    changed = packet(self.board, outside.uci(), depth=5)
                    return chess.engine.BestMove(outside, None), changed, {4: packets[4], 5: changed}
                result, discard = self.run_choice(mutate=mutate)
                self.assertFalse(discard)
                self.assert_preserved(result)
                self.assertEqual(result["reason"], "paired_verification_incomplete_or_unreliable")

    def test_mate_baseline_packet_conflict_and_invalid_budgets_do_not_search(self):
        for baseline in (packet(self.board, self.base.uci(), mate=2), packet(self.board, self.base.uci(), mate=-2)):
            result, _ = self.run_choice(baseline_info=baseline)
            self.assertEqual(result["reason"], "protected_mate")
        result, _ = self.run_choice(baseline_packets={})
        self.assertEqual(result["reason"], "baseline_packet_conflict")
        for budget in (0, 99, True, float("nan"), float("inf")):
            result, _ = self.run_choice(budget_ms=budget)
            self.assertEqual(result["reason"], "short_or_invalid_budget")
        self.assertEqual(self.calls, [])

    def test_callback_exception_discards_engine_and_preserves_baseline(self):
        for failed_stage in (1, 2):
            with self.subTest(stage=failed_stage):
                self.calls = []
                def fail(index, response):
                    if index == failed_stage: raise RuntimeError("search failed")
                    return response
                result, discard = self.run_choice(mutate=fail)
                self.assertTrue(discard)
                self.assert_preserved(result)

    def test_deadline_callback_cannot_leave_a_completed_candidate_applied(self):
        class RunningTimer(PassiveTimer):
            def __init__(self, seconds, callback): self.callback = callback
            def join(self): self.callback()
        with patch.object(tactics.threading, "Timer", RunningTimer):
            result, discard = self.run_choice()
        self.assertTrue(discard)
        self.assertEqual(result["reason"], "tactical_probe_timeout")
        self.assert_preserved(result)

    def test_quiet_initial_position_does_not_spend_native_search_budget(self):
        board = chess.Board()
        move = chess.Move.from_uci("e2e4")
        line = packet(board, move.uci())
        calls = []
        result, _ = tactics.choose(board, move, line, {4: line}, lambda *a, **k: calls.append(a), lambda: None, budget_ms=775)
        self.assertEqual(result["reason"], "no_tactical_candidates")
        self.assertEqual(calls, [])

    def test_budget_hard_cap_and_candidate_set_cap(self):
        result, _ = self.run_choice(budget_ms=10_000)
        self.assertEqual(result["budget_ms"], 1000)
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(all(amount <= 1000 for amount, _ in self.calls))
        self.assertTrue(all(row["requested_ms"] <= row["deadline_remaining_before_ms"] for row in result["attempts"]))
        self.assertLessEqual(len(self.calls[0][1]["root_moves"]), 8)

    def test_early_completed_depth_returns_all_remaining_time_to_the_finalist_pair(self):
        clock = Clock()
        def early(index, response):
            clock.advance({1: 50, 2: 60}[index])
            return response
        with patch.object(tactics.time, "monotonic", side_effect=clock.read), patch.object(tactics.threading, "Timer", PassiveTimer):
            result, discard = self.run_choice(mutate=early)
        self.assertFalse(discard)
        self.assertTrue(result["qualified"])
        self.assertEqual(len(self.calls), 2)
        self.assertAlmostEqual(self.calls[0][0], 775 * .32, places=5)
        self.assertAlmostEqual(self.calls[1][0], 775 - 50 - 2, places=5)
        self.assertGreater(result["search_requested_ms"], result["budget_ms"])
        self.assertEqual(result["search_elapsed_ms"], 110)
        self.assertEqual(result["extra_elapsed_ms"], 110)
        self.assertTrue(all(row["requested_ms"] <= row["deadline_remaining_before_ms"] for row in result["attempts"]))

    def test_a_complete_but_late_pair_cannot_escape_the_whole_turn_deadline(self):
        clock = Clock()
        def late(index, response):
            clock.advance(100 if index == 1 else 800)
            return response
        with patch.object(tactics.time, "monotonic", side_effect=clock.read), patch.object(tactics.threading, "Timer", PassiveTimer):
            result, discard = self.run_choice(mutate=late)
        self.assertTrue(discard)
        self.assert_preserved(result)
        self.assertEqual(result["reason"], "tactical_probe_timeout")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(result["search_elapsed_ms"], 900)

    def test_full_standard_start_history_is_unchanged_by_both_searches(self):
        self.board = chess.Board()
        for move in ("e2e4", "e7e5", "g1f3", "d7d6", "f1c4", "c8g4", "b1c3", "g7g6"):
            self.board.push_uci(move)
        self.base, self.candidate = chess.Move.from_uci("e1g1"), chess.Move.from_uci("f3e5")
        self.base_info = packet(self.board, self.base.uci())
        before = self.board.fen(en_passant="fen"), list(self.board.move_stack)
        result, discard = self.run_choice()
        self.assertFalse(discard)
        self.assertTrue(result["qualified"])
        self.assertEqual(before, (self.board.fen(en_passant="fen"), list(self.board.move_stack)))


if __name__ == "__main__":
    unittest.main()
