"""Early balance is a two-sided objective, without elective piece donations."""
import copy
import time
import unittest
from unittest import mock

import chess
import chess.engine

from sparring_quality_policy_tests import assessment, native_reply, position
import sparring_balance as balance
import sparring_quality as quality


class EarlyBalanceTests(unittest.TestCase):
    def fixture(self, *, black=False, child=500, good=30, root_mate=None, child_mate=None,
                promoted_depth=5, root_after=None):
        board = chess.Board()
        if black:
            board.push_uci("e2e4")
        original = chess.Move.from_uci("e7e5" if black else "e2e4")
        target = chess.Move.from_uci("d7d6" if black else "d2d3")
        calls = []
        def search(amount, *, root_moves, **options):
            self.assertEqual(len(root_moves), 1)
            candidate = root_moves[0]
            calls.append((candidate, amount, options))
            depth = options.get("depth") or promoted_depth
            response = native_reply(board, candidate, depth=depth)
            response[1]["score"] = chess.engine.PovScore(chess.engine.Cp(30 if candidate == target else 500), board.turn)
            return response
        def assess(candidate, amount, cancelled):
            if candidate == original:
                return assessment(board, candidate, 500, child, root_mate=root_mate, child_mate=child_mate)
            return assessment(board, candidate, root_after if root_after is not None else 500,
                              good if candidate == target else 480, root_mate=root_mate)
        return board, original, target, calls, search, assess

    def test_both_colors_discover_native_quiet_in_band_choice_and_promote(self):
        for black in (False, True):
            board, original, target, calls, search, assess = self.fixture(black=black)
            replacement, receipt, discarded = quality.verify(board, original, search, lambda: None, assess)
            self.assertFalse(discarded)
            self.assertEqual(replacement[0].move, target)
            self.assertEqual(receipt["status"], "balanced")
            self.assertEqual(receipt["final_verdict"]["child_cp_own"], 30)
            self.assertTrue(any(options.get("depth") == 3 for _, _, options in calls))
            self.assertEqual(calls[-1][0], target)
            self.assertGreaterEqual(calls[-1][1], 1000)
            self.assertNotIn("pv_uci", receipt["balance"])

    def test_initial_high_score_is_safe_but_not_a_completed_balance_objective(self):
        board, original, _, _, search, assess = self.fixture(good=450)
        replacement, receipt, _ = quality.verify(board, original, search, lambda: None, assess)
        self.assertIsNotNone(replacement)
        self.assertEqual(receipt["status"], "balance_improved")
        self.assertFalse(receipt["final_verdict"]["inside_close_band"])
        self.assertEqual(receipt["balance"]["status"], "improved")

    def test_promising_shallow_discovery_is_not_applied_without_native_completion(self):
        board, original, _, _, search, assess = self.fixture(promoted_depth=3)
        replacement, receipt, _ = quality.verify(board, original, search, lambda: None, assess)
        self.assertIsNone(replacement)
        self.assertEqual(receipt["selected_uci"], original.uci())
        self.assertTrue(any(row.get("reason") == "incomplete_native_promotion" for row in receipt["balance"]["attempts"]))

    def test_reference_root_drift_rejects_locally_appealing_candidate(self):
        board, original, _, _, search, assess = self.fixture(root_after=700)
        replacement, receipt, _ = quality.verify(board, original, search, lambda: None, assess)
        self.assertIsNone(replacement)
        self.assertTrue(any(row.get("reason") == "reference_root_disagreement" for row in receipt["balance"]["attempts"]))

    def test_balance_cannot_cross_to_a_position_further_from_target(self):
        for target_cp in (-95, -70):
            with self.subTest(target_cp=target_cp):
                board, original, _, _, search, assess = self.fixture(child=90, good=target_cp)
                replacement, receipt, _ = quality.verify(board, original, search, lambda: None, assess)
                self.assertIsNone(replacement)
                self.assertEqual(receipt["selected_uci"], original.uci())
                self.assertTrue(any(row.get("reason") == "no_clear_balance_progress"
                                    for row in receipt["balance"]["attempts"]))

    def test_custom_root_mate_still_requires_preserving_the_forced_win(self):
        board = chess.Board("4k3/8/8/8/8/8/4P3/4K3 w - - 0 12")
        move = chess.Move.from_uci("e2e4")
        value = quality.verdict(board, move, assessment(board, move, child=0, root_mate=4))
        self.assertFalse(value["acceptable"])
        self.assertEqual(value["reason"], "forced_win_lost")

    def test_pre20_winning_mate_can_be_deferred_to_verified_finite_close_position(self):
        board, original, target, _, search, assess = self.fixture(root_mate=4, child_mate=3)
        replacement, receipt, _ = quality.verify(board, original, search, lambda: None, assess)
        self.assertEqual(replacement[0].move, target)
        self.assertEqual(receipt["final_verdict"]["reason"], "early_winning_mate_deferred")
        self.assertEqual(receipt["status"], "balanced")
        bad = quality.verdict(board, target, assessment(board, target, child=-101, root_mate=4))
        self.assertFalse(bad["acceptable"])

    def test_after20_keeps_forced_win_and_does_not_seek_early_balance(self):
        board = position(40)
        original = next(iter(board.legal_moves))
        self.assertFalse(quality.verdict(board, original, assessment(board, original, child=0, root_mate=4))["acceptable"])
        def search(*args, **kwargs):
            self.fail("Move 21 must not run early balance discovery")
        replacement, receipt, _ = quality.verify(board, original, search, lambda: None,
            lambda move, *_: assessment(board, move, 500, 500))
        self.assertIsNone(replacement)
        self.assertEqual(receipt["status"], "verified")

    def test_negative_buffer_requests_best_resistance_not_elective_concession(self):
        board, original, _, calls, _, _ = self.fixture(child=-140)
        def search(amount, *, root_moves, **options):
            calls.append((root_moves, amount, options))
            return native_reply(board, root_moves[0])
        def assess(candidate, *_):
            return assessment(board, candidate, -130, -140 if candidate == original else -60)
        replacement, receipt, _ = quality.verify(board, original, search, lambda: None, assess)
        self.assertIsNotNone(replacement)
        self.assertEqual(receipt["balance"]["direction"], "behind")
        self.assertEqual(receipt["final_verdict"]["child_cp_own"], -60)
        self.assertTrue(all("depth" not in options for _, _, options in calls))

    def test_no_better_resistance_keeps_legal_incumbent_and_marks_balance_unmet(self):
        board, original, _, _, _, _ = self.fixture(child=-140)
        def search(amount, *, root_moves, **options):
            return native_reply(board, root_moves[0])
        replacement, receipt, _ = quality.verify(board, original, search, lambda: None,
            lambda candidate, *_: assessment(board, candidate, -130, -140 if candidate == original else -150))
        self.assertIsNone(replacement)
        self.assertEqual(receipt["reason"], "verified_resistance_preserved")
        self.assertEqual(receipt["balance"]["status"], "unmet")

    def test_direct_piece_donation_is_never_even_proposed_for_balance(self):
        board = chess.Board("3r3k/8/8/8/3p4/8/P1N5/4K3 w - - 0 1")
        original, donation = map(chess.Move.from_uci, ("a2a3", "c2d4"))
        self.assertIn(donation, board.legal_moves)
        self.assertFalse(balance.sparring_play.naturalness(board, donation)["admitted"])
        called = []
        def native(amount, allowed, **options):
            called.extend(allowed)
            return native_reply(board, allowed[0], depth=3)
        def evidence(response, allowed):
            return {"cp": 500, "depth": 3, "root": allowed[0].uci()}
        result, record = balance.recover(board, original, assessment(board, original, 500, 500),
            quality.verdict(board, original, assessment(board, original, 500, 500)), native_search=native,
            native_evidence=evidence, audit=lambda candidate: (assessment(board, candidate, 500, 500),
                quality.verdict(board, candidate, assessment(board, candidate, 500, 500))), check=lambda: None,
            deadline=time.monotonic()+7, minimum_native_depth=4)
        self.assertNotIn(donation, called)
        self.assertIsNone(result)

    def test_completed_progress_survives_only_optional_budget_expiry(self):
        board, original, target, _, _, assess = self.fixture(good=300)
        candidate = native_reply(board, target)
        verdict = quality.verdict(board, target, assessment(board, target, 500, 300))
        record = {"active": True, "status": "improved", "reason": "verified_natural_balance_progress"}
        clock = [100.0]
        def recover(*args, **kwargs):
            kwargs["completed"]((candidate, verdict, assessment(board,target,500,300)), record)
            clock[0] = 109
            kwargs["check"]()
        with mock.patch.object(quality.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(balance, "recover", side_effect=recover):
            replacement, receipt, _ = quality.verify(board, original, lambda *_: None, lambda: None, assess)
        self.assertIsNotNone(replacement)
        self.assertEqual(receipt["status"], "balance_improved")
        self.assertEqual(receipt["balance"]["stopped_reason"], "budget_exhausted")
        def cancelled_recover(*args, **kwargs):
            kwargs["completed"]((candidate, verdict, assessment(board,target,500,300)), record)
            raise InterruptedError("cancelled")
        with mock.patch.object(balance, "recover", side_effect=cancelled_recover):
            replacement, receipt, _ = quality.verify(board, original, lambda *_: None, lambda: None, assess,
                cancelled=lambda: False)
        self.assertIsNone(replacement)


class PreventiveBalanceTests(unittest.TestCase):
    def board(self, *, black=False):
        board = chess.Board()
        for move in "g1f3 g8f6 b1c3 b8c6 e2e3 e7e6 f1e2 f8e7".split():
            board.push_uci(move)
        if black:
            board.push_uci("e1g1")
        return board

    def scenario(self, *, black=False, original_cp=500, first_cp=75, center_cp=30,
                 stop=None):
        board = self.board(black=black)
        original, first, center = map(chess.Move.from_uci,
            ("e8g8", "a7a6", "d7d6") if black else ("e1g1", "a2a3", "d2d3"))
        calls, assessed = [], []
        clock, cancelled = [100.0], [False]

        def search(amount, *, root_moves, **options):
            candidate = root_moves[0]
            calls.append((candidate, amount, options))
            value = native_reply(board, candidate, depth=options.get("depth") or 5)
            value[1]["score"] = chess.engine.PovScore(chess.engine.Cp(
                30 if candidate == first else 40 if candidate == center else 500), board.turn)
            return value

        def assess(candidate, *_):
            assessed.append(candidate)
            if candidate == center:
                if stop == "budget":
                    clock[0] = 109.0
                elif stop == "cancel":
                    cancelled[0] = True
            cp = original_cp if candidate == original else first_cp if candidate == first else center_cp if candidate == center else 480
            return assessment(board, candidate, max(original_cp, 100), cp)

        with mock.patch.object(quality.time, "monotonic", side_effect=lambda: clock[0]):
            result = quality.verify(board, original, search, lambda: None, assess,
                                    cancelled=lambda: cancelled[0])
        return result, calls, assessed, original, first, center

    def test_boundary_candidate_keeps_searching_for_center_for_both_colors(self):
        for black in (False, True):
            with self.subTest(black=black):
                (replacement, receipt, discarded), _, assessed, _, first, center = self.scenario(black=black)
                self.assertFalse(discarded)
                self.assertEqual(replacement[0].move, center)
                self.assertLess(assessed.index(first), assessed.index(center))
                self.assertEqual(receipt["status"], "balanced")
                self.assertTrue(receipt["balance"]["center_reached"])
                self.assertEqual(receipt["balance"]["policy"], balance.POLICY)
                self.assertEqual(receipt["balance"]["trigger_cp"], 60)
                self.assertEqual(receipt["balance"]["target_tolerance_cp"], 20)
                self.assertEqual(len(receipt["balance"]["attempts"]), 2)

    def test_first_central_candidate_stops_optional_audits(self):
        (replacement, receipt, _), _, assessed, _, first, center = self.scenario(first_cp=35)
        self.assertEqual(replacement[0].move, first)
        self.assertNotIn(center, assessed)
        self.assertEqual(len(receipt["balance"]["attempts"]), 1)
        self.assertTrue(receipt["balance"]["center_reached"])

    def test_completed_boundary_candidate_survives_budget_but_not_cancellation(self):
        for stop in ("budget", "cancel"):
            with self.subTest(stop=stop):
                (replacement, receipt, _), _, _, original, first, _ = self.scenario(stop=stop)
                if stop == "budget":
                    self.assertEqual(replacement[0].move, first)
                    self.assertEqual(receipt["status"], "balanced")
                    self.assertFalse(receipt["balance"]["center_reached"])
                    self.assertEqual(receipt["balance"]["stopped_reason"], "budget_exhausted")
                else:
                    self.assertIsNone(replacement)
                    self.assertFalse(receipt["applied"])
                    self.assertEqual(receipt["selected_uci"], original.uci())
                    self.assertEqual(receipt["reason"], "cancelled")

    def test_preventive_replacement_requires_twenty_cp_of_progress(self):
        for black in (False, True):
            for candidate_cp, accepted in ((51, False), (50, True)):
                with self.subTest(black=black, candidate_cp=candidate_cp):
                    (replacement, receipt, _), _, _, _, first, _ = self.scenario(
                        black=black, original_cp=70, first_cp=candidate_cp, center_cp=480)
                    self.assertEqual(replacement is not None, accepted)
                    self.assertTrue(receipt["balance"]["preventive"])
                    self.assertEqual(receipt["balance"]["minimum_progress_cp"], 20)
                    if accepted:
                        self.assertEqual(replacement[0].move, first)
                    else:
                        attempt = next(row for row in receipt["balance"]["attempts"] if row["uci"] == first.uci())
                        self.assertEqual(attempt["progress_cp"], 19)
                        self.assertEqual(attempt["reason"], "no_clear_balance_progress")

    def test_soft_trigger_is_strict_and_starts_after_four_moves_each(self):
        for black in (False, True):
            board = self.board(black=black)
            move = next(iter(board.legal_moves))
            for cp, expected in ((60, False), (61, True), (80, True)):
                data = assessment(board, move, 100, cp)
                value = quality.verdict(board, move, data)
                self.assertEqual(balance.required(board, value, data), expected)
        board = self.board()
        board.pop()  # Black's fourth move has not yet been played.
        move = next(iter(board.legal_moves))
        for cp, expected in ((80, False), (81, True)):
            data = assessment(board, move, 100, cp)
            self.assertEqual(balance.required(board, quality.verdict(board, move, data), data), expected)

    def test_soft_trigger_never_extends_past_twentieth_move_or_custom_roots(self):
        for ply in (38, 39, 40, 41):
            board = position(ply)
            move = next(iter(board.legal_moves))
            data = assessment(board, move, 100, 75)
            self.assertEqual(balance.required(board, quality.verdict(board, move, data), data), ply < 40)
        board = chess.Board("4k3/8/8/8/8/8/4P3/4K3 w - - 0 12")
        move = chess.Move.from_uci("e2e4")
        data = assessment(board, move, 500, 500)
        self.assertFalse(balance.required(board, quality.verdict(board, move, data), data))

    def test_preventive_guard_preserves_resource_and_recovery_holds_for_both_colors(self):
        for black in (False, True):
            for protection in ("resources", "recovery"):
                with self.subTest(black=black, protection=protection), \
                        mock.patch.object(balance.sparring_play.sparring_pacing, "resource_guard",
                            return_value={"active": protection == "resources"}) as resources, \
                        mock.patch.object(balance.sparring_play.sparring_pacing, "derive",
                            return_value={"active": protection == "recovery"}) as recovery:
                    (replacement, receipt, _), calls, _, _, _, _ = self.scenario(
                        black=black, original_cp=70, first_cp=30)
                    self.assertIsNone(replacement)
                    self.assertFalse(receipt["applied"])
                    self.assertEqual(receipt["status"], "verified")
                    self.assertNotIn("balance", receipt)
                    self.assertEqual(calls, [])
                    resources.assert_called_once()
                    self.assertEqual(recovery.call_count, int(protection == "recovery"))

    def test_existing_outside_buffer_and_mate_recovery_do_not_use_preventive_guards(self):
        for black in (False, True):
            board = self.board(black=black)
            move = next(iter(board.legal_moves))
            for options in ({"child": 81}, {"child_mate": 3}):
                with self.subTest(black=black, options=options), \
                        mock.patch.object(balance.sparring_play.sparring_pacing, "resource_guard",
                            side_effect=AssertionError("Legacy recovery must keep its existing policy")), \
                        mock.patch.object(balance.sparring_play.sparring_pacing, "derive",
                            side_effect=AssertionError("Legacy recovery must keep its existing policy")):
                    data = assessment(board, move, 100, **options)
                    self.assertTrue(balance.required(board, quality.verdict(board, move, data), data))


class ConfirmedNativeRepairTests(unittest.TestCase):
    def scenario(self, *, ply=0, original_cp=-135, first_cp=-56, second_cp=-50,
                 second_root=14, second_status="ready", first_bound=False,
                 native_depth=2, native_delta=0, cancel_confirmation=False,
                 custom_board=None, custom_original=None, custom_target=None):
        board = custom_board if custom_board is not None else position(ply)
        original, target = list(board.legal_moves)[:2]
        original = custom_original or original
        target = custom_target or target
        calls, assessments = [], []
        cancelled = [False]
        def search(amount, *, root_moves, **options):
            calls.append(list(root_moves))
            reply = native_reply(board, target, depth=native_depth)
            if len(root_moves) == 1:
                reply[1]["score"] = chess.engine.PovScore(chess.engine.Cp(30 + native_delta),board.turn)
            return reply
        def assess(candidate, *_):
            assessments.append(candidate)
            if candidate == original:
                return assessment(board, candidate, 14, original_cp)
            repeat = assessments.count(target) > 1
            value = assessment(board, candidate, second_root if repeat else 14,
                               second_cp if repeat else first_cp)
            if repeat:
                value["status"] = second_status
                if cancel_confirmation:
                    cancelled[0] = True
            elif first_bound:
                value["child"]["bound"] = "lower"
            return value
        # The regular global pool is essential: the root move is selected by
        # Regicide among alternatives before the singleton stability check.
        with mock.patch.object(quality,"NATIVE_BUDGETS_MS",(400,)), mock.patch.object(quality,"MAX_MINOR_PROBES",0):
            result = quality.verify(board,original,search,lambda:None,assess,cancelled=lambda:cancelled[0])
        return result,calls,assessments,target

    def test_stable_native_depth_two_repairs_only_after_two_external_confirmations(self):
        (replacement,receipt,discarded), calls, assessed,target = self.scenario()
        self.assertFalse(discarded)
        self.assertEqual(replacement[0].move,target)
        self.assertEqual(replacement[1]["depth"],2)
        self.assertEqual(receipt["status"],"replaced")
        attempt=receipt["attempts"][0]
        self.assertEqual(attempt["evidence_kind"],"reference_confirmed_native_candidate")
        self.assertEqual(attempt["native_evidence"]["depth"],2)
        self.assertEqual(attempt["reference_confirmation"]["native_completed_depths"],[2,2])
        self.assertEqual(len(assessed),3)
        self.assertGreater(len(calls[0]),1)
        self.assertEqual(calls[1],[target])
        self.assertEqual(attempt["native_promotion"]["status"],"reference_confirmed")

    def test_incomplete_or_bounded_reference_cannot_confirm(self):
        for options in ({"second_status":"loading"},{"first_bound":True}):
            with self.subTest(options=options):
                (replacement,receipt,_),_,_,_=self.scenario(**options)
                self.assertIsNone(replacement)
                self.assertFalse(receipt["applied"])

    def test_two_reference_children_must_both_be_central_and_stable(self):
        for first,second in [(-81,-50),(-56,-81),(-56,40)]:
            with self.subTest(first=first,second=second):
                (replacement,receipt,_),_,_,_=self.scenario(first_cp=first,second_cp=second)
                self.assertIsNone(replacement)
                self.assertFalse(receipt["applied"])

    def test_root_disagreement_and_native_score_instability_reject(self):
        for options in ({"second_root":100},{"native_delta":41},{"native_depth":1}):
            with self.subTest(options=options):
                (replacement,receipt,_),_,_,_=self.scenario(**options)
                self.assertIsNone(replacement)
                self.assertFalse(receipt["applied"])

    def test_after20_and_already_safe_incumbent_keep_depth_four_requirement(self):
        for options in ({"ply":40},{"original_cp":-90}):
            with self.subTest(options=options):
                (replacement,receipt,_),_,_,_=self.scenario(**options)
                self.assertIsNone(replacement)
                self.assertFalse(receipt["applied"])

    def test_cancel_after_second_reference_discards_completed_result(self):
        (replacement,receipt,_),_,_,_=self.scenario(cancel_confirmation=True)
        self.assertIsNone(replacement)
        self.assertFalse(receipt["applied"])
        self.assertEqual(receipt["reason"],"cancelled")

    def test_shallow_repair_cannot_use_a_piece_donation_even_with_good_external_scores(self):
        board=chess.Board()
        for uci in "g1f3 a7a6 f3e5 a6a5 e5c4 b7b6".split():
            board.push_uci(uci)
        original,target=map(chess.Move.from_uci,("e2e4","c4a5"))
        self.assertIn(target,board.legal_moves)
        self.assertFalse(balance.sparring_play.naturalness(board,target)["admitted"])
        (replacement,receipt,_),_,_,_=self.scenario(custom_board=board,custom_original=original,custom_target=target)
        self.assertIsNone(replacement)
        self.assertEqual(receipt["attempts"][0]["reason"],"avoidable_material_donation")
        self.assertNotIn("reference_confirmation",receipt["attempts"][0])

    def test_elective_balance_cannot_use_shallow_reference_confirmation(self):
        fixture=EarlyBalanceTests().fixture(promoted_depth=2)
        board,original,_,_,search,assess=fixture
        replacement,receipt,_=quality.verify(board,original,search,lambda:None,assess)
        self.assertIsNone(replacement)
        self.assertFalse(receipt["applied"])
        self.assertTrue(all(attempt.get("native_evidence") is None for attempt in receipt["balance"]["attempts"]))


class RookOriginTests(unittest.TestCase):
    def test_central_rook_is_compared_before_another_global_pawn_search_for_both_colors(self):
        sequence=("b1c3 g7g6 g1f3 d7d5 e2e3 f8g7 f1b5 b8c6 b5c6 b7c6 e1g1 c8g4 "
                  "d2d4 g8f6 h2h3 g4f5 a2a4 f6e4 c3e4 d5e4 f3d2 c6c5 d4c5 d8d5 "
                  "d1e2 e8g8 e2c4 d5c6 f1d1 f8d8")
        white=chess.Board()
        for uci in sequence.split():white.push_uci(uci)
        for black in (False,True):
            board=white
            original,pawn,good=map(chess.Move.from_uci,("c4b5","g2g4","d1e1"))
            if black:
                from sparring_quality_discovery_tests import mirrored_history,mirrored_move
                board=mirrored_history(white)
                original,pawn,good=map(mirrored_move,(original,pawn,good))
            calls=[]
            def search(amount,*,root_moves,**options):
                calls.append(list(root_moves))
                chosen=pawn if len(calls)==1 else good
                self.assertIn(chosen,root_moves)
                return native_reply(board,chosen,depth=5)
            def assess(move,*_):
                return assessment(board,move,-72,-200 if move==original else (-140 if black else -95) if move==pawn else -40)
            replacement,receipt,_=quality.verify(board,original,search,lambda:None,assess)
            self.assertEqual(replacement[0].move,good)
            self.assertEqual([row["kind"] for row in receipt["attempts"]],["global","rook_origin"])
            self.assertEqual(receipt["attempts"][1]["origin"],"d8" if black else "d1")
            self.assertTrue(all(move.from_square==good.from_square for move in calls[1]))
            self.assertEqual(len(calls[1]),2)
            self.assertEqual(receipt["attempts"][1]["budget_ms"],quality.ROOK_PROBE_MS)
            if not black:
                self.assertEqual(receipt["attempts"][0]["reason"],"verified_repair_outside_buffer")


class FinalDonationGateTests(unittest.TestCase):
    def hanging_bishop(self):
        board=chess.Board()
        sequence=("e2e3 g8f6 d2d4 b8c6 f1e2 e7e6 a2a3 d7d5 b2b4 f8d6 g1f3 e8g8 "
                  "c1d2 e6e5 d4e5 c6e5 f3e5 d6e5 d2c3 f8e8 e1g1 c8d7 a1a2 c7c6 c3d4 "
                  "e5d4 e2f3 d4e3 c2c4 d5c4 b1d2 e3d4 d2c4 d7f5 a2d2")
        for uci in sequence.split():board.push_uci(uci)
        return board

    def test_recorded_h5_re_capture_loses_queen_and_cannot_claim_compensation(self):
        board=self.hanging_bishop();move=chess.Move.from_uci("h7h5")
        natural=balance.sparring_play.naturalness(board,move)
        self.assertFalse(natural["admitted"])
        threat=next(row for row in natural["vulnerable_captures"] if row["reply_uci"]=="d2d4")
        self.assertEqual(threat["recapture_cp"],0)
        self.assertTrue(threat["recapture_exchange"]["complete"])
        value=quality.verdict(board,move,assessment(board,move,184,-9))
        self.assertTrue(value["verified"])
        self.assertFalse(value["acceptable"])
        self.assertEqual(value["reason"],"avoidable_material_donation")

    def test_same_final_gate_applies_to_repair_candidates_not_only_incumbent(self):
        board=self.hanging_bishop();move=chess.Move.from_uci("a7a5")
        self.assertIn(move,board.legal_moves)
        value=quality.verdict(board,move,assessment(board,move,184,0))
        self.assertFalse(value["acceptable"])
        self.assertEqual(value["reason"],"avoidable_material_donation")

    def test_forced_bad_position_and_verified_mating_compensation_remain_distinct(self):
        board=self.hanging_bishop();move=chess.Move.from_uci("h7h5")
        forced=quality.verdict(board,move,assessment(board,move,-200,-250))
        self.assertTrue(forced["acceptable"])
        mate=quality.verdict(board,move,assessment(board,move,184,child_mate=3))
        self.assertTrue(mate["acceptable"])
        self.assertEqual(mate["reason"],"winning_mate_verified")

    def test_pending_equal_exchange_passes_final_gate_with_its_own_receipt(self):
        from pending_exchange_tests import black_pending
        board=black_pending();move=chess.Move.from_uci("a7a6")
        value=quality.verdict(board,move,assessment(board,move,363,20))
        self.assertTrue(value["acceptable"])
        self.assertEqual(value["naturalness"]["pending_exchange"]["kind"],"pending_equal_exchange")

    def test_completed_boundary_repair_survives_only_optional_exhaustion(self):
        for mode in ("budget","cancel","error"):
            with self.subTest(mode=mode):
                board=chess.Board();original,good,other=map(chess.Move.from_uci,("e2e4","d2d3","c2c3"))
                clock=[100.0];cancelled=[False];calls=[]
                def search(amount,*,root_moves,**options):
                    calls.append(list(root_moves))
                    if len(calls)==2:
                        if mode=="budget":clock[0]=109
                        elif mode=="cancel":cancelled[0]=True
                        else:raise RuntimeError("search_failed")
                    return native_reply(board,good if len(calls)==1 else other)
                def assess(move,*_):return assessment(board,move,0,-300 if move==original else -90)
                with mock.patch.object(quality.time,"monotonic",side_effect=lambda:clock[0]), \
                     mock.patch.object(quality,"NATIVE_BUDGETS_MS",(400,400)), \
                     mock.patch.object(quality,"MAX_ROOK_PROBES",0):
                    replacement,receipt,_=quality.verify(board,original,search,lambda:None,assess,cancelled=lambda:cancelled[0])
                if mode=="budget":
                    self.assertEqual(replacement[0].move,good)
                    self.assertEqual(receipt["repair_margin"]["status"],"outside_buffer")
                    self.assertEqual(receipt["repair_margin"]["stopped_reason"],"budget_exhausted")
                else:
                    self.assertIsNone(replacement)
                    self.assertFalse(receipt["applied"])


class UnsafePositiveBalanceTests(unittest.TestCase):
    def test_recorded_capture_donation_with_positive_score_still_gets_quiet_balance_discovery(self):
        from pending_exchange_tests import changed_attacker_pending
        board=changed_attacker_pending();original,good=map(chess.Move.from_uci,("e2b5","g1f1"))
        self.assertIn(original,board.legal_moves)
        initial=quality.verdict(board,original,assessment(board,original,363,143))
        self.assertTrue(initial["verified"])
        self.assertFalse(initial["acceptable"])
        self.assertEqual(initial["reason"],"avoidable_material_donation")
        calls=[]
        def search(amount,*,root_moves,**options):
            chosen=root_moves[0];calls.append((chosen,options))
            result=native_reply(board,chosen,depth=options.get("depth") or 5)
            result[1]["score"]=chess.engine.PovScore(chess.engine.Cp(30 if chosen==good else 500),board.turn)
            return result
        def assess(move,*_):return assessment(board,move,363,143 if move==original else 0 if move==good else 450)
        replacement,receipt,_=quality.verify(board,original,search,lambda:None,assess)
        self.assertEqual(replacement[0].move,good)
        self.assertEqual(receipt["status"],"balanced")
        self.assertTrue(any(options.get("depth")==3 for _,options in calls))
        self.assertTrue(receipt["final_verdict"]["acceptable"])
        self.assertEqual(receipt["final_verdict"]["naturalness"]["pending_exchange"]["opponent_changed_recapture"],["c7a5"])

    def test_failed_balance_discovery_falls_through_to_safe_native_repair_if_time_remains(self):
        board=FinalDonationGateTests().hanging_bishop()
        original,good=map(chess.Move.from_uci,("h7h5","c6c5"))
        self.assertTrue(quality.pending_exchange.naturalness(board,good)["admitted"])
        called=[]
        def search(amount,*,root_moves,**options):
            called.append(root_moves);self.assertIn(good,root_moves);return native_reply(board,good)
        def assess(move,*_):return assessment(board,move,184,143 if move==original else 30)
        with mock.patch.object(balance,"recover",return_value=(None,{"active":True,"status":"unmet"})) as recovery:
            replacement,receipt,_=quality.verify(board,original,search,lambda:None,assess)
        recovery.assert_called_once()
        self.assertEqual(replacement[0].move,good)
        self.assertEqual(receipt["status"],"replaced")
        self.assertTrue(called)

    def test_failed_balance_at_exhausted_budget_never_labels_original_donation_verified(self):
        board=FinalDonationGateTests().hanging_bishop();original=chess.Move.from_uci("h7h5")
        clock=[100.0]
        def recover(*args,**kwargs):
            clock[0]=109
            return None,{"active":True,"status":"unmet"}
        with mock.patch.object(quality.time,"monotonic",side_effect=lambda:clock[0]), \
             mock.patch.object(balance,"recover",side_effect=recover):
            replacement,receipt,_=quality.verify(board,original,lambda *a,**k:None,lambda:None,
                lambda move,*_:assessment(board,move,184,143))
        self.assertIsNone(replacement)
        self.assertEqual(receipt["status"],"unverified")
        self.assertEqual(receipt["reason"],"budget_exhausted")
        self.assertFalse(receipt["final_verdict"]["acceptable"])


if __name__ == "__main__":
    unittest.main()

