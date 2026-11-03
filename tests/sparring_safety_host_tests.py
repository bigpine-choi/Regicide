"""Final sparring safety gate through the real host with controlled UCI peers."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import unittest
from unittest.mock import patch

import chess

import recovery_host_tests as support
from opening_variety_tests import info

host = support.host
PREFIX = "b1c3 e7e5 g1f3 b8c6 d2d4 e5d4 f3d4 c6d4 d1d4 g8f6 c3b5 f8e7 c1f4 d7d6 e1c1 e8g8"


class SafetyHostTests(unittest.TestCase):
    # Reuse fixture utilities, not the recovery test methods themselves.
    setUp = support.RecoveryHostTests.setUp
    wait = support.RecoveryHostTests.wait
    position = support.RecoveryHostTests.position
    queue = support.RecoveryHostTests.queue
    saved_decision = support.RecoveryHostTests.saved_decision
    wait_for_second_search = support.RecoveryHostTests.wait_for_second_search

    def prepare(self, *, sparring=True, gate=None, original_cp=160):
        board = self.position(PREFIX, sparring=sparring)
        self.queue(board, "b5a7", 17, packets=[info(board, "b5a7", 17, 5)], cost_ms=0)
        child = board.copy(stack=True)
        child.push_uci("b5a7")
        self.queue(child, "c8d7", original_cp,
                   packets=[info(child, "c8d7", original_cp, 7)], cost_ms=0, gate=gate)
        self.queue(board, "d4c4", 4, packets=[info(board, "d4c4", 4, 6)], cost_ms=0)
        alternative = board.copy(stack=True)
        alternative.push_uci("d4c4")
        reply = next(iter(alternative.legal_moves)).uci()
        self.queue(alternative, reply, -4, packets=[info(alternative, reply, -4, 6)], cost_ms=0)
        return board

    def test_poisoned_pawn_baseline_replaced_before_application_and_saved(self):
        board = self.prepare()
        before = board.fen()
        state = self.game.engine_move(self.game.revision)
        self.assertIsNone(state["error"])
        self.assertEqual(state["last_move"], "d4c4")
        self.assertEqual(len(self.peer.searches), 4)
        self.assertEqual(self.waits, [], "Verification must not add a cosmetic wait")
        self.assertEqual(self.peer.searches[1]["board"].move_stack[-1].uci(), "b5a7")
        self.assertEqual(self.peer.searches[2]["board"].fen(), before)
        self.assertNotIn(chess.Move.from_uci("b5a7"), self.peer.searches[2]["root_moves"])
        self.assertEqual(self.peer.searches[3]["board"].move_stack[-1].uci(), "d4c4")
        receipt = self.saved_decision()
        self.assertEqual(receipt["reason"], "tactical_safety_replacement")
        self.assertEqual(receipt["pre_safety_selection"]["selected_uci"], "b5a7")
        self.assertEqual(receipt["tactical_safety"]["original_child"]["cp_own"], -160)
        self.assertEqual(receipt["tactical_safety"]["alternative_child"]["cp_own"], 4)
        self.assertEqual(receipt["band_control"]["strict_band_status"], "met")
        self.assertEqual(receipt["band_control"]["selected_postmove_cp"], 4)
        saved = json.loads((self.game.save_dir / (self.game.game_id + ".json")).read_text(encoding="utf-8"))
        self.assertEqual(saved["sparring_play"]["decisions"][-1]["tactical_safety"], receipt["tactical_safety"])

    def test_normal_mode_keeps_existing_search_behavior(self):
        self.prepare(sparring=False)
        with patch.object(host.sparring_safety, "verify", side_effect=AssertionError("normal mode safety call")):
            state = self.game.engine_move(self.game.revision)
        self.assertIsNone(state["error"])
        self.assertEqual(state["last_move"], "b5a7")
        self.assertEqual(len(self.peer.searches), 1)

    def test_compensated_risky_capture_is_preserved(self):
        self.prepare(original_cp=-30)
        state = self.game.engine_move(self.game.revision)
        self.assertIsNone(state["error"])
        self.assertEqual(state["last_move"], "b5a7")
        self.assertEqual(len(self.peer.searches), 2)
        self.assertEqual(self.saved_decision()["tactical_safety"]["reason"], "compensation_verified")

    def test_low_clock_skips_optional_verification_without_claiming_safety(self):
        self.prepare()
        self.game._clock._balances["white"] = 9000
        state = self.game.engine_move(self.game.revision)
        self.assertIsNone(state["error"])
        self.assertEqual(state["last_move"], "b5a7")
        self.assertEqual(len(self.peer.searches), 1)
        self.assertEqual(self.saved_decision()["tactical_safety"]["status"], "skipped")
        self.assertEqual(self.saved_decision()["tactical_safety"]["reason"], "insufficient_budget")

    def test_close_interrupts_child_search_without_applying_late_move(self):
        gate = threading.Event()
        board = self.prepare(gate=gate)
        before = board.fen()
        with ThreadPoolExecutor(max_workers=2) as pool:
            running = pool.submit(self.game.engine_move, self.game.revision)
            self.wait_for_second_search()
            closing = pool.submit(self.game.close)
            self.assertTrue(gate.wait(3), "Close must interrupt safety child search")
            running.result(5)
            closing.result(5)
        self.assertEqual(self.game.board.fen(), before)
        self.assertIsNone(self.game.error)
        self.assertIsNone(self.game._engine)

    def test_clock_cut_cancels_safety_but_preserves_a_legal_move(self):
        gate = threading.Event()
        self.prepare(gate=gate)
        with ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(self.game.engine_move, self.game.revision)
            self.wait_for_second_search()
            with self.game._lock:
                self.game._clock._balances["white"] = 9000
            self.assertTrue(gate.wait(3), "Safety watchdog must notice low clock")
            state = running.result(5)
        self.assertIsNone(state["error"])
        self.assertEqual(state["last_move"], "b5a7")
        self.assertEqual(self.saved_decision()["tactical_safety"]["status"], "unverified")
        self.assertEqual(self.saved_decision()["tactical_safety"]["reason"], "cancelled")
        self.assertEqual(self.waits, [])

    def test_clock_expiry_rejects_late_move(self):
        gate = threading.Event()
        board = self.prepare(gate=gate)
        before = board.fen()
        with ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(self.game.engine_move, self.game.revision)
            self.wait_for_second_search()
            self.now.advance(601)
            self.assertTrue(gate.wait(3))
            state = running.result(5)
        self.assertEqual(state["termination"], "time_forfeit")
        self.assertEqual(self.game.board.fen(), before)
        self.assertIsNone(state["error"])

    def test_invalidated_game_token_rejects_late_safety_result(self):
        gate = threading.Event()
        board = self.prepare(gate=gate)
        before = board.fen()
        with ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(self.game.engine_move, self.game.revision)
            self.wait_for_second_search()
            # Simulate the identity invalidation used by a replaced game;
            # public new_game itself correctly refuses an active search.
            with self.game._lock:
                self.game._game_token = object()
            self.assertTrue(gate.wait(3))
            state = running.result(5)
        self.assertEqual(self.game.board.fen(), before)
        self.assertIsNone(state["error"])


if __name__ == "__main__":
    unittest.main()
