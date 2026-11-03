"""Final quality audit through the real host, without real games or engines.

Native and external scores intentionally disagree. Only native peer results
may become moves; the independent referee supplies position evidence alone.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import threading
import time
import unittest
from unittest.mock import patch

import chess

import recovery_host_tests as support
from opening_variety_tests import info
from sparring_referee_host_tests import ControlledReferee, REFEREE_NAME, REFEREE_SHA
import sparring_safety_host_tests as safety_support
from sparring_state_tests import late_game_board
import sparring_referee

host = support.host


class QualityReferee(ControlledReferee):
    """Context-complete evidence, with controllable failures and cancellation."""

    def __init__(self):
        super().__init__()
        self.quality_calls = []
        self.root_cp = 0
        self.child_cp = {}
        self.root_pv = None
        self.quality_error = None
        self.mutate = None
        self.gate = None
        self.entered = threading.Event()
        self.cancelled = threading.Event()
        self.session_entries = 0
        self.session_exits = 0
        self.session_active = False
        self.session_required = True
        self.session_error = None

    @contextmanager
    def quality_session(self):
        if self.session_error is not None:
            raise self.session_error
        if self.session_active:
            raise AssertionError("Quality sessions must not be nested")
        self.session_entries += 1
        self.session_active = True
        try:
            yield self
        finally:
            self.session_active = False
            self.session_exits += 1

    @staticmethod
    def packet(position, own_turn, cp, move=None):
        selected = move or next(iter(position.legal_moves))
        white_cp = cp if own_turn else -cp
        return {"uci": selected.uci(), "cp": white_cp, "cp_white": white_cp,
                "cp_own": cp, "mate": None, "mate_white": None, "mate_own": None,
                "depth": 12, "nodes": 1000, "bound": "exact", "multipv": 1,
                "pv_uci": [selected.uci()], "white_share": .5, "wdl_white": None,
                "fen": position.fen(en_passant="fen"), "terminal": False, "winner": None}

    def assess_move_quality(self, **kwargs):
        if self.session_required and not self.session_active:
            raise AssertionError("Quality assessment must run inside its session")
        self.quality_calls.append({**kwargs, "board": kwargs["board"].copy(stack=True),
                                   "session": self.session_entries})
        self.entered.set()
        callback = kwargs.get("cancelled")
        deadline = time.monotonic() + 5
        while self.gate is not None and not self.gate.wait(.01):
            if callback is not None and callback():
                self.cancelled.set()
                return {"status": "unverified", "reason": "cancelled"}
            if time.monotonic() >= deadline:
                raise TimeoutError("controlled quality gate was not interrupted")
        if self.quality_error is not None:
            raise self.quality_error
        board, move = kwargs["board"], kwargs["selected_move"]
        child = board.copy(stack=True)
        child.push(move)
        receipt = {"source": sparring_referee.SOURCE, "policy": sparring_referee.POLICY,
                   "engine_name": REFEREE_NAME, "binary_sha256": REFEREE_SHA,
                   "available": True, "threads": 1, "hash_mb": 32,
                   "skill_level": 20, "limit_strength": False,
                   "game_id": kwargs["game_id"], "revision": kwargs["revision"],
                   "fen": board.fen(en_passant="fen"),
                   "turn": "white" if board.turn else "black",
                   "history_uci": [item.uci() for item in board.move_stack],
                   "selected_uci": move.uci(), "status": "ready",
                   "reason": "exact_root_and_child", "budget_ms": kwargs["budget_ms"],
                   "elapsed_ms": 1,
                   "root": self.packet(board, board.turn, self.root_cp, self.root_pv),
                   "child": self.packet(child, board.turn, self.child_cp.get(move.uci(), 0))}
        if self.mutate is not None:
            self.mutate(receipt)
        return receipt


class QualityHostTests(unittest.TestCase):
    wait = support.RecoveryHostTests.wait
    position = support.RecoveryHostTests.position
    queue = support.RecoveryHostTests.queue
    saved_decision = support.RecoveryHostTests.saved_decision
    wait_for_second_search = support.RecoveryHostTests.wait_for_second_search

    def setUp(self):
        support.RecoveryHostTests.setUp(self)
        self.referee = QualityReferee()
        self.game._referee = self.referee
        self.game._referee_sha256 = REFEREE_SHA
        self.game._referee_name = REFEREE_NAME
        self.game._referee_info = {"name": REFEREE_NAME, "sha256": REFEREE_SHA}
        original = host.sparring_play.choose

        def baseline_only(*args, **kwargs):
            # Retain real receipt construction while isolating this final gate
            # from earlier candidate discovery/recovery policy decisions.
            kwargs.update(movetime_ms=0, recovery_budget_ms=0)
            return original(*args, **kwargs)

        selection = patch.object(host.sparring_play, "choose", side_effect=baseline_only)
        selection.start()
        self.addCleanup(selection.stop)

    def prepare(self, ply=3, *, sparring=True, root_cp=0, child_cp=0):
        board = self.position(late_game_board(ply), sparring=sparring)
        # Keep ordinary discovery disabled, with ample time for final audits.
        self.game._clock._balances["white" if board.turn else "black"] = 179_000
        moves = [move for move in board.legal_moves
                 if not host.sparring_safety.inspect(board, move)["risky"]]
        self.assertGreaterEqual(len(moves), 3)
        self.referee.root_cp = root_cp
        self.referee.child_cp = {moves[0].uci(): child_cp}
        self.queue(board, moves[0].uci(), 17, cost_ms=0)
        return board.copy(stack=True), moves

    def replacement(self, board, move, *, external_cp=0, native_cp=41, **extra):
        self.queue(board, move.uci(), native_cp, cost_ms=0, **extra)
        self.referee.child_cp[move.uci()] = external_cp

    def assert_played(self, state, move):
        self.assertIsNone(state["error"])
        self.assertEqual(state["last_move"], move.uci())

    def test_baseline_only_is_audited_before20_for_both_colors(self):
        for ply in (38, 39):
            with self.subTest(ply=ply):
                board, moves = self.prepare(ply, root_cp=300, child_cp=30)
                self.referee.quality_calls.clear()
                state = self.game.engine_move(self.game.revision)
                self.assert_played(state, moves[0])
                self.assertEqual(len(self.peer.searches), 1)
                self.assertEqual(len(self.referee.quality_calls), 1)
                self.assertEqual(self.referee.quality_calls[0]["board"].fen(), board.fen())
                self.assertEqual(self.saved_decision()["quality_guard"]["status"], "verified")
                self.assertFalse(self.saved_decision()["quality_guard"]["repair_attempted"])

    def test_quiet_donation_is_replaced_only_by_a_native_search_result_and_saved(self):
        board, moves = self.prepare(child_cp=-330)
        incumbent, native, external_pv = moves[:3]
        self.referee.root_pv = external_pv
        self.replacement(board, native, external_cp=25, native_cp=41)
        state = self.game.engine_move(self.game.revision)
        self.assert_played(state, native)
        self.assertNotEqual(state["last_move"], external_pv.uci())
        self.assertEqual(len(self.peer.searches), 2)
        search = self.peer.searches[1]
        self.assertEqual(search["board"].fen(), board.fen())
        self.assertNotIn(incumbent, search["root_moves"])
        self.assertIn(native, search["root_moves"])
        self.assertEqual([row["selected_move"] for row in self.referee.quality_calls], [incumbent, native])
        self.assertEqual([row["session"] for row in self.referee.quality_calls], [1, 1])
        self.assertEqual((self.referee.session_entries, self.referee.session_exits), (1, 1))
        decision = self.saved_decision()
        self.assertEqual(decision["reason"], "quality_guard_replacement")
        self.assertEqual(decision["pre_quality_selection"]["selected_uci"], incumbent.uci())
        self.assertEqual(decision["selected_cp_own"], 41)
        receipt = decision["quality_guard"]
        self.assertEqual(receipt["initial"]["child"]["cp_own"], -330)
        self.assertTrue(receipt["applied"])
        self.assertEqual(receipt["selected_uci"], native.uci())
        saved = json.loads((self.game.save_dir / (self.game.game_id + ".json")).read_text(encoding="utf-8"))
        self.assertEqual(saved["sparring_play"]["decisions"][-1]["quality_guard"], receipt)
        self.assertEqual(self.waits, [], "Final verification must not add a cosmetic delay")

    def test_ramp_floor_uses_unrestricted_root_not_an_already_bad_native_baseline(self):
        board, moves = self.prepare(78, root_cp=250, child_cp=20)
        self.replacement(board, moves[1], external_cp=90)
        self.replacement(board, moves[2], external_cp=235)
        state = self.game.engine_move(self.game.revision)
        self.assert_played(state, moves[2])
        self.assertEqual(len(self.peer.searches), 3)
        self.assertNotIn(moves[0], self.peer.searches[2]["root_moves"])
        self.assertNotIn(moves[1], self.peer.searches[2]["root_moves"])
        receipt = self.saved_decision()["quality_guard"]
        self.assertEqual(len(receipt["attempts"]), 2)
        self.assertEqual(receipt["initial"]["root"]["cp_own"], 250)

    def test_best_verified_native_damage_limitation_replaces_known_collapse(self):
        board, moves = self.prepare(78, root_cp=250, child_cp=-400)
        generated = []
        original = self.peer.analysis

        def native_reply(position, limit, **options):
            # The controlled native engine must obey both global exclusions
            # and independently derived own-piece candidate groups.
            if options.get("root_moves"):
                move = options["root_moves"][0]
                index = len(generated)
                external = (-200, 170, 180)[index] if index < 3 else 110
                native = 111 * (index + 1)
                self.replacement(board, move, external_cp=external, native_cp=native,
                                 packets=[info(board, move.uci(), native, 10)])
                generated.append(move)
            return original(position, limit, **options)

        with patch.object(self.peer, "analysis", side_effect=native_reply):
            state = self.game.engine_move(self.game.revision)
        self.assertGreaterEqual(len(generated), 4)
        self.assert_played(state, generated[2])
        self.assertEqual(len(self.peer.searches), len(generated) + 1)
        self.assertEqual(len(self.referee.quality_calls), len(generated) + 1)
        decision = self.saved_decision()
        self.assertEqual(decision["reason"], "quality_guard_damage_limitation")
        self.assertEqual(decision["selected_cp_own"], 333, "External 180cp must not replace the native score")
        self.assertEqual(decision["pre_quality_selection"]["selected_uci"], moves[0].uci())
        receipt = decision["quality_guard"]
        self.assertEqual(receipt["status"], "mitigated")
        self.assertTrue(receipt["applied"])
        self.assertEqual(receipt["selected_uci"], generated[2].uci())
        self.assertEqual(receipt["final_verdict"]["child_cp_own"], 180)
        self.assertTrue(receipt["final_verdict"]["verified"])
        self.assertFalse(receipt["final_verdict"]["acceptable"], "Damage limitation is not a floor-pass claim")

    def test_clock_cut_does_not_apply_a_cached_damage_limitation_candidate(self):
        board, moves = self.prepare(78, root_cp=250, child_cp=-400)
        self.replacement(board, moves[1], external_cp=180, native_cp=333,
                         packets=[info(board, moves[1].uci(), 333, 10)])
        gate = threading.Event()
        self.replacement(board, moves[2], external_cp=170, native_cp=222,
                         packets=[info(board, moves[2].uci(), 222, 10)], gate=gate)
        with ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(self.game.engine_move, self.game.revision)
            for _ in range(3000):
                if len(self.peer.searches) >= 3:
                    break
                threading.Event().wait(.001)
            self.assertGreaterEqual(len(self.peer.searches), 3, "The cached mitigation must exist before cancellation")
            with self.game._lock:
                self.game._clock._balances["white" if board.turn else "black"] = 9000
            self.assertTrue(gate.wait(3))
            state = running.result(5)
        self.assert_played(state, moves[0])
        receipt = self.saved_decision()["quality_guard"]
        self.assertEqual(receipt["status"], "unverified")
        self.assertFalse(receipt["applied"])
        self.assertEqual(receipt["selected_uci"], moves[0].uci())
        self.assertEqual(receipt["final_verdict"], receipt["initial_verdict"])

    def test_move50_and_later_audit_baselines_without_seeking_close_concessions(self):
        for ply in (98, 99):
            with self.subTest(ply=ply):
                board, moves = self.prepare(ply, root_cp=250, child_cp=30)
                self.referee.quality_calls.clear()
                self.replacement(board, moves[1], external_cp=240)
                state = self.game.engine_move(self.game.revision)
                self.assert_played(state, moves[1])
                self.assertEqual(len(self.referee.quality_calls), 2)
                self.assertEqual(self.saved_decision()["phase"], "finish")
                self.assertFalse(self.saved_decision()["band_control"]["active"])
                self.assertTrue(self.saved_decision()["quality_guard"]["applied"])

    def test_quality_gate_runs_after_tactical_replacement(self):
        safety_support.SafetyHostTests.prepare(self)
        self.game._clock._balances["white"] = 179_000
        self.referee.child_cp = {"d4c4": 4}
        state = self.game.engine_move(self.game.revision)
        self.assert_played(state, chess.Move.from_uci("d4c4"))
        self.assertEqual([row["selected_move"].uci() for row in self.referee.quality_calls], ["d4c4"])
        self.assertEqual(self.saved_decision()["reason"], "tactical_safety_replacement")
        self.assertEqual(self.saved_decision()["quality_guard"]["original_uci"], "d4c4")
        self.assertEqual(len(self.peer.searches), 4)

    def test_normal_mode_does_not_run_quality_guard(self):
        _board, moves = self.prepare(sparring=False)
        with patch.object(host.sparring_quality, "verify", side_effect=AssertionError("normal-mode quality call")):
            state = self.game.engine_move(self.game.revision)
        self.assert_played(state, moves[0])
        self.assertEqual(self.referee.quality_calls, [])
        self.assertEqual(len(self.peer.searches), 1)

    def test_missing_referee_keeps_legal_incumbent_without_safety_claim(self):
        _board, moves = self.prepare()
        self.game._referee = None
        state = self.game.engine_move(self.game.revision)
        self.assert_played(state, moves[0])
        self.assertEqual(self.saved_decision()["quality_guard"]["status"], "skipped")
        self.assertEqual(len(self.peer.searches), 1)

    def test_referee_failure_keeps_legal_incumbent_as_unverified(self):
        _board, moves = self.prepare()
        self.referee.quality_error = RuntimeError("controlled assessor failure")
        state = self.game.engine_move(self.game.revision)
        self.assert_played(state, moves[0])
        self.assertEqual(self.saved_decision()["quality_guard"]["status"], "unverified")
        self.assertEqual(len(self.peer.searches), 1)
        self.assertEqual((self.referee.session_entries, self.referee.session_exits), (1, 1))

    def test_quality_session_startup_failure_keeps_a_legal_unverified_incumbent(self):
        _board, moves = self.prepare()
        self.referee.session_error = sparring_referee.RefereeUnavailable("controlled busy quality session")
        state = self.game.engine_move(self.game.revision)
        self.assert_played(state, moves[0])
        self.assertEqual(self.referee.quality_calls, [])
        self.assertEqual(self.saved_decision()["quality_guard"]["status"], "unverified")
        self.assertEqual(self.saved_decision()["quality_guard"]["reason"], "quality_session_unavailable")
        self.assertEqual(len(self.peer.searches), 1)

    def test_custom_assessor_without_session_api_remains_compatible(self):
        _board, moves = self.prepare()
        self.referee.quality_session = None
        self.referee.session_required = False
        state = self.game.engine_move(self.game.revision)
        self.assert_played(state, moves[0])
        self.assertEqual(len(self.referee.quality_calls), 1)
        self.assertEqual(self.saved_decision()["quality_guard"]["status"], "verified")

    def test_native_repair_failure_keeps_the_actual_incumbents_failed_verdict(self):
        board, moves = self.prepare(child_cp=-330)
        self.replacement(board, moves[1], failure=RuntimeError("controlled native repair failure"))
        state = self.game.engine_move(self.game.revision)
        self.assert_played(state, moves[0])
        receipt = self.saved_decision()["quality_guard"]
        self.assertEqual(receipt["status"], "unverified")
        self.assertFalse(receipt["applied"])
        self.assertTrue(receipt["repair_attempted"])
        self.assertEqual(receipt["selected_uci"], moves[0].uci())
        self.assertEqual(receipt["final_verdict"], receipt["initial_verdict"])
        self.assertFalse(receipt["final_verdict"]["acceptable"])
        self.assertEqual(receipt["final_verdict"]["child_cp_own"], -330)
        self.assertEqual(len(self.referee.quality_calls), 1)

    def test_clock_cut_interrupts_native_repair_and_discards_its_engine(self):
        board, moves = self.prepare(child_cp=-330)
        gate = threading.Event()
        self.replacement(board, moves[1], gate=gate)
        with ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(self.game.engine_move, self.game.revision)
            self.wait_for_second_search()
            with self.game._lock:
                self.game._clock._balances["white" if board.turn else "black"] = 9000
            self.assertTrue(gate.wait(3), "Clock cut must interrupt the native repair")
            state = running.result(5)
        self.assert_played(state, moves[0])
        self.assertIsNone(self.game._engine)
        receipt = self.saved_decision()["quality_guard"]
        self.assertEqual(receipt["status"], "unverified")
        self.assertEqual(receipt["reason"], "cancelled")
        self.assertFalse(receipt["applied"])
        self.assertEqual(receipt["final_verdict"], receipt["initial_verdict"])

    def test_close_interrupts_native_repair_without_applying_a_late_move(self):
        board, moves = self.prepare(child_cp=-330)
        gate = threading.Event()
        self.replacement(board, moves[1], gate=gate)
        with ThreadPoolExecutor(max_workers=2) as pool:
            running = pool.submit(self.game.engine_move, self.game.revision)
            self.wait_for_second_search()
            closing = pool.submit(self.game.close)
            self.assertTrue(gate.wait(3), "Close must interrupt the native repair")
            running.result(5)
            closing.result(5)
        self.assertEqual(self.game.board.fen(), board.fen())
        self.assertIsNone(self.game.error)
        self.assertIsNone(self.game._engine)

    def test_malformed_external_evidence_cannot_claim_verification(self):
        mutations = {
            "wrong_engine": lambda row: row.update(binary_sha256="c" * 64),
            "wrong_game": lambda row: row.update(game_id="stale-game"),
            "wrong_history": lambda row: row.update(history_uci=[]),
            "shallow_child": lambda row: row["child"].update(depth=9),
            "bounded_child": lambda row: row["child"].update(bound="upper"),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                _board, moves = self.prepare(child_cp=-330)
                self.referee.mutate = mutate
                state = self.game.engine_move(self.game.revision)
                self.assert_played(state, moves[0])
                self.assertEqual(self.saved_decision()["quality_guard"]["status"], "unverified")
                self.assertEqual(len(self.peer.searches), 1)

    def test_low_clock_skips_quality_without_claiming_safety(self):
        board, moves = self.prepare()
        self.game._clock._balances["white" if board.turn else "black"] = 9000
        state = self.game.engine_move(self.game.revision)
        self.assert_played(state, moves[0])
        self.assertEqual(self.referee.quality_calls, [])
        self.assertEqual(self.saved_decision()["quality_guard"]["status"], "skipped")
        self.assertEqual(self.saved_decision()["quality_guard"]["reason"], "insufficient_budget")
        self.assertEqual(len(self.peer.searches), 1)

    def test_close_interrupts_assessment_without_applying_late_move(self):
        board, _moves = self.prepare()
        before = board.fen()
        self.referee.gate = threading.Event()
        with ThreadPoolExecutor(max_workers=2) as pool:
            running = pool.submit(self.game.engine_move, self.game.revision)
            self.assertTrue(self.referee.entered.wait(3))
            closing = pool.submit(self.game.close)
            self.assertTrue(self.referee.cancelled.wait(3))
            running.result(5)
            closing.result(5)
        self.assertEqual(self.game.board.fen(), before)
        self.assertIsNone(self.game.error)
        self.assertEqual((self.referee.session_entries, self.referee.session_exits), (1, 1))

    def test_clock_cut_cancels_assessment_but_preserves_a_legal_move(self):
        board, moves = self.prepare()
        self.referee.gate = threading.Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(self.game.engine_move, self.game.revision)
            self.assertTrue(self.referee.entered.wait(3))
            with self.game._lock:
                self.game._clock._balances["white" if board.turn else "black"] = 9000
            self.assertTrue(self.referee.cancelled.wait(3))
            state = running.result(5)
        self.assert_played(state, moves[0])
        self.assertEqual(self.saved_decision()["quality_guard"]["status"], "unverified")
        self.assertEqual(self.waits, [])

    def test_clock_expiry_rejects_late_quality_result(self):
        board, _moves = self.prepare()
        before = board.fen()
        self.referee.gate = threading.Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(self.game.engine_move, self.game.revision)
            self.assertTrue(self.referee.entered.wait(3))
            self.now.advance(180)
            self.assertTrue(self.referee.cancelled.wait(3))
            state = running.result(5)
        self.assertEqual(state["termination"], "time_forfeit")
        self.assertEqual(self.game.board.fen(), before)
        self.assertIsNone(state["error"])

    def test_invalidated_game_token_rejects_late_quality_result(self):
        board, _moves = self.prepare()
        before = board.fen()
        self.referee.gate = threading.Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(self.game.engine_move, self.game.revision)
            self.assertTrue(self.referee.entered.wait(3))
            with self.game._lock:
                self.game._game_token = object()
            self.assertTrue(self.referee.cancelled.wait(3))
            state = running.result(5)
        self.assertEqual(self.game.board.fen(), before)
        self.assertIsNone(state["error"])


if __name__ == "__main__":
    unittest.main()
