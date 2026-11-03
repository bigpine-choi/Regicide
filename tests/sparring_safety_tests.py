"""Deterministic exposed-piece safety checks; no strength or Elo claim."""

from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

import chess
import chess.engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import sparring_safety as safety


HISTORY = "b1c3 e7e5 g1f3 b8c6 d2d4 e5d4 f3d4 c6d4 d1d4 g8f6 c3b5 f8e7 c1f4 d7d6 e1c1 e8g8"


def actual_position():
    board = chess.Board()
    for move in HISTORY.split():
        board.push_uci(move)
    return board


class ScriptedSearch:
    def __init__(self, board, scores, alternative="d4c4", modifier=None):
        self.board, self.scores, self.alternative = board, list(scores), alternative
        self.modifier, self.calls = modifier, []

    def __call__(self, budget, **options):
        position = options.get("position", self.board)
        allowed = options.get("root_moves")
        move = chess.Move.from_uci(self.alternative) if allowed is not None else next(iter(position.legal_moves))
        cp_own = self.scores.pop(0)
        cp_relative = cp_own if position.turn == self.board.turn else -cp_own
        info = {"depth": 6, "score": chess.engine.PovScore(chess.engine.Cp(cp_relative), position.turn), "pv": [move]}
        packets = {6: dict(info)}
        index = len(self.calls)
        self.calls.append((budget, options, position.fen()))
        if self.modifier:
            self.modifier(index, info, packets, position)
        return chess.engine.BestMove(move, None), info, packets


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.board, self.move = actual_position(), chess.Move.from_uci("b5a7")
        self.closed = []

    def run_gate(self, search, **options):
        return safety.verify(self.board, self.move, search, lambda: self.closed.append(True),
                             budget_ms=options.pop("budget_ms", 3000), **options)

    def test_actual_poisoned_pawn_triggers_even_with_queen_recapture(self):
        found = safety.inspect(self.board, self.move)
        self.assertTrue(found["risky"])
        self.assertEqual(found["reason"], "exposed_higher_value_capture")
        self.assertTrue(found["naturalness"]["admitted"])
        self.assertIn("a8a7", found["capture_replies"])
        branch = self.board.copy()
        branch.push(self.move)
        branch.push_uci("a8a7")
        self.assertIn(chess.Move.from_uci("d4a7"), branch.legal_moves)

    def test_quiet_move_and_equal_exchange_do_not_trigger(self):
        self.assertFalse(safety.inspect(chess.Board(), chess.Move.from_uci("e2e4"))["risky"])
        exchange = chess.Board()
        for move in HISTORY.split()[:7]:
            exchange.push_uci(move)
        self.assertFalse(safety.inspect(exchange, chess.Move.from_uci("c6d4"))["risky"])

    def test_obvious_knight_donation_triggers(self):
        board = chess.Board("r5k1/pp6/8/1N6/8/8/8/6K1 w - - 0 1")
        found = safety.inspect(board, chess.Move.from_uci("b5a7"))
        self.assertEqual(found["reason"], "obvious_material_donation")
        self.assertTrue(found["risky"])

    def test_quiet_hanging_knight_also_triggers(self):
        board = chess.Board("r5k1/8/8/1N6/8/8/8/6K1 w - - 0 1")
        move = chess.Move.from_uci("b5a3")
        self.assertFalse(board.is_capture(move))
        found = safety.inspect(board, move)
        self.assertEqual(found["reason"], "obvious_material_donation")
        self.assertTrue(found["risky"])

    def test_positive_donation_still_compares_better_native_alternative(self):
        self.board = chess.Board("r5k1/8/8/1N6/8/8/8/6K1 w - - 0 1")
        self.move = chess.Move.from_uci("b5a3")
        replacement, receipt, _ = self.run_gate(ScriptedSearch(self.board, [100, 300, 300], alternative="b5c3"))
        self.assertIsNotNone(replacement)
        self.assertEqual(receipt["risk"]["reason"], "obvious_material_donation")
        self.assertEqual(receipt["searches"], 3)
        self.assertEqual(receipt["improvement_cp"], 200)

    def test_positive_donation_not_replaced_without_verified_better_move(self):
        self.board = chess.Board("r5k1/8/8/1N6/8/8/8/6K1 w - - 0 1")
        self.move = chess.Move.from_uci("b5a3")
        replacement, receipt, _ = self.run_gate(ScriptedSearch(self.board, [100, 120, 115], alternative="b5c3"))
        self.assertIsNone(replacement)
        self.assertEqual(receipt["reason"], "no_verified_improvement")
        self.assertEqual(receipt["selected_move"], "b5a3")

    def test_compensated_capture_preserved_by_native_child_evidence(self):
        search = ScriptedSearch(self.board, [25])
        replacement, receipt, discarded = self.run_gate(search)
        self.assertIsNone(replacement)
        self.assertEqual(receipt["status"], "preserved")
        self.assertEqual(receipt["original_child"]["cp_own"], 25)
        self.assertEqual(len(search.calls), 1)
        self.assertFalse(discarded)

    def test_replacement_requires_independent_root_and_child(self):
        search = ScriptedSearch(self.board, [-250, 10, 5])
        before = self.board.fen()
        replacement, receipt, discarded = self.run_gate(search)
        self.assertEqual(replacement[0].move.uci(), "d4c4")
        self.assertEqual(receipt["status"], "replaced")
        self.assertEqual(receipt["reason"], "verified_alternative_in_band")
        self.assertEqual(receipt["improvement_cp"], 255)
        self.assertEqual(self.board.fen(), before)
        self.assertFalse(discarded)
        self.assertNotIn(self.move, search.calls[1][1]["root_moves"])
        self.assertEqual(search.calls[2][1]["position"].peek().uci(), "d4c4")
        self.assertTrue(all(options["fresh"] and not options["publish"] for _, options, _ in search.calls))
        self.assertLessEqual(sum(amount for amount, _, _ in search.calls), 3000)

    def test_negative_one_boundary_receives_alternative_comparison(self):
        replacement, receipt, _ = self.run_gate(ScriptedSearch(self.board, [-100, 0, 0]))
        self.assertIsNotNone(replacement)
        self.assertEqual(receipt["searches"], 3)

    def test_root_depth_five_requires_depth_six_child_confirmation(self):
        def root_five(i, info, packets, board):
            if i == 1:
                info["depth"] = 5
                packets.clear()
                packets[5] = dict(info)
        replacement, receipt, _ = self.run_gate(ScriptedSearch(self.board, [-160, 2, 3], modifier=root_five))
        self.assertIsNotNone(replacement)
        self.assertEqual(receipt["alternative_root"]["depth"], 5)
        self.assertEqual(receipt["alternative_child"]["depth"], 6)

    def test_complete_winning_mate_child_preserves_candidate(self):
        board = chess.Board("7k/8/5K2/8/6Q1/8/8/8 w - - 0 1")
        move = chess.Move.from_uci("g4g5")
        def search(amount, **options):
            child = options["position"]
            pv = [chess.Move.from_uci(uci) for uci in ("h8h7", "g5g7")]
            info = {"depth": 6, "score": chess.engine.PovScore(chess.engine.Mate(-1), child.turn), "pv": pv}
            return chess.engine.BestMove(pv[0], None), info, {6: dict(info)}
        # Exercise mate validation independently of geometric risk detection.
        with patch.object(safety, "inspect", return_value={"risky": True, "reason": "controlled_risk"}):
            replacement, receipt, _ = safety.verify(board, move, search, lambda: None, budget_ms=3000)
        self.assertIsNone(replacement)
        self.assertEqual(receipt["status"], "preserved")
        self.assertEqual(receipt["original_child"]["winner"], "white")

    def test_failed_final_child_never_publishes_already_discovered_alternative(self):
        def incomplete_child(i, info, packets, board):
            if i == 2:
                packets.clear()
        replacement, receipt, _ = self.run_gate(ScriptedSearch(self.board, [-250, 10, 5], modifier=incomplete_child))
        self.assertIsNotNone(receipt["alternative_root"])
        self.assertIsNone(replacement)
        self.assertEqual(receipt["selected_move"], "b5a7")
        self.assertEqual(receipt["status"], "unverified")

    def test_genuine_outside_band_improvement_is_labelled(self):
        replacement, receipt, _ = self.run_gate(ScriptedSearch(self.board, [-450, -280, -300]))
        self.assertIsNotNone(replacement)
        self.assertEqual(receipt["reason"], "verified_alternative_improves_outside_band")

    def test_root_and_child_must_both_show_minimum_improvement(self):
        for scores in ([-250, -200, -170], [-250, -180, -100], [-250, 10, -280], [-250, 100, -100]):
            with self.subTest(scores=scores):
                replacement, receipt, _ = self.run_gate(ScriptedSearch(self.board, scores))
                self.assertIsNone(replacement)
                self.assertNotEqual(receipt["status"], "replaced")

    def test_partial_bounded_shallow_and_wrong_pov_evidence_never_replaces(self):
        def partial(i, info, packets, board):
            packets[6] = {"depth": 6}
        def bounded(i, info, packets, board):
            info["lowerbound"] = True
            packets[6] = dict(info)
        def shallow(i, info, packets, board):
            info["depth"] = 5
            packets.clear()
            packets[5] = dict(info)
        def wrong_pov(i, info, packets, board):
            info["score"] = chess.engine.PovScore(chess.engine.Cp(500), not board.turn)
            packets[6] = dict(info)
        def malformed_mate(i, info, packets, board):
            info["score"] = chess.engine.PovScore(chess.engine.Mate(3), board.turn)
            packets[6] = dict(info)
        for modifier in (partial, bounded, shallow, wrong_pov, malformed_mate):
            with self.subTest(modifier=modifier.__name__):
                replacement, receipt, _ = self.run_gate(ScriptedSearch(self.board, [-250], modifier=modifier))
                self.assertIsNone(replacement)
                self.assertEqual(receipt["status"], "unverified")
                self.assertEqual(receipt["selected_move"], "b5a7")

    def test_black_perspective_remains_original_mover(self):
        self.board = self.board.mirror()
        self.move = chess.Move.from_uci("b4a2")
        search = ScriptedSearch(self.board, [-250, 10, 5], alternative="d5c5")
        replacement, receipt, _ = self.run_gate(search)
        self.assertIsNotNone(replacement)
        self.assertEqual(receipt["original_child"]["cp"], 250)
        self.assertEqual(receipt["alternative_child"]["cp_own"], 5)

    def test_low_budget_and_pre_cancel_start_no_search(self):
        for options in ({"budget_ms": 100}, {"cancelled": lambda: True}):
            search = ScriptedSearch(self.board, [])
            replacement, receipt, discarded = self.run_gate(search, **options)
            self.assertIsNone(replacement)
            self.assertEqual(len(search.calls), 0)
            self.assertFalse(discarded)

    def test_cancellation_closes_stalled_search_and_rejects_late_packet(self):
        entered, release, cancel = threading.Event(), threading.Event(), threading.Event()
        native = ScriptedSearch(self.board, [50])
        def search(amount, **options):
            entered.set()
            release.wait(2)
            return native(amount, **options)
        def close():
            self.closed.append(True)
            release.set()
        timer = threading.Thread(target=lambda: (entered.wait(1), cancel.set()), daemon=True)
        timer.start()
        replacement, receipt, discarded = safety.verify(self.board, self.move, search, close,
                                                         budget_ms=3000, cancelled=cancel.is_set)
        timer.join(1)
        self.assertIsNone(replacement)
        self.assertEqual(receipt["reason"], "cancelled")
        self.assertTrue(discarded)
        self.assertEqual(len(self.closed), 1)

    def test_stage_deadline_closes_stalled_search(self):
        release = threading.Event()
        native = ScriptedSearch(self.board, [50])
        def search(amount, **options):
            release.wait(2)
            return native(amount, **options)
        def close():
            self.closed.append(True)
            release.set()
        replacement, receipt, discarded = safety.verify(self.board, self.move, search, close, budget_ms=600)
        self.assertIsNone(replacement)
        self.assertEqual(receipt["reason"], "stage_budget_exceeded")
        self.assertTrue(discarded)
        self.assertLess(receipt["elapsed_ms"], 1000)

    def test_native_failure_discards_engine_but_preserves_move(self):
        def fail(*args, **options):
            raise RuntimeError("controlled native failure")
        replacement, receipt, discarded = self.run_gate(fail)
        self.assertIsNone(replacement)
        self.assertEqual(receipt["selected_move"], "b5a7")
        self.assertEqual(receipt["reason"], "native_search_failed")
        self.assertTrue(discarded)
        self.assertEqual(len(self.closed), 1)


if __name__ == "__main__":
    unittest.main()
