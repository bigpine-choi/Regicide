"""Total-turn pacing, time pressure, automatic sparring and interrupt safety."""
from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

import chess
import chess.engine

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import decision_support
import engine_pacing
from game_clock import GameClock
from game_clock_tests import Now
from human_play_tests import FakeFactory
from play_human import HumanGame, RevisionError


class PacingPlanTests(unittest.TestCase):
    def setUp(self):
        self.board = chess.Board(); self.now = Now()
        self.clock = GameClock("10+0", monotonic=self.now).snapshot()

    def at(self, remaining):
        return {**self.clock, "white_ms": remaining}

    def test_shared_recommendation_with_engine_side_as_human(self):
        result = engine_pacing.plan(self.board, self.clock)
        shared = decision_support.assess(self.board, chess.WHITE, self.clock)["recommendation"]
        self.assertTrue(result["enabled"])
        self.assertEqual(result["recommended_total_ms"], shared["total_ms"])
        self.assertEqual(result["target_total_ms"], shared["total_ms"] - 1000)

    def test_already_spent_time_is_subtracted_not_added(self):
        plan = engine_pacing.plan(self.board, self.clock)
        later = {**self.clock, "turn_elapsed_ms": 3000, "white_ms": 597000}
        self.assertEqual(engine_pacing.remaining_wait_ms(plan, later), max(0, plan["target_total_ms"] - 3000))

    def test_search_past_target_needs_no_hold(self):
        plan = engine_pacing.plan(self.board, self.clock)
        later = {**self.clock, "turn_elapsed_ms": 60000, "white_ms": 540000}
        self.assertEqual(engine_pacing.remaining_wait_ms(plan, later), 0)

    def test_four_minute_boundary_and_monotonic_pressure(self):
        plans = [engine_pacing.plan(self.board, self.at(ms)) for ms in [240000, 239999, 180000, 120000, 60000]]
        self.assertEqual(plans[0]["pressure_factor"], 1)
        self.assertLess(plans[1]["pressure_factor"], 1)
        self.assertLessEqual(abs(plans[0]["target_total_ms"] - plans[1]["target_total_ms"]), 1)
        targets = [p["target_total_ms"] for p in plans]
        self.assertEqual(targets, sorted(targets, reverse=True))
        self.assertAlmostEqual(plans[2]["pressure_factor"], 7 / 8)
        self.assertAlmostEqual(plans[3]["pressure_factor"], 3 / 4)
        self.assertAlmostEqual(plans[4]["pressure_factor"], 5 / 8)

    def test_automatic_budget_independent_of_old_manual_setting(self):
        plan = engine_pacing.plan(self.board, self.clock)
        a = engine_pacing.search_budget(plan, self.clock, 100, automatic=True)
        b = engine_pacing.search_budget(plan, self.clock, 5000, automatic=True)
        self.assertEqual(a, b)
        self.assertLessEqual(a["baseline_ms"], 3000)
        self.assertLessEqual(a["baseline_ms"], a["total_search_ms"])
        self.assertGreater(a["completion_margin_ms"], 0)

    def test_low_clock_omits_hold_but_still_caps_search(self):
        for remaining in (10000, 1000, 100, 1):
            with self.subTest(remaining=remaining):
                clock = self.at(remaining)
                plan = engine_pacing.plan(self.board, clock)
                self.assertFalse(plan["enabled"])
                self.assertEqual(engine_pacing.remaining_wait_ms(plan, clock), 0)
                budget = engine_pacing.search_budget(plan, clock, 5000, automatic=True)
                self.assertLessEqual(budget["baseline_ms"], remaining)
                self.assertLess(budget["baseline_ms"], 5000)

    def test_low_clock_is_rechecked_during_hold(self):
        plan = engine_pacing.plan(self.board, self.at(20000))
        self.assertTrue(plan["enabled"])
        later = {**self.at(9999), "turn_elapsed_ms": 10001}
        self.assertEqual(engine_pacing.remaining_wait_ms(plan, later), 0)

    def test_future_increment_cannot_fund_current_turn(self):
        clock = {**self.at(11000), "increment_ms": 10000}
        plan = engine_pacing.plan(self.board, clock)
        budget = engine_pacing.search_budget(plan, clock, 5000, automatic=True)
        self.assertLess(budget["total_search_ms"], 11000)
        self.assertLessEqual(engine_pacing.remaining_wait_ms(plan, clock), 1000)

    def test_untimed_legacy_keeps_exact_search_limit(self):
        clock = GameClock(None, monotonic=self.now).snapshot()
        plan = engine_pacing.plan(self.board, clock)
        self.assertFalse(plan["enabled"])
        self.assertEqual(engine_pacing.search_budget(plan, clock, 1000, automatic=True)["baseline_ms"], 1000)

    def test_seeded_rhythm_reproducible_and_varies_across_games(self):
        plans = [engine_pacing.plan(self.board, self.clock, seed=str(i)) for i in range(30)]
        self.assertEqual(plans[0], engine_pacing.plan(self.board, self.clock, seed="0"))
        self.assertGreater(len({p["variation_factor"] for p in plans}), 20)
        self.assertGreater(len({p["target_total_ms"] for p in plans}), 10)
        self.assertTrue(all(.4 <= p["variation_factor"] <= 1 for p in plans))
        self.assertTrue(all(p["target_total_ms"] <= p["recommended_total_ms"] for p in plans))

    def test_same_seed_different_position_gets_different_rhythm(self):
        before = engine_pacing.plan(self.board, self.clock, seed="fixed")
        self.board.push_uci("e2e4")
        after = engine_pacing.plan(self.board, {**self.clock, "running_color": "black"}, seed="fixed")
        self.assertNotEqual(before["variation_factor"], after["variation_factor"])

    def test_seeded_complex_rhythm_and_low_clock_keep_bounds(self):
        self.board = chess.Board("r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1")
        for left in (600000, 180000, 120000, 10000, 1000):
            plan = engine_pacing.plan(self.board, self.at(left), seed="fixed")
            self.assertLessEqual(plan["target_total_ms"], 45000)
            self.assertLess(plan["target_total_ms"], left)
            self.assertGreaterEqual(plan["variation_factor"], .65)
            self.assertLessEqual(plan["variation_factor"], 1.35)
            if left <= 10000:
                self.assertEqual(engine_pacing.remaining_wait_ms(plan, self.at(left)), 0)


class PacingHostTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(); self.root = Path(self.folder.name)
        self.binary = self.root / "fake.exe"; self.binary.write_bytes(b"fake")
        self.now = Now(); self.factory = FakeFactory(); self.waits = []
        self.game = HumanGame(self.binary, self.root / "games", self.factory,
                              opening_variety_enabled=False, monotonic=self.now,
                              pacing_wait=self.advance_wait)
        self.addCleanup(self.folder.cleanup); self.addCleanup(self.game.close)
        self.search_seconds = 1.0
        peer = self.factory.instances[0]; original = peer.play
        def play(board, limit, *, game=None, info=None, root_moves=None):
            self.now.advance(self.search_seconds)
            if root_moves:
                return chess.engine.PlayResult(root_moves[0], None, {"depth": 2})
            return original(board, limit, game=game, info=info)
        peer.play = play

    def advance_wait(self, seconds, cancel_event):
        self.waits.append(seconds)
        self.now.advance(seconds)

    def new(self, *, color="black", control="10+0", ms=1000, **modes):
        state = self.game.new_game(color, ms, self.game.revision, time_control=control, **modes)
        self.game._opening_seed = "pacing-tests"
        return state

    def engine(self, move=None):
        if move: self.factory.instances[-1].replies.append(move)
        return self.game.engine_move(self.game.revision)

    def test_search_and_prior_elapsed_count_toward_total(self):
        self.new(); self.now.advance(2)
        expected = engine_pacing.plan(self.game.board, self.game._clock.snapshot(), seed=self.game._opening_seed)
        state = self.engine("e2e4")
        elapsed = self.game._clock.history[1]["elapsed_ms"]
        self.assertAlmostEqual(elapsed, max(3000, expected["target_total_ms"]), delta=1)
        self.assertAlmostEqual(sum(self.waits) * 1000, max(0, expected["target_total_ms"] - 3000), delta=1)
        self.assertFalse(state["engine_thinking"])
        self.assertIsNone(state["engine"]["pacing"])
        self.assertFalse(self.factory.instances[0].closed)

    def test_slow_search_does_not_get_extra_full_recommendation(self):
        self.new(); self.search_seconds = 20
        self.engine("e2e4")
        self.assertEqual(self.waits, [])
        self.assertEqual(self.game._clock.history[1]["elapsed_ms"], 20000)

    def test_untimed_turn_never_waits(self):
        self.new(control=None)
        self.engine("e2e4")
        self.assertEqual(self.waits, [])
        self.assertEqual(self.factory.instances[0].calls[0][1].time, 1.0)

    def test_all_timed_modes_use_same_pacing_plan(self):
        targets = []
        self.search_seconds = 0
        for modes in ({}, {"boss_mode": True}, {"sparring_mode": True}):
            self.new(**modes); self.waits.clear()
            self.engine("e2e4")
            receipt = self.game._engine_pacing_decisions[1]
            targets.append(receipt["target_total_ms"])
            self.assertTrue(self.waits)
        self.assertEqual(len(set(targets)), 1)

    def test_timed_sparring_manual_setting_rejected_and_omission_allowed(self):
        state = self.new(sparring_mode=True, ms=None)
        self.assertTrue(state["engine"]["automatic_think_time"])
        self.assertEqual(state["engine"]["time_management"], "automatic")
        with self.assertRaises(ValueError):
            self.game.set_think_time(5000, self.game.game_id, state["engine"]["settings_revision"])
        budgets = []
        self.search_seconds = 0
        for setting in (100, 5000):
            self.new(sparring_mode=True, ms=setting); self.engine("e2e4")
            budgets.append(self.game._engine_movetimes[1])
        self.assertEqual(budgets[0], budgets[1])

    def test_low_clock_has_no_hold_and_short_search(self):
        self.new(sparring_mode=True); self.now.advance(599)
        self.search_seconds = 0
        state = self.engine("e2e4")
        self.assertEqual(self.waits, [])
        self.assertLess(self.game._engine_movetimes[1], 100)
        self.assertEqual(state["result"], "*")
        self.assertGreater(state["clock"]["white_ms"], 0)

    def test_polling_during_hold_does_not_block_and_flag_discards_move(self):
        self.new(); entered = threading.Event(); release = threading.Event()
        self.search_seconds = 0
        def wait(seconds, cancel_event):
            entered.set(); release.wait(3)
        self.game._pacing_wait = wait
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.engine, "e2e4")
            self.assertTrue(entered.wait(2))
            before = time.monotonic(); state = self.game.state()
            self.assertLess(time.monotonic() - before, .5)
            self.assertEqual(state["engine"]["pacing"]["status"], "waiting")
            self.assertEqual(state["moves"], [])
            self.now.advance(600); self.game.state(); release.set()
            state = future.result(2)
        self.assertEqual(state["termination"], "time_forfeit")
        self.assertEqual(state["moves"], [])

    def test_close_interrupts_hold_without_applying_selected_move(self):
        self.new(); entered = threading.Event()
        self.search_seconds = 0
        def wait(seconds, cancel_event):
            entered.set(); cancel_event.wait(3)
        self.game._pacing_wait = wait
        with ThreadPoolExecutor(max_workers=2) as pool:
            future = pool.submit(self.engine, "e2e4")
            self.assertTrue(entered.wait(2))
            close = pool.submit(self.game.close)
            close.result(3); state = future.result(3)
        self.assertEqual(state["moves"], [])
        self.assertIsNone(state["error"])
        record = json.loads((self.root / "games" / (self.game.game_id + ".json")).read_text(encoding="utf-8"))
        self.assertEqual(record["uci_moves"], [])

    def test_skip_wait_is_bound_to_current_turn(self):
        self.new()
        self.search_seconds = 0
        self.assertTrue(self.game.skip_engine_wait(self.game.revision)["released"])
        self.engine("e2e4")
        self.assertEqual(self.waits, [])
        self.game.human_move("e7e5", self.game.revision)
        self.engine("g1f3")
        self.assertTrue(self.waits)
        with self.assertRaises(RevisionError): self.game.skip_engine_wait(self.game.revision - 1)

    def test_revision_change_during_hold_drops_reply_without_error(self):
        self.new(); changed = False
        self.search_seconds = 0
        def wait(seconds, cancel_event):
            nonlocal changed
            if not changed:
                with self.game._lock:
                    self.game.revision += 1; changed = True
        self.game._pacing_wait = wait
        state = self.engine("e2e4")
        self.assertEqual(state["moves"], [])
        self.assertIsNone(state["error"])

    def test_mating_move_uses_pre_move_recommendation(self):
        self.new(color="white")
        self.game.human_move("f2f3", self.game.revision); self.engine("e7e5")
        self.game.human_move("g2g4", self.game.revision); self.waits.clear()
        before_fen = self.game.board.fen(en_passant="fen")
        state = self.engine("d8h4")
        receipt = self.game._engine_pacing_decisions[4]
        self.assertEqual(receipt["before_fen"], before_fen)
        self.assertGreater(receipt["recommended_total_ms"], 0)
        self.assertGreaterEqual(self.game._clock.history[4]["elapsed_ms"], receipt["target_total_ms"] - 1)
        self.assertEqual(state["termination"], "checkmate")


if __name__ == "__main__": unittest.main()
