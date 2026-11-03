"""Keep proven damage limitation and promote promising native discoveries."""
import threading
import unittest
from unittest import mock

import chess

from sparring_quality_policy_tests import assessment, native_reply, position
import sparring_quality as quality


def recorded_move21():
    # Actual 1700-white regression: 21.axb4 lost far more than 21.Nxf6+.
    board = chess.Board()
    history = ("b1c3 g7g6 g1f3 c7c5 d2d4 c5d4 d1d4 g8f6 c1e3 b8c6 "
               "d4a4 d7d6 h2h3 c8d7 e1c1 c6a5 a4d4 f8g7 c1b1 b7b5 "
               "d4b4 a8b8 g2g3 e8g8 f1g2 a5c6 b4h4 d8c7 g3g4 f8c8 "
               "f3g5 c6e5 f2f4 e5c4 e3d4 c4a3 b2a3 h7h6 g5e4 b5b4")
    for uci in history.split():
        board.push_uci(uci)
    return board


class QualityMitigationTests(unittest.TestCase):
    def run_scores(self, board, root, initial, children, *, repeated_root=None):
        moves = list(board.legal_moves)
        incumbent = moves[0]
        safe = [move for move in moves[1:] if quality.pending_exchange.naturalness(board, move)["admitted"]]
        values = dict(zip(safe, children))
        def search(amount, *, root_moves, **options):
            return native_reply(board, next(move for move in values if move in root_moves))
        def assess(move, amount, cancelled):
            return assessment(board, move, root if move == incumbent or repeated_root is None else repeated_root,
                              initial if move == incumbent else values[move])
        with mock.patch.object(quality, "NATIVE_BUDGETS_MS", (400,) * len(children)), \
                mock.patch.object(quality, "MAX_MINOR_PROBES", 0), \
                mock.patch.object(quality, "MAX_ROOK_PROBES", 0):
            return quality.verify(board, incumbent, search, lambda: None, assess)

    def test_recorded_two_cp_floor_miss_does_not_restore_five_pawn_loss(self):
        board = recorded_move21()
        original, better = chess.Move.from_uci("a3b4"), chess.Move.from_uci("e4f6")
        def search(amount, *, root_moves, **options):
            self.assertIn(better, root_moves)
            return native_reply(board, better)
        def assess(move, amount, cancelled):
            return assessment(board, move, -136, -646 if move == original else -158)
        with mock.patch.object(quality, "NATIVE_BUDGETS_MS", (400,)):
            replacement, receipt, discarded = quality.verify(board, original, search, lambda: None, assess)
        self.assertEqual(replacement[0].move, better)
        self.assertFalse(discarded)
        self.assertEqual((receipt["status"], receipt["reason"]),
                         ("mitigated", "verified_native_damage_limitation"))
        self.assertFalse(receipt["final_verdict"]["acceptable"])
        self.assertEqual(receipt["final_verdict"]["floor_cp"], -136)
        self.assertEqual(receipt["mitigation"]["improvement_cp"], 488)
        self.assertFalse(receipt["mitigation"]["floor_met"])
        self.assertFalse(receipt["final_verdict"]["inside_close_band"])

    def test_best_finite_improvement_is_retained_for_both_colors_and_all_phases(self):
        for ply in (38, 39, 40, 41, 98, 99):
            with self.subTest(ply=ply):
                replacement, receipt, _ = self.run_scores(position(ply), 0, -500, [-220, -150, -190])
                self.assertIsNotNone(replacement)
                self.assertEqual(receipt["status"], "mitigated")
                self.assertEqual(receipt["final_verdict"]["child_cp_own"], -150)
                self.assertFalse(receipt["final_verdict"]["acceptable"])
                self.assertEqual(receipt["mitigation"]["attempt_index"], 1)

    def test_fully_acceptable_repair_takes_precedence_over_cached_mitigation(self):
        replacement, receipt, _ = self.run_scores(position(40), 0, -500, [-150, 0])
        self.assertIsNotNone(replacement)
        self.assertEqual(receipt["status"], "replaced")
        self.assertTrue(receipt["final_verdict"]["acceptable"])
        self.assertIsNone(receipt["mitigation"])

    def test_mitigation_requires_large_original_loss_and_clear_improvement(self):
        for initial, child, expected in ((-249, -130, False), (-250, -151, False), (-250, -150, True)):
            with self.subTest(initial=initial, child=child):
                replacement, receipt, _ = self.run_scores(position(40), -100, initial, [child])
                self.assertEqual(replacement is not None, expected)
                self.assertEqual(receipt["status"], "mitigated" if expected else "unverified")

    def test_root_disagreement_cannot_be_bypassed_by_large_improvement(self):
        replacement, receipt, _ = self.run_scores(position(40), -136, -646, [-158], repeated_root=-35)
        self.assertIsNone(replacement)
        self.assertEqual(receipt["attempts"][0]["reason"], "reference_root_disagreement")

    def test_losing_mate_and_unverified_candidate_cannot_be_mitigations(self):
        for mode in ("mate", "unverified"):
            with self.subTest(mode=mode):
                board = position(40)
                original = next(iter(board.legal_moves))
                def search(amount, *, root_moves, **options):
                    return native_reply(board, root_moves[0])
                def assess(move, amount, cancelled):
                    if move == original:
                        return assessment(board, move, 0, -500)
                    return (assessment(board, move, 0, child_mate=-4) if mode == "mate"
                            else {"status": "unverified"})
                with mock.patch.object(quality, "NATIVE_BUDGETS_MS", (400,)):
                    replacement, receipt, _ = quality.verify(board, original, search, lambda: None, assess)
                self.assertIsNone(replacement)
                self.assertIsNone(receipt["mitigation"])

    def test_cancel_after_completed_mitigation_restores_original(self):
        self.assert_cached_then_stop("cancelled")

    def test_generic_search_failure_after_completed_mitigation_restores_original(self):
        self.assert_cached_then_stop("exception")

    def test_budget_exhaustion_can_keep_only_earlier_completed_mitigation(self):
        self.assert_cached_then_stop("budget_exhausted")

    def test_cancellation_wins_even_when_budget_also_expires(self):
        self.assert_cached_then_stop("cancelled_and_budget")

    def test_native_watchdog_can_keep_only_previously_completed_mitigation(self):
        board = position(40)
        original = next(iter(board.legal_moves))
        clock, calls = [100.0], []
        closed = threading.Event()
        def search(amount, *, root_moves, **options):
            calls.append(root_moves[0])
            if len(calls) == 2:
                clock[0] += amount / 1000 + .2
                self.assertTrue(closed.wait(.4), "watchdog must stop the unfinished native search")
            return native_reply(board, root_moves[0])
        def assess(move, amount, cancelled):
            return assessment(board, move, 0, -500 if move == original else -150)
        with mock.patch.object(quality.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(quality, "NATIVE_BUDGETS_MS", (400, 400)):
            replacement, receipt, discarded = quality.verify(board, original, search, closed.set, assess)
        self.assertTrue(discarded)
        self.assertEqual(replacement[0].move, calls[0])
        self.assertEqual(receipt["status"], "mitigated")
        self.assertEqual(receipt["mitigation"]["stopped_reason"], "native_timeout")

    def assert_cached_then_stop(self, mode):
        board = position(40)
        original = next(iter(board.legal_moves))
        clock, calls, assessed = [100.0], [], []
        cancelled = threading.Event()
        def search(amount, *, root_moves, **options):
            move = root_moves[0]
            calls.append(move)
            if len(calls) == 2:
                if mode == "exception":
                    raise RuntimeError("fixture failure")
                if "cancelled" in mode:
                    cancelled.set()
                if "budget" in mode:
                    clock[0] += 9
            return native_reply(board, move)
        def assess(move, amount, interrupted):
            assessed.append(move)
            return assessment(board, move, 0, -500 if move == original else -150)
        with mock.patch.object(quality.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(quality, "NATIVE_BUDGETS_MS", (400, 400)):
            replacement, receipt, _ = quality.verify(board, original, search, lambda: None, assess,
                                                       cancelled=cancelled.is_set)
        self.assertEqual(assessed, [original, calls[0]])
        if mode == "budget_exhausted":
            self.assertEqual(replacement[0].move, calls[0])
            self.assertEqual(receipt["status"], "mitigated")
            self.assertEqual(receipt["mitigation"]["stopped_reason"], "budget_exhausted")
        else:
            self.assertIsNone(replacement)
            self.assertEqual(receipt["selected_uci"], original.uci())
            self.assertEqual(receipt["final_verdict"], receipt["initial_verdict"])


class QualityPromotionTests(unittest.TestCase):
    def scenario(self, *, child=0, promotion_depth=4, promotion_bound=False,
                 discovery_bound=False, cancel_promotion=False, promotion_wrong_root=False, original=-500):
        board = position(40)
        original_move, chosen = list(board.legal_moves)[:2]
        calls, assessed = [], []
        cancelled = threading.Event()
        def search(amount, *, root_moves, **options):
            calls.append({"moves": list(root_moves), "budget_ms": amount, **options})
            promoting = len(root_moves) == 1
            if promoting and cancel_promotion:
                cancelled.set()
            move = original_move if promoting and promotion_wrong_root else chosen
            value = native_reply(board, move, depth=promotion_depth if promoting else 3)
            if promotion_bound and promoting or discovery_bound and not promoting:
                value[1]["lowerbound"] = True
            return value
        def assess(move, amount, interrupted):
            assessed.append(move)
            return assessment(board, move, 0, original if move == original_move else child)
        with mock.patch.object(quality, "NATIVE_BUDGETS_MS", (400,)):
            replacement, receipt, _ = quality.verify(board, original_move, search, lambda: None, assess,
                                                       cancelled=cancelled.is_set)
        return replacement, receipt, calls, assessed, chosen

    def test_shallow_promising_candidate_is_promoted_before_acceptance(self):
        replacement, receipt, calls, assessed, chosen = self.scenario()
        self.assertEqual(replacement[0].move, chosen)
        self.assertEqual(replacement[1]["depth"], 4)
        self.assertEqual(receipt["status"], "replaced")
        self.assertEqual(len(assessed), 2)
        self.assertEqual(calls[1]["moves"], [chosen])
        self.assertLessEqual(calls[1]["budget_ms"], quality.NATIVE_PROMOTION_MS)
        self.assertTrue(calls[1]["fresh"])
        self.assertFalse(calls[1]["publish"])
        attempt = receipt["attempts"][0]
        self.assertEqual(attempt["discovery_evidence"]["depth"], 3)
        self.assertEqual(attempt["native_evidence"]["depth"], 4)
        self.assertEqual(attempt["native_promotion"]["status"], "verified")

    def test_shallow_mitigation_also_requires_native_depth_four(self):
        replacement, receipt, _calls, _assessed, _chosen = self.scenario(child=-150)
        self.assertIsNotNone(replacement)
        self.assertEqual(receipt["status"], "mitigated")
        self.assertEqual(receipt["attempts"][0]["native_evidence"]["depth"], 4)
        self.assertFalse(receipt["final_verdict"]["acceptable"])

    def test_bad_shallow_candidate_spends_no_promotion_search(self):
        replacement, receipt, calls, assessed, _chosen = self.scenario(child=-550)
        self.assertIsNone(replacement)
        self.assertEqual((len(calls), len(assessed)), (1, 2))
        self.assertIsNone(receipt["attempts"][0]["native_promotion"])

    def test_bounded_discovery_is_rejected_before_external_audit(self):
        replacement, receipt, calls, assessed, _chosen = self.scenario(discovery_bound=True)
        self.assertIsNone(replacement)
        self.assertEqual((len(calls), len(assessed)), (1, 1))
        self.assertEqual(receipt["attempts"][0]["reason"], "incomplete_native_candidate")

    def test_shallow_or_bounded_promotion_cannot_be_applied(self):
        for options in ({"promotion_depth": 3}, {"promotion_bound": True}):
            with self.subTest(options=options):
                replacement, receipt, calls, _assessed, _chosen = self.scenario(**options)
                self.assertIsNone(replacement)
                self.assertEqual(len(calls), 2)
                self.assertEqual(receipt["attempts"][0]["reason"], "incomplete_native_promotion")
                self.assertIsNone(receipt["attempts"][0]["native_evidence"])

    def test_cancellation_after_promotion_cannot_apply_the_new_move(self):
        replacement, receipt, calls, _assessed, _chosen = self.scenario(cancel_promotion=True)
        self.assertIsNone(replacement)
        self.assertEqual(len(calls), 2)
        self.assertEqual(receipt["reason"], "cancelled")
        self.assertFalse(receipt["applied"])

    def test_promotion_cannot_substitute_an_unassessed_move(self):
        replacement, receipt, _calls, _assessed, _chosen = self.scenario(promotion_wrong_root=True)
        self.assertIsNone(replacement)
        self.assertEqual(receipt["reason"], "invalid_native_candidate")
        self.assertFalse(receipt["applied"])


if __name__ == "__main__":
    unittest.main()
