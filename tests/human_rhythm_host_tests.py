"""Post-search timing integration with controlled packets and injected clocks.

No external engine or user game is used. These tests inspect which evidence
crosses the timing boundary and independently reconcile actual clock charges.
"""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import chess
import chess.engine

import human_play_tests as base
from game_clock_tests import Now
from opening_variety_tests import Peer

host = base.play_human


class PeerClock(Now):
    # Reuse the scored-stream peer's explicit cost injection.
    @property
    def now(self):
        return self.value

    @now.setter
    def now(self, value):
        self.value = value


def packet(board, moves, cp=30, depth=6, **extra):
    pv = [chess.Move.from_uci(move) for move in moves.split()]
    return {"depth": depth, "score": chess.engine.PovScore(chess.engine.Cp(cp), board.turn),
            "pv": pv, **extra}


class HumanRhythmHostTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="regicide-human-rhythm-host-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.binary = self.root / "controlled-peer.exe"
        self.binary.write_bytes(b"controlled UCI peer, never executed")
        self.now = PeerClock()
        self.peer = Peer()
        self.peer.clock = self.now
        self.waits = []
        self.game = host.HumanGame(self.binary, self.root / "games", lambda _: self.peer,
            hint_engine_factory=base.FakeFactory(), evaluation_engine_factory=base.FakeFactory(),
            opening_variety_enabled=False, monotonic=self.now, pacing_wait=self.advance_wait)
        self.addCleanup(self.game.close)
        self.plan_calls, self.refine_calls = [], []
        self.refined_target = None
        self.on_refine = None
        self.plan_patch = patch.object(host.engine_pacing, "plan", side_effect=self.controlled_plan)
        self.refine_patch = patch.object(host.engine_pacing, "refine", side_effect=self.controlled_refine, create=True)
        self.plan_patch.start()
        self.refine_patch.start()
        self.addCleanup(self.plan_patch.stop)
        self.addCleanup(self.refine_patch.stop)

    def controlled_plan(self, board, clock, evaluation=None, *, seed=None, history=None):
        self.plan_calls.append({"board": board.copy(stack=True), "clock": copy.deepcopy(clock),
                                "history": copy.deepcopy(history), "seed": seed})
        color = "white" if board.turn else "black"
        return {"policy": host.engine_pacing.POLICY, "enabled": clock.get("enabled") is True,
                "color": color, "root_ply": len(board.move_stack), "before_fen": board.fen(en_passant="fen"),
                "recommended_total_ms": 10000, "target_total_ms": 10000, "reserve_ms": 10000,
                "spent_at_request_ms": clock.get("turn_elapsed_ms", 0),
                "initial_remaining_ms": clock.get(color + "_ms"), "pressure_factor": 1.0,
                "variation_factor": 1.0, "rhythm_profile": "controlled_test",
                "reason": "controlled_total_turn" if clock.get("enabled") else "untimed"}

    def controlled_refine(self, board, pacing, clock, *, selected_move, baseline_packets,
                          selected_info, history=None, seed=None):
        self.assertIsNot(history, self.game._engine_pacing_decisions)
        self.refine_calls.append({"board": board.copy(stack=True), "pacing": copy.deepcopy(pacing),
            "clock": copy.deepcopy(clock), "selected_move": selected_move,
            "baseline_packets": copy.deepcopy(baseline_packets), "selected_info": copy.deepcopy(selected_info),
            "history": copy.deepcopy(history), "seed": seed})
        if self.on_refine:
            self.on_refine()
        refined = copy.deepcopy(pacing)
        if self.refined_target is not None:
            refined["target_total_ms"] = self.refined_target
        refined["selected_uci"] = selected_move.uci()
        refined["refinement"] = {"kind": "controlled_test"}
        return refined

    def advance_wait(self, seconds, cancel_event):
        self.waits.append(seconds)
        self.now.advance(seconds)

    def new(self, color="black", *, sparring=False, control="10+0"):
        state = self.game.new_game(color, 1000, self.game.revision,
                                   sparring_mode=sparring, time_control=control)
        self.game._opening_seed = "human-rhythm-host"
        return state

    def script(self, moves, *, cost_ms=0, cp=30, depths=(4, 5, 6), board=None, extra_packets=()):
        board = self.game.board if board is None else board
        self.peer.scripts.append({"move": moves.split()[0],
            "packets": [packet(board, moves, cp, depth) for depth in depths] + list(extra_packets),
            "cost_ms": cost_ms})

    def engine(self):
        state = self.game.engine_move(self.game.revision)
        self.assertIsNone(state["error"])
        return state

    def human(self, uci):
        return self.game.human_move(uci, self.game.revision)

    def record(self):
        return json.loads((self.game.save_dir / (self.game.game_id + ".json")).read_text(encoding="utf-8"))

    def test_fresh_clock_total_elapsed_and_original_allocation_are_preserved(self):
        self.new(sparring=False)
        self.now.advance(.75)
        self.script("e2e4 e7e5 g1f3", cost_ms=600)
        self.refined_target = 4000
        self.engine()
        self.assertEqual(len(self.refine_calls), 1)
        call = self.refine_calls[0]
        self.assertAlmostEqual(call["clock"]["turn_elapsed_ms"], 1350, delta=.01)
        self.assertAlmostEqual(call["clock"]["white_ms"], 598650, delta=.01)
        self.assertEqual(call["selected_move"].uci(), "e2e4")
        self.assertEqual(call["selected_info"]["depth"], 6)
        self.assertEqual(sorted(call["baseline_packets"]), [4, 5, 6])
        receipt = self.game._engine_pacing_decisions[1]
        self.assertEqual(receipt["allocation"], call["pacing"]["allocation"])
        self.assertEqual(self.game._engine_movetimes[1], call["pacing"]["allocation"]["baseline_ms"])
        self.assertEqual(len(self.peer.searches), 1)
        self.assertAlmostEqual(self.game._clock.history[1]["elapsed_ms"], 4000, delta=1)
        self.assertAlmostEqual(sum(self.waits) * 1000, 2650, delta=1)

    def test_alternative_pv_is_used_and_child_or_bound_packets_never_leak(self):
        self.new("white", sparring=True)
        self.human("e2e4")
        board = self.game.board.copy(stack=True)
        self.script("c7c5 g1f3 b8c6", cost_ms=400)
        candidate = packet(board, "e7e5 g1f3 b8c6", cp=28, depth=6)
        self.peer.scripts.append({"move": "e7e5", "cost_ms": 200,
            "packets": [packet(board, "e7e5 g1f3 b8c6", 28, 4), candidate,
                        packet(board, "e7e5 g1f3 b8c6", 900, 12, lowerbound=True),
                        {"depth": 20, "nodes": 999999}]})
        child = board.copy(stack=True)
        child.push_uci("e7e5")
        self.script("g1f3 b8c6 f1b5", cost_ms=100, cp=999, depths=(30,), board=child)

        def choose(position, best, final, packets, search, close, **kwargs):
            search(300, root_move=chess.Move.from_uci("e7e5"), publish=False, fresh=True, depth=6)
            # Even publish=True is suppressed for an after-board search.
            search(300, position=child, publish=True, fresh=True, depth=30)
            return {"policy": host.sparring_play.POLICY, "ply": len(position.move_stack) + 1,
                    "before_fen": position.fen(en_passant="fen"),
                    "history_uci": [move.uci() for move in position.move_stack],
                    "selected_uci": "e7e5", "baseline_uci": best.uci(), "reason": "controlled_test",
                    "opponent_observation": {"valid": False}}, False

        with patch.object(host.sparring_play, "choose", side_effect=choose):
            state = self.engine()
        self.assertEqual(state["last_move"], "e7e5")
        call = self.refine_calls[0]
        self.assertEqual(call["selected_move"].uci(), "e7e5")
        self.assertEqual(call["selected_info"], candidate)
        self.assertEqual(sorted(call["baseline_packets"]), [4, 5, 6])
        self.assertTrue(all(row["pv"][0].uci() == "c7c5" for row in call["baseline_packets"].values()))
        self.assertAlmostEqual(call["clock"]["turn_elapsed_ms"], 700, delta=.01)

    def test_selected_move_without_exact_root_evidence_receives_no_baseline_pv(self):
        self.new("white", sparring=True)
        self.human("e2e4")
        self.script("c7c5 g1f3 b8c6")

        def choose(board, best, *args, **kwargs):
            return {"policy": host.sparring_play.POLICY, "ply": len(board.move_stack) + 1,
                    "before_fen": board.fen(en_passant="fen"),
                    "history_uci": [move.uci() for move in board.move_stack],
                    "selected_uci": "e7e5", "baseline_uci": best.uci(), "reason": "controlled_missing_evidence",
                    "opponent_observation": {"valid": False}}, False

        with patch.object(host.sparring_play, "choose", side_effect=choose):
            self.engine()
        self.assertIn(self.refine_calls[0]["selected_info"], (None, {}))
        self.assertEqual(self.refine_calls[0]["selected_move"].uci(), "e7e5")

    def test_search_past_refined_target_adds_no_fresh_delay(self):
        self.new()
        self.script("e2e4 e7e5", cost_ms=20000)
        self.refined_target = 3500
        self.engine()
        self.assertEqual(self.waits, [])
        self.assertEqual(self.game._clock.history[1]["elapsed_ms"], 20000)
        self.assertEqual(self.game._engine_pacing_decisions[1]["spent_before_wait_ms"], 20000)

    def test_cached_move_refines_without_inventing_fresh_search_evidence(self):
        self.new("white")
        self.game.opening_variety_enabled = True
        self.human("e2e4")
        self.script("c7c5 g1f3 b8c6")
        self.engine()
        searches = len(self.peer.searches)
        self.assertTrue(self.game._opening_memo)
        self.game.undo(self.game.revision)
        self.human("e2e4")
        state = self.engine()
        self.assertEqual(state["last_move"], "c7c5")
        self.assertEqual(len(self.peer.searches), searches)
        self.assertTrue(self.game._opening_decisions[2]["reused"])
        self.assertEqual(self.refine_calls[-1]["baseline_packets"], {})
        self.assertIn(self.refine_calls[-1]["selected_info"], (None, {}))

    def test_refinement_failure_preserves_legal_move_and_original_pacing(self):
        self.new()
        self.script("e2e4 e7e5")
        def fail():
            raise ValueError("controlled timing failure")
        self.on_refine = fail
        state = self.engine()
        self.assertEqual(state["last_move"], "e2e4")
        self.assertEqual(self.game._engine_pacing_decisions[1]["target_total_ms"], 10000)
        self.assertAlmostEqual(self.game._clock.history[1]["elapsed_ms"], 10000, delta=1)

    def test_late_client_time_already_beyond_refined_target_is_not_repaid(self):
        self.new()
        self.now.advance(3)
        self.script("e2e4 e7e5", cost_ms=200)
        self.refined_target = 2000
        self.engine()
        self.assertEqual(self.waits, [])
        self.assertAlmostEqual(self.game._clock.history[1]["elapsed_ms"], 3200, delta=.01)

    def test_history_is_detached_and_undo_removes_discarded_future_rhythm(self):
        self.new()
        self.script("e2e4 e7e5 g1f3")
        self.engine()
        first = copy.deepcopy(self.game._engine_pacing_decisions[1])
        self.human("e7e5")
        self.script("g1f3 b8c6 f1b5")
        self.engine()
        self.assertEqual(self.plan_calls[-1]["history"], {1: first})
        self.assertEqual(self.refine_calls[-1]["history"], {1: first})
        self.assertEqual(set(self.game._engine_pacing_decisions), {1, 3})
        self.game.undo(self.game.revision)
        self.assertEqual(self.game._engine_pacing_decisions, {1: first})
        self.human("c7c5")
        self.script("g1f3 b8c6 d2d4")
        self.engine()
        self.assertEqual(self.plan_calls[-1]["history"], {1: first})
        self.assertEqual(self.refine_calls[-1]["history"], {1: first})
        self.plan_calls[-1]["history"][1]["test_mutation"] = True
        self.assertNotIn("test_mutation", self.game._engine_pacing_decisions[1])

    def test_handicap_cancel_restores_rhythm_and_forced_branch_discards_it(self):
        self.new()
        self.script("e2e4 e7e5 g1f3")
        self.engine()
        self.human("e7e5")
        self.script("g1f3 b8c6 f1b5")
        self.engine()
        before = copy.deepcopy(self.game._engine_pacing_decisions)
        self.game.begin_handicap(1, self.game.revision)
        self.assertEqual(self.game._engine_pacing_decisions, {})
        self.game.cancel_handicap(self.game.revision)
        self.assertEqual(self.game._engine_pacing_decisions, before)
        self.game.begin_handicap(1, self.game.revision)
        state = self.game.forced_move("d2d4", self.game.revision)
        self.assertFalse(state["clock"]["enabled"])
        self.assertEqual(self.game._engine_pacing_decisions, {})

    def test_new_game_resets_history_and_game_seed(self):
        self.new()
        self.script("e2e4 e7e5")
        self.engine()
        seed = self.game._opening_seed
        self.game.new_game("black", 1000, self.game.revision, time_control="10+0")
        self.assertNotEqual(self.game._opening_seed, seed)
        self.script("d2d4 d7d5")
        self.engine()
        self.assertEqual(self.plan_calls[-1]["history"], {})
        self.assertEqual(self.refine_calls[-1]["history"], {})
        self.assertEqual(set(self.game._engine_pacing_decisions), {1})

    def test_refinement_cannot_reenable_explicit_skip_wait(self):
        self.new()
        self.game.skip_engine_wait(self.game.revision)
        self.script("e2e4 e7e5", cost_ms=300)
        self.refined_target = 20000
        self.engine()
        self.assertEqual(self.waits, [])
        self.assertEqual(self.game._engine_pacing_decisions[1]["completion_reason"], "client_closing")
        self.assertAlmostEqual(self.game._clock.history[1]["elapsed_ms"], 300, delta=.01)

    def test_clock_flag_during_refinement_discards_selected_move(self):
        self.new()
        self.script("e2e4 e7e5")
        self.on_refine = lambda: self.now.advance(601)
        state = self.engine()
        self.assertEqual(state["termination"], "time_forfeit")
        self.assertEqual(state["moves"], [])
        self.assertEqual(self.game._engine_pacing_decisions, {})

    def test_revision_change_during_refinement_discards_selected_move(self):
        self.new()
        self.script("e2e4 e7e5")
        def change():
            with self.game._lock:
                self.game.revision += 1
        self.on_refine = change
        state = self.engine()
        self.assertEqual(state["moves"], [])
        self.assertEqual(self.game._engine_pacing_decisions, {})

    def test_close_during_refinement_does_not_apply_or_wait_for_selected_move(self):
        self.new()
        self.script("e2e4 e7e5")
        entered, release = threading.Event(), threading.Event()
        def block():
            entered.set()
            if not release.wait(3):
                raise AssertionError("refinement test gate was not released")
        self.on_refine = block
        with ThreadPoolExecutor(max_workers=2) as pool:
            moving = pool.submit(self.engine)
            self.assertTrue(entered.wait(2))
            closing = pool.submit(self.game.close)
            deadline = time.monotonic() + 2
            while not self.game._closing and time.monotonic() < deadline:
                time.sleep(.005)
            self.assertTrue(self.game._closing)
            release.set()
            state = moving.result(timeout=3)
            closing.result(timeout=3)
        self.assertEqual(state["moves"], [])
        self.assertEqual(self.waits, [])
        self.assertEqual(self.record()["uci_moves"], [])


if __name__ == "__main__":
    unittest.main()
