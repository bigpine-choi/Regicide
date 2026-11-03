"""Shared automatic timing and selection-analysis reserves through the host.

Controlled peers and an injected monotonic clock are used here. These are not
native latency measurements; the separate HTTP/native diagnostic covers that.
"""
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

import chess

import human_play_tests as base
from opening_variety_tests import Peer
from boss_play_tests import packet

host = base.play_human
MODES = ({}, {"boss_mode": True}, {"sparring_mode": True})


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class CommonHumanPacingHostTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="regicide-common-pacing-")
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        self.binary = root / "controlled-peer.exe"
        self.binary.write_bytes(b"controlled non-playing test peer")
        self.now, self.waits, self.peer = Clock(), [], Peer()
        self.peer.clock = self.now
        self.game = host.HumanGame(self.binary, root / "games", lambda _: self.peer,
            hint_engine_factory=base.FakeFactory(), evaluation_engine_factory=base.FakeFactory(),
            opening_variety_enabled=False, dynamic_play_enabled=False,
            monotonic=self.now, pacing_wait=self.wait)
        self.addCleanup(self.game.close)

    def wait(self, seconds, cancel):
        self.waits.append(seconds)
        self.now.advance(seconds)

    def new(self, modes=None, *, control="10+0", ms=None, color="black"):
        state = self.game.new_game(color, ms, self.game.revision,
                                   time_control=control, **(modes or {}))
        self.game._opening_seed = "common-human-pacing-host"
        self.waits.clear()
        self.peer.searches.clear()
        return state

    def test_every_timed_mode_is_automatic_and_rejects_fixed_turn_settings(self):
        for modes in MODES:
            with self.subTest(modes=modes):
                state = self.new(modes)
                self.assertTrue(state["engine"]["automatic_think_time"])
                self.assertEqual(state["engine"]["time_management"], "automatic")
                self.assertEqual(state["engine"]["movetime_ms"], 1000)
                before = (self.game.board.fen(), self.game.revision, self.game._settings_revision)
                with self.assertRaises(ValueError):
                    self.game.set_think_time(3000, self.game.game_id, self.game._settings_revision)
                self.assertEqual((self.game.board.fen(), self.game.revision, self.game._settings_revision), before)

    def test_application_can_start_timed_while_constructor_default_remains_legacy(self):
        self.assertFalse(self.game.state()["engine"]["automatic_think_time"])
        self.assertFalse(self.game.state()["clock"]["enabled"])
        application = host.HumanGame(self.binary, self.game.save_dir.parent / "application",
            engine_factory=base.FakeFactory(), hint_engine_factory=base.FakeFactory(),
            evaluation_engine_factory=base.FakeFactory(), initial_time_control="10+0")
        self.addCleanup(application.close)
        state = application.state()
        self.assertTrue(state["clock"]["enabled"])
        self.assertTrue(state["engine"]["automatic_think_time"])
        self.assertEqual(state["engine"]["time_management"], "automatic")

    def test_same_clock_position_seed_has_same_automatic_allocation_in_all_modes(self):
        allocations = []
        targets = []
        for modes in MODES:
            with self.subTest(modes=modes):
                self.new(modes)
                board = chess.Board("6k1/5ppp/8/8/3q4/8/3R4/5K2 w - - 0 30")
                self.game.board = board
                self.game._clock.adjust("white", -360000)
                scored = packet(board, "d2d4", 30, 6)
                scored["pv"].append(chess.Move.from_uci("g8f8"))
                self.peer.scripts = [{"move": "d2d4", "packets": [scored], "cost_ms": 5}]
                result = self.game.engine_move(self.game.revision)
                self.assertIsNone(result["error"])
                receipt = self.game._engine_pacing_decisions[1]
                self.assertTrue(receipt["allocation"]["automatic"])
                self.assertEqual(receipt["pressure_start_ms"], 300000)
                self.assertAlmostEqual(receipt["pressure_factor"], .9, places=3)
                allocations.append(copy.deepcopy(receipt["allocation"]))
                targets.append(receipt["target_total_ms"])
                self.assertEqual(len(self.peer.searches), 1)
        self.assertEqual(allocations[0], allocations[1])
        self.assertEqual(allocations[1], allocations[2])
        self.assertEqual(len(set(targets)), 1)

    def test_untimed_research_api_keeps_explicit_manual_budget_and_no_hold(self):
        for modes in MODES:
            with self.subTest(modes=modes):
                state = self.new(modes, control=None, ms=1000)
                self.assertFalse(state["engine"]["automatic_think_time"])
                changed = self.game.set_think_time(3000, self.game.game_id, self.game._settings_revision)
                self.assertEqual(changed["engine"]["movetime_ms"], 3000)
                board = self.game.board.copy(stack=True)
                # A shallow score keeps mode-specific speculation inactive.
                self.peer.scripts = [{"move": "e2e4", "packets": [packet(board, "e2e4", 20, 2)]}]
                result = self.game.engine_move(self.game.revision)
                self.assertIsNone(result["error"])
                self.assertEqual(self.peer.searches[0]["limit"].time, 3)
                self.assertEqual(self.waits, [])

    def test_short_human_target_does_not_disable_native_gambit_verification(self):
        self.new({"boss_mode": True}, color="white")
        self.game.human_move("d2d4", self.game.revision)
        board = self.game.board.copy(stack=True)
        self.peer.scripts = [
            {"move": "g8f6", "packets": [packet(board, "g8f6", 25)], "cost_ms": 200},
            {"move": "d2d4", "packets": [packet(chess.Board(), "d2d4", 0)], "cost_ms": 50},
            {"move": "e7e5", "packets": [packet(board, "e7e5", -150)], "cost_ms": 100},
        ]
        original = host.engine_pacing.plan

        def short_plan(*args, **kwargs):
            value = original(*args, **kwargs)
            value["target_total_ms"] = 100
            return value

        with patch.object(host.engine_pacing, "plan", side_effect=short_plan):
            result = self.game.engine_move(self.game.revision)
        self.assertIsNone(result["error"])
        self.assertEqual(result["last_move"], "e7e5")
        decision = self.game._boss_decisions[2]
        pacing = self.game._engine_pacing_decisions[2]
        self.assertTrue(decision["line_complete"])
        self.assertGreaterEqual(decision["baseline_budget_ms"], host.boss_play.MIN_TOTAL_MS)
        self.assertGreater(decision["extra_budget_ms"], 0)
        self.assertTrue(pacing["allocation"]["automatic"])
        self.assertTrue(pacing["allocation"]["analysis_reserve_active"])
        self.assertGreaterEqual(self.peer.searches[0]["limit"].time, .5)
        self.assertEqual(len(self.peer.searches), 3)
        self.assertGreaterEqual(pacing["spent_before_wait_ms"], 350)
        self.assertAlmostEqual(self.game._clock.history[-1]["elapsed_ms"],
            pacing["spent_before_wait_ms"] + pacing["waited_ms"], delta=2)

    def test_http_omission_is_accepted_in_every_timed_mode_but_not_untimed(self):
        server = host.HumanServer(self.game)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()

        def request(body):
            req = urllib.request.Request(server.origin + "/api/new", data=json.dumps(body).encode(),
                headers={"X-Regicide-Token": server.token, "Origin": server.origin,
                         "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as response:
                return json.loads(response.read())

        try:
            for modes in MODES:
                result = request({"revision": self.game.revision, "color": "black",
                                  "time_control": "10+0", **modes})
                self.assertTrue(result["engine"]["automatic_think_time"])
                self.assertEqual(result["engine"]["time_management"], "automatic")
            before = (self.game.game_id, self.game.revision, self.game.board.fen())
            with self.assertRaises(urllib.error.HTTPError) as failed:
                request({"revision": self.game.revision, "color": "black", "time_control": None})
            self.assertEqual(failed.exception.code, 400)
            self.assertEqual((self.game.game_id, self.game.revision, self.game.board.fen()), before)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())


if __name__ == "__main__":
    unittest.main()
