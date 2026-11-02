"""Bounded rating-only observations through the real host, using a fake UCI peer.

The clock and search costs are controlled; no native engine or personal games
are used. These cases separate observing a human move from choosing a bot move.
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

import human_play_tests as base
from human_rhythm_host_tests import PeerClock, packet
from opening_variety_tests import Peer

host = base.play_human


class RatingObservationHostTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="regicide-rating-observation-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        binary = self.root / "controlled-peer.exe"
        binary.write_bytes(b"controlled UCI peer, never executed")
        self.now, self.peer = PeerClock(), Peer()
        self.peer.clock = self.now
        self.game = host.HumanGame(binary, self.root / "games", lambda _: self.peer,
            hint_engine_factory=base.FakeFactory(), evaluation_engine_factory=base.FakeFactory(),
            opening_variety_enabled=False, monotonic=self.now,
            pacing_wait=lambda seconds, event: self.now.advance(seconds))
        self.addCleanup(self.game.close)
        self.baseline_ms, self.total_ms = 200, 900
        self.observed = []
        original_observe = host.boss_play.observe_human_move

        def observe(board, search, close, *, budget_ms, forced):
            self.observed.append({"budget_ms": budget_ms, "forced": forced,
                "active_extra_ms": self.game._active_extra_budget_ms,
                "before_evaluation": copy.deepcopy(self.game.evaluation())})
            result = original_observe(board, search, close, budget_ms=budget_ms, forced=forced)
            self.observed[-1]["after_evaluation"] = copy.deepcopy(self.game.evaluation())
            return result

        allocation = patch.object(host.engine_pacing, "search_budget", side_effect=self.allocate)
        observation = patch.object(host.boss_play, "observe_human_move", side_effect=observe)
        allocation.start(); observation.start()
        self.addCleanup(allocation.stop); self.addCleanup(observation.stop)

    def allocate(self, pacing, clock, configured_ms, *, automatic=False):
        return {"baseline_ms": self.baseline_ms,
                "total_search_ms": self.total_ms if clock.get("enabled") else None,
                "completion_margin_ms": 25, "automatic": bool(automatic)}

    def prepare(self, *, control="10+0", color="white", sparring=True, baseline_cost=200, gate=None):
        self.game.new_game(color, 1000, self.game.revision, sparring_mode=sparring, time_control=control)
        self.game._opening_seed = "rating-observation-host"
        if color == "white":
            self.game.human_move("e2e4", self.game.revision)
            moves = "e7e5 g1f3 b8c6"
        else:
            moves = "e2e4 e7e5 g1f3"
        self.peer.scripts = [{"move": moves.split()[0], "cost_ms": baseline_cost, "gate": gate,
            "packets": [packet(self.game.board, moves, cp=35, depth=depth) for depth in (4, 5, 6)]}]

    def quality_script(self, *, move="e2e4", cp=30, cost=150, failure=None, gate=None):
        before = self.game.board.copy(stack=True)
        before.pop()
        self.peer.scripts.append({"move": move, "cost_ms": cost, "failure": failure, "gate": gate,
            "packets": [packet(before, move, cp=cp, depth=depth) for depth in (4, 5, 6)]})

    def engine(self):
        state = self.game.engine_move(self.game.revision)
        self.assertIsNone(state["error"])
        return state

    def record(self):
        return json.loads((self.game.save_dir / (self.game.game_id + ".json")).read_text(encoding="utf-8"))

    def test_short_baseline_observes_and_saves_rating_without_changing_move(self):
        self.prepare(); self.quality_script()
        state = self.engine()
        self.assertEqual(state["last_move"], "e7e5")
        self.assertEqual(len(self.peer.searches), 2)
        self.assertEqual(self.peer.searches[0]["limit"].time, .2)
        self.assertEqual(self.peer.searches[1]["board"].fen(), chess.STARTING_FEN)
        self.assertEqual(self.observed[0]["active_extra_ms"], 500)
        self.assertEqual(self.observed[0]["budget_ms"], 500)
        self.assertEqual(self.observed[0]["before_evaluation"], self.observed[0]["after_evaluation"])
        self.assertEqual(state["engine"]["sparring_rating"]["observations"], 1)
        self.assertEqual(state["engine"]["sparring_rating"]["updated_human_ply"], 1)
        row = self.game._sparring_decisions[2]
        self.assertTrue(row["rating_only"])
        self.assertEqual(row["reason"], "rating_observation_only")
        self.assertEqual(row["selected_uci"], row["baseline_uci"])
        self.assertEqual(row["candidate_budget_ms"], 0)
        self.assertEqual(row["probes"], [])
        self.assertEqual(row["baseline_budget_ms"], self.baseline_ms)
        self.assertEqual(self.record()["sparring_rating"]["curve"][0]["estimate"],
                         state["engine"]["sparring_rating"]["estimate"])
        self.assertEqual(self.game._engine_pacing_decisions[2]["allocation"]["total_search_ms"], self.total_ms)

    def test_clock_limited_full_policy_can_use_smaller_observation_only_allowance(self):
        self.baseline_ms, self.total_ms = 700, 1000
        self.prepare(baseline_cost=700); self.quality_script(cost=75)
        state = self.engine()
        self.assertEqual(state["last_move"], "e7e5")
        self.assertEqual(len(self.peer.searches), 2)
        self.assertAlmostEqual(self.observed[0]["budget_ms"], 275, delta=.01)
        self.assertEqual(self.observed[0]["active_extra_ms"], 275)
        self.assertTrue(self.game._sparring_decisions[2]["rating_only"])
        self.assertLessEqual(700 + self.observed[0]["budget_ms"] + 25, self.total_ms + .01)

    def test_actual_baseline_overrun_reduces_remaining_observation_budget(self):
        self.prepare(baseline_cost=610); self.quality_script(cost=100)
        self.engine()
        self.assertAlmostEqual(self.observed[0]["budget_ms"], 265, delta=.01)
        self.assertLessEqual(610 + self.observed[0]["budget_ms"] + 25, self.total_ms + .01)

    def test_baseline_overrun_below_minimum_skips_observation(self):
        self.prepare(baseline_cost=800)
        state = self.engine()
        self.assertEqual(state["last_move"], "e7e5")
        self.assertEqual(len(self.peer.searches), 1)
        self.assertEqual(self.observed, [])
        self.assertEqual(state["engine"]["sparring_rating"]["observations"], 0)

    def test_no_original_allocation_room_never_borrows_from_wait_time(self):
        self.total_ms = 300
        self.prepare()
        self.engine()
        self.assertEqual(len(self.peer.searches), 1)
        self.assertEqual(self.observed, [])

    def test_low_clock_skips_observation_before_and_after_baseline(self):
        for before, cost in ((591, 200), (589.8, 400)):
            with self.subTest(elapsed=before):
                self.prepare(baseline_cost=cost)
                self.now.advance(before)
                self.observed.clear(); self.peer.searches.clear()
                state = self.engine()
                self.assertEqual(state["last_move"], "e7e5")
                self.assertEqual(self.observed, [])
                self.assertEqual(len(self.peer.searches), 1)

    def test_unclocked_short_baseline_does_not_invent_an_optional_budget(self):
        self.prepare(control=None)
        self.engine()
        self.assertEqual(self.observed, [])
        self.assertEqual(len(self.peer.searches), 1)

    def test_no_human_move_or_forced_game_cannot_create_rating_evidence(self):
        self.prepare(color="black")
        self.engine()
        self.assertEqual(self.observed, [])
        self.prepare()
        self.game._forced_plies.add(1)
        self.peer.searches.clear()
        self.engine()
        self.assertEqual(self.observed, [])
        self.assertEqual(len(self.peer.searches), 1)

    def test_observation_failure_keeps_baseline_and_no_rating(self):
        self.prepare(); self.quality_script(failure=RuntimeError("controlled optional failure"))
        state = self.engine()
        self.assertEqual(state["last_move"], "e7e5")
        self.assertEqual(state["engine"]["sparring_rating"]["observations"], 0)
        self.assertTrue(self.peer.closed)
        self.assertEqual(self.game._sparring_decisions[2]["selected_uci"], "e7e5")

    def test_actual_human_comparison_cannot_publish_or_replace_the_bot_move(self):
        self.prepare()
        self.quality_script(move="d2d4", cp=100, cost=50)
        self.quality_script(move="e2e4", cp=0, cost=60)
        state = self.engine()
        self.assertEqual(state["last_move"], "e7e5")
        self.assertEqual(len(self.peer.searches), 3)
        self.assertEqual(self.peer.searches[2]["root_moves"], [chess.Move.from_uci("e2e4")])
        self.assertEqual(self.observed[0]["before_evaluation"], self.observed[0]["after_evaluation"])
        self.assertEqual(self.game._sparring_decisions[2]["opponent_observation"]["loss_cp"], 100)

    def test_close_during_baseline_stops_rating_work_and_does_not_apply_reply(self):
        gate = threading.Event()
        self.prepare(gate=gate)
        with ThreadPoolExecutor(max_workers=2) as pool:
            moving = pool.submit(self.engine)
            self.assertTrue(self.peer.waiting.wait(2))
            closing = pool.submit(self.game.close)
            deadline = time.monotonic() + 2
            while not self.game._closing and time.monotonic() < deadline:
                time.sleep(.005)
            self.assertTrue(self.game._closing)
            gate.set()
            state = moving.result(timeout=3)
            closing.result(timeout=3)
        self.assertEqual(self.observed, [])
        self.assertEqual(len(self.peer.searches), 1)
        self.assertEqual(len(state["moves"]), 1)
        self.assertEqual(self.game._sparring_decisions, {})


if __name__ == "__main__":
    unittest.main()
