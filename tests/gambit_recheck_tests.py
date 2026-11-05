"""Spend existing gambit thinking time on native verification, without an oracle.

The isolated Rc8 position reproduces the reported donation. No private game
history is included; controlled native replies exercise host routing only.
"""
from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import json
import threading
import unittest
from unittest.mock import patch

import chess

import recovery_host_tests as support
from opening_variety_tests import info

host = support.host
pacing = host.engine_pacing
FEN = "kb4rr/pp6/4p3/2R2p2/1P1Pq3/P3BR1P/8/5Q1K w - - 7 37"


class RecheckBudgetTests(unittest.TestCase):
    def setUp(self):
        self.plan = {"policy": pacing.POLICY, "enabled": True, "color": "white",
                     "target_total_ms": 9230, "post_search_reduction_ms": 1500,
                     "reserve_ms": 15562, "clarity_kind": "unclassified",
                     "refinement": {"kind": "long_deliberation",
                                    "search_evidence": {"unsettled": True}}}
        self.clock = {"enabled": True, "status": "running", "running_color": "white",
                      "white_ms": 192500, "turn_elapsed_ms": 1711}

    def test_only_existing_hold_after_reduction_funds_work(self):
        before = copy.deepcopy(self.plan)
        self.assertEqual(pacing.gambit_recheck_budget(self.plan, self.clock), 5869)
        self.assertEqual(self.plan, before)
        self.plan["target_total_ms"] = 50000
        self.assertEqual(pacing.gambit_recheck_budget(self.plan, self.clock), 6000)

    def test_clock_reserve_and_minimum_viable_work(self):
        self.clock["white_ms"] = 16500
        self.assertEqual(pacing.gambit_recheck_budget(self.plan, self.clock), 788)
        self.clock["white_ms"] = 16000
        self.assertEqual(pacing.gambit_recheck_budget(self.plan, self.clock), 0)
        self.plan["reserve_ms"] = 0
        self.clock["white_ms"] = 10000
        self.assertEqual(pacing.gambit_recheck_budget(self.plan, self.clock), 0)

    def test_ineligible_turns_never_receive_extra_time(self):
        changes = [{"enabled": False}, {"policy": "unknown"}, {"opening_budget": {"active": True}},
                   {"clarity_kind": "single_reply"}, {"target_total_ms": 1900},
                   {"refinement": {"kind": "ordinary_decision", "search_evidence": {"unsettled": False}}},
                   {"gambit_recheck": {"active": True}}]
        for change in changes:
            with self.subTest(change=change):
                self.assertEqual(pacing.gambit_recheck_budget({**self.plan, **change}, self.clock), 0)
        for change in ({"enabled": False}, {"status": "paused"}, {"running_color": "black"},
                       {"white_ms": float("inf")}, {"turn_elapsed_ms": -1}):
            with self.subTest(clock=change):
                self.assertEqual(pacing.gambit_recheck_budget(self.plan, {**self.clock, **change}), 0)

    def test_unsettled_ordinary_turn_can_reclaim_time_and_never_wait_again(self):
        self.plan["refinement"]["kind"] = "ordinary_decision"
        self.assertGreater(pacing.gambit_recheck_budget(self.plan, self.clock), 0)
        self.plan["gambit_recheck"] = {"active": True, "status": "unverified"}
        self.assertEqual(pacing.remaining_wait_ms(self.plan, self.clock), 0)


class GambitRecheckHostTests(unittest.TestCase):
    setUp = support.RecoveryHostTests.setUp
    wait = support.RecoveryHostTests.wait
    position = support.RecoveryHostTests.position
    queue = support.RecoveryHostTests.queue
    wait_for_second_search = support.RecoveryHostTests.wait_for_second_search

    def prepare(self, *, gambit=True, gate=None, depth=6, baseline="c5c8"):
        board = self.position(chess.Board(FEN), sparring=False)
        self.game.boss_mode = gambit
        self.game._clock._balances["white"] = 194211
        self.queue(board, baseline, -645,
                   packets=[info(board, "c5e5", -323, 4), info(board, baseline, -645, 5)], cost_ms=0)
        child = board.copy(stack=True)
        child.push_uci(baseline)
        self.queue(child, "g8c8", 874, packets=[info(child, "g8c8", 874, depth)], cost_ms=0, gate=gate)
        self.queue(board, "c5c3", -714, packets=[info(board, "c5c3", -714, 7)], cost_ms=0)
        child = board.copy(stack=True)
        child.push_uci("c5c3")
        self.queue(child, "g8g3", 714, packets=[info(child, "g8g3", 714, 6)], cost_ms=0)
        return board

    def saved_decision(self):
        return self.game._boss_decisions[len(self.game.board.move_stack)]

    def test_verified_native_alternative_replaces_rc8_and_saves_evidence(self):
        board = self.prepare()
        before = board.fen()
        with patch.object(self.game, "_apply_sparring_referee", side_effect=AssertionError("external selection")):
            state = self.game.engine_move(self.game.revision)
        self.assertIsNone(state["error"])
        self.assertEqual(state["last_move"], "c5c3")
        self.assertEqual(len(self.peer.searches), 4)
        self.assertEqual(self.waits, [], "Used thinking time cannot be followed by a cosmetic hold")
        self.assertEqual(self.peer.searches[1]["board"].move_stack[-1].uci(), "c5c8")
        self.assertEqual(self.peer.searches[2]["board"].fen(), before)
        self.assertNotIn(chess.Move.from_uci("c5c8"), self.peer.searches[2]["root_moves"])
        self.assertEqual(self.peer.searches[3]["board"].move_stack[-1].uci(), "c5c3")
        decision = self.saved_decision()
        self.assertEqual(decision["baseline_uci"], "c5c8")
        self.assertEqual(decision["selected_uci"], "c5c3")
        self.assertEqual(decision["pre_recheck_selected_uci"], "c5c8")
        self.assertEqual(decision["reason"], "gambit_tactical_recheck")
        check = decision["gambit_recheck"]
        self.assertEqual(check["original_child"]["cp_own"], -874)
        self.assertEqual(check["alternative_child"]["cp_own"], -714)
        self.assertEqual(check["funded_from"], "existing_refined_idle_time")
        self.assertNotIn("history_uci", check)
        self.assertEqual(check["history_sha256"], hashlib.sha256(b"").hexdigest())
        timing = self.game._engine_pacing_decisions[len(self.game.board.move_stack)]
        self.assertEqual(timing["selected_uci"], "c5c3")
        self.assertEqual(timing["forecast"]["pv_uci"][0], "c5c3")
        self.assertLessEqual(timing["gambit_recheck"]["budget_ms"], 6000)
        self.assertFalse(self.game._boss_memo, "Clock-specific verification must not enter the policy memo")
        saved = json.loads((self.game.save_dir / (self.game.game_id + ".json")).read_text(encoding="utf-8"))
        self.assertEqual(saved["boss_play"]["decisions"][-1]["gambit_recheck"], check)

    def test_normal_mode_preserves_existing_search_path(self):
        self.prepare(gambit=False)
        with patch.object(host.sparring_safety, "verify", side_effect=AssertionError("normal verification")):
            state = self.game.engine_move(self.game.revision)
        self.assertIsNone(state["error"])
        self.assertEqual(state["last_move"], "c5c8")
        self.assertEqual(len(self.peer.searches), 1)

    def test_low_clock_skips_optional_check(self):
        self.prepare()
        self.game._clock._balances["white"] = 9000
        state = self.game.engine_move(self.game.revision)
        self.assertIsNone(state["error"])
        self.assertEqual(state["last_move"], "c5c8")
        self.assertEqual(len(self.peer.searches), 1)

    def test_stable_or_unexposed_selection_does_not_request_verification(self):
        for stable in (True, False):
            with self.subTest(stable=stable):
                board = self.prepare(baseline="c5c8" if stable else "c5c3")
                if stable:
                    self.peer.scripts[0]["packets"] = [info(board, "c5c8", -645, d) for d in (4, 5)]
                with patch.object(host.sparring_safety, "verify", side_effect=AssertionError("untriggered verification")):
                    state = self.game.engine_move(self.game.revision)
                self.assertIsNone(state["error"])
                self.assertEqual(state["last_move"], "c5c8" if stable else "c5c3")
                self.assertEqual(len(self.peer.searches), 1)

    def test_clock_increase_does_not_extend_original_verification_deadline(self):
        gate = threading.Event()
        self.prepare(gate=gate)
        with ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(self.game.engine_move, self.game.revision)
            self.wait_for_second_search()
            with self.game._lock:
                original = copy.deepcopy(self.game._active_pacing)
                self.game._clock._balances["white"] += 60000
                self.game._refresh_active_pacing()
                updated = self.game._active_pacing
                self.assertEqual(updated["gambit_recheck"]["visible_deadline_ms"],
                                 original["gambit_recheck"]["visible_deadline_ms"])
                self.assertLessEqual(updated["target_total_ms"], original["target_total_ms"])
                self.now.advance(original["gambit_recheck"]["visible_deadline_ms"] / 1000)
            self.assertTrue(gate.wait(3), "Original visible deadline must cancel work despite more clock")
            state = running.result(5)
        self.assertIsNone(state["error"])
        self.assertEqual(state["last_move"], "c5c8")
        self.assertEqual(self.saved_decision()["gambit_recheck"]["reason"], "cancelled")
        self.assertEqual(self.waits, [])

    def test_unreliable_child_keeps_baseline_without_safety_claim(self):
        self.prepare(depth=5)
        state = self.game.engine_move(self.game.revision)
        self.assertIsNone(state["error"])
        self.assertEqual(state["last_move"], "c5c8")
        self.assertEqual(len(self.peer.searches), 2)
        self.assertEqual(self.saved_decision()["gambit_recheck"]["status"], "unverified")
        self.assertEqual(self.waits, [])

    def test_optional_verifier_failure_preserves_legal_baseline(self):
        self.prepare()
        with patch.object(host.sparring_safety, "verify", side_effect=RuntimeError("controlled failure")):
            state = self.game.engine_move(self.game.revision)
        self.assertIsNone(state["error"])
        self.assertEqual(state["last_move"], "c5c8")
        self.assertEqual(self.saved_decision()["gambit_recheck"]["reason"], "verification_failed")
        self.assertIsNone(self.game._engine)
        self.assertEqual(self.waits, [])

    def test_close_interrupts_verification_without_late_move(self):
        gate = threading.Event()
        board = self.prepare(gate=gate)
        before = board.fen()
        with ThreadPoolExecutor(max_workers=2) as pool:
            running = pool.submit(self.game.engine_move, self.game.revision)
            self.wait_for_second_search()
            closing = pool.submit(self.game.close)
            self.assertTrue(gate.wait(3))
            running.result(5)
            closing.result(5)
        self.assertEqual(self.game.board.fen(), before)
        self.assertIsNone(self.game.error)

    def test_clock_cut_interrupts_verification_and_keeps_legal_baseline(self):
        gate = threading.Event()
        self.prepare(gate=gate)
        with ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(self.game.engine_move, self.game.revision)
            self.wait_for_second_search()
            with self.game._lock:
                self.game._clock._balances["white"] = 9000
                self.game._refresh_active_pacing()
            self.assertTrue(gate.wait(3))
            state = running.result(5)
        self.assertIsNone(state["error"])
        self.assertEqual(state["last_move"], "c5c8")
        self.assertEqual(self.saved_decision()["gambit_recheck"]["status"], "unverified")
        self.assertEqual(self.waits, [])

    def test_clock_expiry_rejects_verification_result(self):
        gate = threading.Event()
        board = self.prepare(gate=gate)
        before = board.fen()
        with ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(self.game.engine_move, self.game.revision)
            self.wait_for_second_search()
            self.now.advance(200)
            self.assertTrue(gate.wait(3))
            state = running.result(5)
        self.assertEqual(state["termination"], "time_forfeit")
        self.assertEqual(self.game.board.fen(), before)
        self.assertIsNone(state["error"])

    def test_invalidated_game_rejects_late_verification(self):
        gate = threading.Event()
        board = self.prepare(gate=gate)
        before = board.fen()
        with ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(self.game.engine_move, self.game.revision)
            self.wait_for_second_search()
            with self.game._lock:
                self.game._game_token = object()
            self.assertTrue(gate.wait(3))
            state = running.result(5)
        self.assertEqual(self.game.board.fen(), before)
        self.assertIsNone(state["error"])


if __name__ == "__main__":
    unittest.main()
