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


class TacticalTests(unittest.TestCase):
    def setUp(self):
        self.board = chess.Board("k5r1/5p2/8/8/5N2/1B6/8/7K w - - 0 1")
        self.base = chess.Move.from_uci("f4d5")
        self.candidate = chess.Move.from_uci("f4g6")
        self.base_info = packet(self.board, self.base.uci())
        self.calls, self.closed = [], []

    def run_choice(self, *, candidate=None, geometry=True, candidate_cp=20, final_cp=20, confirmation_cp=None,
                   alternative_cp=100, alternative_mate=None, mutate=None, budget_ms=775,
                   baseline_info=None, baseline_packets=None):
        selected = candidate or self.candidate
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
                self.assertEqual(kwargs["root_move"], selected)
                common = packet(self.board, selected.uci(), candidate_cp if confirmation_cp is None else confirmation_cp)
                final = packet(self.board, selected.uci(), final_cp, depth=5)
                response = (chess.engine.BestMove(selected, None), final, {4: common, 5: final})
            else:
                roots = kwargs["root_moves"]
                self.assertEqual(set(roots), set(self.board.legal_moves) - {selected})
                other = self.base if self.base != selected else roots[0]
                final = packet(self.board, other.uci(), alternative_cp, mate=alternative_mate)
                response = (chess.engine.BestMove(other, None), final, {4: final})
            return mutate(index, response) if mutate else response
        base_info = self.base_info if baseline_info is None else baseline_info
        packets = {4: base_info} if baseline_packets is None else baseline_packets
        with patch.object(tactics.sound_sacrifice, "evidence", side_effect=evidence):
            return tactics.choose(self.board, self.base, base_info, packets, search, lambda: self.closed.append(True),
                                  budget_ms=budget_ms)

    def test_three_probes_qualify_a_new_investment_with_deeper_and_all_alternative_checks(self):
        before = self.board.fen(), list(self.board.move_stack)
        result, discard = self.run_choice()
        self.assertFalse(discard)
        self.assertTrue(result["applied"] and result["qualified"])
        self.assertEqual(result["selected_uci"], self.candidate.uci())
        self.assertEqual(result["kind"], "sound_sacrifice_candidate")
        self.assertEqual(len(self.calls), 3)
        self.assertEqual([options["depth"] for _, options in self.calls], [4, 5, 4])
        self.assertTrue(all(options["fresh"] and not options["publish"] for _, options in self.calls))
        self.assertTrue(all(row["requested_ms"] <= row["deadline_remaining_before_ms"]
                            for row in result["attempts"]))
        self.assertEqual(result["evidence"]["candidate_final"]["depth"], 5)
        self.assertEqual(result["evidence"]["candidate_compared"]["depth"], 4)
        self.assertEqual(before, (self.board.fen(), list(self.board.move_stack)))

    def test_baseline_can_be_qualified_without_replacing_it(self):
        result, _ = self.run_choice(candidate=self.base)
        self.assertTrue(result["qualified"])
        self.assertFalse(result["applied"])
        self.assertEqual(result["selected_uci"], self.base.uci())

    def test_quiet_baseline_does_not_hide_a_near_best_sacrifice(self):
        baseline = packet(self.board, self.base.uci(), cp=30)
        result, discard = self.run_choice(baseline_info=baseline, candidate_cp=10, final_cp=5, alternative_cp=30)
        self.assertFalse(discard)
        self.assertTrue(result["applied"] and result["qualified"])
        self.assertEqual(result["candidate_group"], "sacrifice")
        self.assertEqual(self.calls[0][1]["root_moves"], [self.candidate])
        self.assertNotIn(self.base, self.calls[0][1]["root_moves"])
        self.assertEqual(result["loss_cp"], 20)

    def test_forcing_group_is_used_only_when_no_complete_sacrifice_is_available(self):
        candidate = chess.Move.from_uci("b3f7")
        result, _ = self.run_choice(candidate=candidate, geometry=False, alternative_cp=-250)
        self.assertEqual(result["candidate_group"], "forcing_critical")
        self.assertNotIn(self.base, self.calls[0][1]["root_moves"])
        self.assertTrue(all(self.board.is_capture(move) or self.board.gives_check(move)
                            for move in self.calls[0][1]["root_moves"]))

    def test_non_sacrifice_forcing_move_can_be_a_critical_opportunity(self):
        candidate = chess.Move.from_uci("b3f7")
        result, _ = self.run_choice(candidate=candidate, geometry=False, alternative_cp=-200)
        self.assertTrue(result["qualified"])
        self.assertEqual(result["kind"], "critical_opportunity")

    def test_winning_elsewhere_excludes_gratuitous_sacrifice(self):
        for kwargs in ({"alternative_cp": 250}, {"alternative_mate": 2}):
            self.calls = []
            result, _ = self.run_choice(**kwargs)
            self.assertFalse(result["qualified"] or result["applied"])
            self.assertEqual(result["reason"], "already_winning_without_sacrifice")

    def test_losing_alternative_mate_is_preserved_as_mate_not_fake_cp(self):
        result, _ = self.run_choice(alternative_mate=-2)
        self.assertIn("critical_opportunity", result["kinds"])
        self.assertIsNone(result["evidence"]["alternative"]["cp"])
        self.assertEqual(result["evidence"]["alternative"]["mate"], -2)

    def test_nonnegative_floor_and_same_depth_concession_caps_are_enforced(self):
        for changes in ({"candidate_cp": -1}, {"final_cp": -1}, {"candidate_cp": 4}, {"confirmation_cp": 4}):
            self.calls = []
            # Baseline 30 makes candidate 4 a 26cp concession.
            baseline = packet(self.board, self.base.uci(), cp=30)
            result, _ = self.run_choice(baseline_info=baseline, **changes)
            self.assertFalse(result["qualified"] or result["applied"])

    def test_deeper_nonnegative_score_is_not_compared_with_shallower_baseline(self):
        baseline = packet(self.board, self.base.uci(), cp=162)
        result, discard = self.run_choice(candidate=self.base, baseline_info=baseline,
                                          candidate_cp=162, final_cp=122, alternative_cp=100)
        self.assertFalse(discard)
        self.assertTrue(result["qualified"])
        self.assertEqual(result["loss_cp"], 0)
        self.assertEqual(result["evidence"]["deeper_delta_from_common"], -40)
        self.assertFalse(result["evidence"]["deeper_nonlosing_confirmation"]["near_best_at_this_depth"])

    def test_loss_and_compared_receipt_use_only_worst_confirmed_common_depth(self):
        baseline = packet(self.board, self.base.uci(), cp=50)
        result, _ = self.run_choice(baseline_info=baseline, candidate_cp=40, confirmation_cp=30, final_cp=0)
        self.assertTrue(result["qualified"])
        self.assertEqual(result["loss_cp"], 20)
        self.assertEqual(result["evidence"]["candidate_compared"]["cp"], 30)
        self.assertEqual(result["evidence"]["candidate_compared"]["depth"], 4)
        self.assertEqual(result["evidence"]["deeper_delta_from_common"], -30)

    def test_better_discovered_cp_is_allowed_after_deeper_confirmation(self):
        result, _ = self.run_choice(candidate_cp=400, final_cp=300)
        self.assertTrue(result["qualified"])
        self.assertEqual(result["loss_cp"], 0)

    def test_wrong_pov_bounds_missing_pv_depth_or_reply_root_fail_closed(self):
        changes = [lambda line: {**line, "lowerbound": True},
                   lambda line: {**line, "multipv": 2},
                   lambda line: {**line, "score": chess.engine.PovScore(chess.engine.Cp(20), chess.BLACK)},
                   lambda line: {**line, "pv": line["pv"][:1]},
                   lambda line: {**line, "depth": 3}]
        for change in changes:
            self.calls = []
            def mutate(index, response):
                if index != 1:
                    return response
                reply, line, packets = response
                changed = change(line)
                return reply, changed, {4: changed}
            result, _ = self.run_choice(mutate=mutate)
            self.assertFalse(result["qualified"] or result["applied"])

    def test_incomplete_deeper_or_alternative_query_retains_baseline(self):
        for stage in (2, 3):
            self.calls = []
            def mutate(index, response):
                reply, line, packets = response
                return (reply, line, {}) if index == stage else response
            result, _ = self.run_choice(mutate=mutate)
            self.assertFalse(result["qualified"] or result["applied"])
            self.assertEqual(result["selected_uci"], self.base.uci())

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
        def fail(index, response):
            raise RuntimeError("search failed")
        result, discard = self.run_choice(mutate=fail)
        self.assertTrue(discard)
        self.assertFalse(result["qualified"] or result["applied"])
        self.assertEqual(result["selected_uci"], self.base.uci())

    def test_deadline_callback_cannot_leave_a_completed_candidate_applied(self):
        class RunningTimer:
            def __init__(self, seconds, callback):
                self.callback = callback
            def start(self):
                pass
            def cancel(self):
                pass
            def join(self):
                self.callback()
        with patch.object(tactics.threading, "Timer", RunningTimer):
            result, discard = self.run_choice()
        self.assertTrue(discard)
        self.assertEqual(result["reason"], "tactical_probe_timeout")
        self.assertFalse(result["qualified"] or result["applied"])

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
        self.assertTrue(all(amount <= 1000 for amount, _ in self.calls))
        self.assertTrue(all(row["requested_ms"] <= row["deadline_remaining_before_ms"]
                            for row in result["attempts"]))
        self.assertLessEqual(len(self.calls[0][1]["root_moves"]), 8)

    def test_early_completed_depth_returns_time_to_the_final_alternative_probe(self):
        class Clock:
            now = 100.0
            def read(self):
                return self.now
            def advance(self, milliseconds):
                self.now += milliseconds / 1000
        class PassiveTimer:
            def __init__(self, *_):
                pass
            def start(self):
                pass
            def cancel(self):
                pass
            def join(self):
                pass
        clock = Clock()
        def early(index, response):
            clock.advance({1: 50, 2: 60, 3: 100}[index])
            return response
        with patch.object(tactics.time, "monotonic", side_effect=clock.read), patch.object(tactics.threading, "Timer", PassiveTimer):
            result, discard = self.run_choice(mutate=early)
        self.assertFalse(discard)
        self.assertTrue(result["qualified"])
        self.assertEqual(len(self.calls), 3)
        self.assertAlmostEqual(self.calls[2][0], 775 - 50 - 60 - 2, places=5)
        self.assertGreater(result["search_requested_ms"], result["budget_ms"])
        self.assertEqual(result["search_elapsed_ms"], 210)
        self.assertEqual(result["extra_elapsed_ms"], 210)
        self.assertTrue(all(row["requested_ms"] <= row["deadline_remaining_before_ms"]
                            for row in result["attempts"]))

    def test_a_complete_but_late_probe_cannot_escape_the_whole_turn_deadline(self):
        class Clock:
            now = 100.0
            def read(self):
                return self.now
        class PassiveTimer:
            def __init__(self, *_):
                pass
            def start(self):
                pass
            def cancel(self):
                pass
            def join(self):
                pass
        clock = Clock()
        def late(index, response):
            clock.now += .1 if index == 1 else .8
            return response
        with patch.object(tactics.time, "monotonic", side_effect=clock.read), patch.object(tactics.threading, "Timer", PassiveTimer):
            result, discard = self.run_choice(mutate=late)
        self.assertTrue(discard)
        self.assertFalse(result["qualified"] or result["applied"])
        self.assertEqual(result["selected_uci"], self.base.uci())
        self.assertEqual(result["reason"], "tactical_probe_timeout")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(result["search_elapsed_ms"], 900)


if __name__ == "__main__":
    unittest.main()
