"""Tactical-policy wiring through HumanGame; mocked choices are not chess evidence."""
import copy
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import chess
import chess.engine

import human_play_tests as harness
from opening_variety_tests import Peer

host = harness.play_human
HISTORY = "e2e4 e7e5 g1f3 b8c6 f1c4 d7d6 b1c3 c8g4".split()


def line(board, uci, cp=50):
    move = chess.Move.from_uci(uci)
    after = board.copy(stack=True)
    after.push(move)
    reply = next(iter(after.legal_moves))
    return {"depth": 4, "nodes": 500,
            "score": chess.engine.PovScore(chess.engine.Cp(cp), board.turn),
            "pv": [move, reply]}


class TacticalHostTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="regicide-tactics-host-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        binary = self.root / "controlled-peer.exe"
        binary.write_bytes(b"test peer, not executable")
        self.peer = Peer()
        self.game = host.HumanGame(binary, self.root / "games", lambda _: self.peer,
            hint_engine_factory=harness.FakeFactory(), evaluation_engine_factory=harness.FakeFactory(),
            opening_variety_enabled=False, dynamic_play_enabled=False)
        self.addCleanup(self.game.close)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.observer = self.stack.enter_context(patch.object(host.boss_play, "observe_human_move",
            return_value=({"valid": False, "reason": "controlled_no_observation", "elapsed_ms": 0}, False)))
        self.tactical_calls = []

    def prepare(self, *, ms=1000, history=HISTORY, admit=False, boss=True, control=None,
                baseline="e1g1", forced=False):
        self.game.new_game("black", ms, self.game.revision, boss_mode=boss, time_control=control)
        board = chess.Board()
        for ply, uci in enumerate(history, 1):
            if control is not None:
                self.assertTrue(self.game._clock.complete_move("white" if board.turn else "black", ply))
            board.push_uci(uci)
        self.game.board = board.copy(stack=True)
        self.game._opening_seed = next(f"{n:032x}" for n in range(4096)
            if (host.boss_play.boss_opponent.gate(board, [], f"{n:032x}")["draw"] < .01 if admit else
                host.boss_play.boss_opponent.gate(board, [], f"{n:032x}")["draw"] > .95))
        self.game._forced_plies = {1} if forced else set()
        self.game.revision += 1
        self.game._sync_evaluation()
        self.game._save()
        self.peer.scripts = [{"move": baseline, "packets": [line(board, baseline)]}]
        return board

    def receipt(self, board, plan, baseline_move, info, seed, selected, reason):
        row = host.boss_play.receipt(board, {**plan, "eligible": False, "reason": reason},
                                    baseline_move, info, seed=seed)
        row.update(selected_uci=selected, candidate_uci=selected, reason=reason)
        return row

    def tactics(self, *, qualified=True, selected="f3e5", discard=False, delay=0, entered=None, gate=None):
        def choose(board, plan, baseline_move, info, packets, seed, search, close_engine, **kwargs):
            self.tactical_calls.append({"board": board.copy(stack=True), "plan": copy.deepcopy(plan),
                                        "baseline": baseline_move.uci(), "packets": copy.deepcopy(packets)})
            if entered is not None:
                entered.set()
            if gate is not None and not gate.wait(3):
                raise TimeoutError("controlled tactic release")
            if delay:
                time.sleep(delay)
            move = selected if qualified and not discard else baseline_move.uci()
            row = self.receipt(board, plan, baseline_move, info, seed, move, "controlled_tactical_choice")
            row["tactics"] = {"qualified": qualified, "selected_uci": move,
                              "applied": move != baseline_move.uci(), "reason": "controlled_test_result",
                              "budget_ms": plan["extra_budget_ms"], "extra_elapsed_ms": delay * 1000}
            return row, discard
        return self.stack.enter_context(patch.object(host.boss_play, "explore_tactics", side_effect=choose))

    def no_legacy_selection(self):
        return [self.stack.enter_context(patch.object(host.boss_play, name,
            side_effect=AssertionError("qualified tactics must bypass " + name)))
            for name in ("explore_initiative", "explore_traps")]

    def test_qualified_tactics_precede_closed_speculative_frequency_gate(self):
        before = self.prepare()
        self.assertEqual(host.boss_play.plan(before, 1000, self.game._opening_seed)["mode"], "trap_hunt")
        tactical = self.tactics()
        self.no_legacy_selection()
        state = self.game.engine_move(self.game.revision)
        self.assertIsNone(state["error"])
        self.assertEqual(state["last_move"], "f3e5")
        tactical.assert_called_once()
        row = self.game._boss_decisions[9]
        self.assertFalse(row["frequency"]["attempt_trap"])
        self.assertTrue(row["tactics"]["qualified"])
        self.assertEqual([m.uci() for m in self.tactical_calls[0]["board"].move_stack], HISTORY)
        self.assertEqual(self.peer.searches[0]["board"].move_stack, before.move_stack)
        self.assertEqual(self.peer.searches[0]["limit"].time, 1)
        self.assertLessEqual(row["extra_budget_ms"], 1000)

    def test_unqualified_tactics_leave_only_actual_remaining_budget_for_legacy_path(self):
        self.prepare(admit=True)
        self.tactics(qualified=False, delay=.02)
        calls = []
        def legacy(board, plan, move, info, packets, seed, search, close, **kwargs):
            calls.append(copy.deepcopy(plan))
            return self.receipt(board, plan, move, info, seed, "f3g5", "controlled_legacy_choice"), False
        fallback = self.stack.enter_context(patch.object(host.boss_play, "explore_traps", side_effect=legacy))
        state = self.game.engine_move(self.game.revision)
        self.assertIsNone(state["error"])
        self.assertEqual(state["last_move"], "f3g5")
        fallback.assert_called_once()
        self.assertLess(calls[0]["extra_budget_ms"], self.tactical_calls[0]["plan"]["extra_budget_ms"] - 10)
        self.assertGreater(calls[0]["extra_budget_ms"], 0)
        self.assertFalse(self.game._boss_decisions[9]["tactics"]["qualified"])

    def test_post_sacrifice_initiative_also_gives_verified_tactics_priority(self):
        history = "e2e4 e7e5 f2f4 e5f4".split()
        before = self.prepare(history=history, baseline="f1c4")
        self.assertEqual(host.boss_play.plan(before, 1000, self.game._opening_seed)["mode"], "initiative")
        tactical = self.tactics(selected="b1c3")
        self.no_legacy_selection()
        state = self.game.engine_move(self.game.revision)
        self.assertIsNone(state["error"])
        self.assertEqual(state["last_move"], "b1c3")
        self.assertTrue(self.game._boss_decisions[5]["tactics"]["qualified"])
        tactical.assert_called_once()

    def test_discarded_tactical_probe_preserves_baseline_without_legacy_probe(self):
        self.prepare(admit=True)
        self.tactics(qualified=False, discard=True)
        legacy = self.no_legacy_selection()
        state = self.game.engine_move(self.game.revision)
        self.assertIsNone(state["error"])
        self.assertEqual(state["last_move"], "e1g1")
        self.assertEqual(self.game._boss_decisions[9]["selected_uci"], "e1g1")
        for selection in legacy:
            selection.assert_not_called()

    def test_qualified_baseline_is_protected_from_later_speculative_replacement(self):
        self.prepare(admit=True)
        self.tactics(selected="e1g1")
        self.no_legacy_selection()
        state = self.game.engine_move(self.game.revision)
        self.assertEqual(state["last_move"], "e1g1")
        self.assertTrue(self.game._boss_decisions[9]["tactics"]["qualified"])
        self.assertFalse(self.game._boss_decisions[9]["tactics"]["applied"])

    def test_real_selector_sends_two_finalists_and_saves_quality_without_a_grade(self):
        # Exercise the real selector and production callback. Only geometry and
        # UCI answers are controlled; this checks wiring, not chess strength.
        board = self.prepare()
        candidate = "f3e5"
        first = line(board, candidate, cp=40)
        deeper = {**line(board, candidate, cp=55), "depth": 5}
        self.peer.scripts.extend([
            {"move": candidate, "packets": [first]},
            {"move": candidate, "packets": [first, deeper]},
        ])
        self.stack.enter_context(patch.object(host.boss_play.gambit_tactics.sound_sacrifice, "evidence",
            side_effect=lambda _board, move, **_: {"eligible": move.uci() == candidate,
                "complete": True, "offers": [], "investment_cp": 300}))
        self.no_legacy_selection()
        state = self.game.engine_move(self.game.revision)
        self.assertIsNone(state["error"])
        self.assertEqual(state["last_move"], candidate)
        self.assertEqual(len(self.peer.searches), 3)  # baseline + two optional probes
        final_search = self.peer.searches[-1]
        self.assertEqual(final_search["limit"].depth, 5)
        self.assertEqual({m.uci() for m in final_search["root_moves"]}, {candidate, "e1g1"})
        decision = self.game._boss_decisions[9]
        self.assertEqual(decision["policy"], "regicide-boss-play-v9")
        self.assertTrue(decision["tactics"]["quality_qualified"])
        self.assertEqual(decision["compared_depth"], 4)
        self.assertEqual(decision["final_best_defense_cp"], 55)
        self.assertFalse(decision["tactics"]["evidence"]["paired_verification"]["global_best_proven"])
        stored = json.loads((self.game.save_dir / f"{self.game.game_id}.json").read_text(encoding="utf-8"))
        saved = stored["boss_play"]["decisions"][0]
        self.assertEqual(saved["tactics"]["classification_scope"], "local_native_verification_not_chesscom_grade")
        self.assertNotIn("annotation", saved)

    def test_tactical_receipt_survives_save_and_memo_replay_but_undo_removes_played_receipt(self):
        self.prepare()
        tactical = self.tactics()
        self.no_legacy_selection()
        self.game.engine_move(self.game.revision)
        stored = json.loads((self.game.save_dir / f"{self.game.game_id}.json").read_text(encoding="utf-8"))
        decision = stored["boss_play"]["decisions"][0]
        self.assertEqual(decision["tactics"]["selected_uci"], "f3e5")
        self.assertEqual(decision["history_uci"], HISTORY)
        self.assertNotIn("annotation", decision, "play receipt is not a Chess.com or local badge verdict")
        self.game.undo(self.game.revision)
        self.assertNotIn(9, self.game._boss_decisions)
        self.assertEqual([m.uci() for m in self.game.board.move_stack], HISTORY[:-1])
        self.game.human_move(HISTORY[-1], self.game.revision)
        replayed = self.game.engine_move(self.game.revision)
        self.assertEqual(replayed["last_move"], "f3e5")
        self.assertTrue(self.game._boss_decisions[9]["reused"])
        self.assertEqual(len(self.peer.searches), 1)
        tactical.assert_called_once()

    def test_short_budget_forced_game_and_normal_mode_do_not_invoke_tactical_selector(self):
        tactical = self.tactics()
        for parameters in ({"ms": 100}, {"forced": True}, {"boss": False}):
            with self.subTest(parameters=parameters):
                self.prepare(**parameters)
                state = self.game.engine_move(self.game.revision)
                self.assertIsNone(state["error"])
                self.assertEqual(state["last_move"], "e1g1")
        tactical.assert_not_called()

    def test_authored_opening_stays_on_existing_path(self):
        self.prepare(history=[], baseline="e2e4")
        plan = host.boss_play.plan(self.game.board, 1000, self.game._opening_seed)
        self.assertEqual(plan["mode"], "opening")
        actual = plan["candidate_uci"]
        self.peer.scripts = [{"move": actual, "packets": [line(self.game.board, actual)]}]
        tactical = self.tactics()
        state = self.game.engine_move(self.game.revision)
        self.assertIsNone(state["error"])
        self.assertEqual(state["last_move"], actual)
        tactical.assert_not_called()

    def test_low_clock_disables_optional_tactics(self):
        self.prepare(control="10+0")
        self.game._clock._balances["white"] = 10000
        tactical = self.tactics()
        state = self.game.engine_move(self.game.revision)
        self.assertIsNone(state["error"])
        self.assertEqual(state["last_move"], "e1g1")
        tactical.assert_not_called()
        self.assertLessEqual(self.peer.searches[0]["limit"].time, 1)

    def test_late_tactical_result_cannot_mutate_newer_game_revision(self):
        self.prepare()
        entered, release = threading.Event(), threading.Event()
        self.tactics(entered=entered, gate=release)
        before = self.game.revision
        record = self.game.save_dir / f"{self.game.game_id}.json"
        saved = record.read_bytes()
        with ThreadPoolExecutor(max_workers=1) as executor:
            task = executor.submit(self.game.engine_move, before)
            try:
                self.assertTrue(entered.wait(3))
                with self.game._lock:
                    self.game._game_token = object()
            finally:
                release.set()
            state = task.result(timeout=3)
        self.assertIsNone(state["error"])
        self.assertEqual([m.uci() for m in self.game.board.move_stack], HISTORY)
        self.assertEqual(self.game._boss_decisions, {})
        self.assertEqual(self.game._boss_memo, {})
        self.assertEqual(record.read_bytes(), saved)


if __name__ == "__main__":
    unittest.main(verbosity=2)
