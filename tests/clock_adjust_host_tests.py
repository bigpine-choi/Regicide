"""Live editing while the host searches/waits; controlled peers, temporary games."""
from concurrent.futures import ThreadPoolExecutor
import threading
import unittest

import human_rhythm_host_tests as rhythm
from game_archive import GameArchive

host = rhythm.host


class ClockAdjustHostTests(unittest.TestCase):
    setUp = rhythm.HumanRhythmHostTests.setUp
    controlled_refine = rhythm.HumanRhythmHostTests.controlled_refine
    advance_wait = rhythm.HumanRhythmHostTests.advance_wait
    new = rhythm.HumanRhythmHostTests.new
    script = rhythm.HumanRhythmHostTests.script
    engine = rhythm.HumanRhythmHostTests.engine
    human = rhythm.HumanRhythmHostTests.human
    record = rhythm.HumanRhythmHostTests.record

    def controlled_plan(self, *args, **kwargs):
        result = rhythm.HumanRhythmHostTests.controlled_plan(self, *args, **kwargs)
        result["target_total_ms"] = getattr(self, "test_target", 10000)
        return result

    def edit(self, side, delta):
        return self.game.adjust_clock(side, delta, self.game.game_id, self.game._clock.edit_revision)

    def replay(self):
        return GameArchive(self.game.save_dir).get_game(self.game.game_id)

    def test_role_mapping_both_colors_and_preserved_position_revision(self):
        for color in ("white", "black"):
            with self.subTest(color=color):
                self.new(color, sparring=True)
                original = self.game.state()
                self.now.advance(2)
                first = self.edit("human", 60000)
                second = self.edit("engine", -10000)
                self.assertEqual(second["revision"], original["revision"])
                self.assertEqual(second["fen"], original["fen"])
                self.assertEqual((first["clock"]["edit_revision"], second["clock"]["edit_revision"]), (1, 2))
                self.assertEqual(second["clock"][color + "_ms"], 660000 - (2000 if color == "white" else 0))
                other = "black" if color == "white" else "white"
                self.assertEqual(second["clock"][other + "_ms"], 590000 - (2000 if other == "white" else 0))
                self.assertEqual(len(self.record()["clock"]["adjustments"]), 2)
                self.replay()

    def test_invalid_and_stale_requests_leave_clock_and_board_unchanged(self):
        self.new("white", sparring=True)
        self.edit("human", 10000)
        before = self.game._clock.metadata()
        revision = self.game.revision
        for args, error in ((("human", 10000, self.game.game_id, 0), host.RevisionError),
                            (("human", 10000, "other-game", 1), host.RevisionError),
                            (("human", 10000, self.game.game_id, True), host.RevisionError),
                            (("white", 10000, self.game.game_id, 1), ValueError),
                            (("human", 1000, self.game.game_id, 1), ValueError),
                            (("human", True, self.game.game_id, 1), ValueError)):
            with self.subTest(args=args), self.assertRaises(error):
                self.game.adjust_clock(*args)
            self.assertEqual(self.game._clock.metadata(), before)
            self.assertEqual(self.game.revision, revision)

    def test_untimed_and_finished_games_reject_clock_edits(self):
        for sparring, control in ((False, None), (True, None)):
            self.new("white", sparring=sparring, control=control)
            self.assertFalse(self.game.state()["clock"]["editable"])
            with self.assertRaises(ValueError):
                self.edit("human", 10000)
        self.new("white", sparring=True)
        self.game.resign(self.game.revision)
        with self.assertRaises(ValueError):
            self.edit("human", 10000)
        self.assertFalse(self.game.state()["clock"]["editable"])

    def test_all_modes_offer_both_clocks_for_either_human_color(self):
        for mode in ("basic", "gambit", "sparring"):
            for color in ("white", "black"):
                with self.subTest(mode=mode, color=color):
                    self.game.new_game(color, None, self.game.revision,
                        boss_mode=mode == "gambit", sparring_mode=mode == "sparring", time_control="10+0")
                    before = self.game.state()
                    self.assertTrue(before["clock"]["editable"])
                    self.edit("human", 60000)
                    after = self.edit("engine", -10000)
                    other = "black" if color == "white" else "white"
                    self.assertEqual(after["clock"][color + "_ms"], 660000)
                    self.assertEqual(after["clock"][other + "_ms"], 590000)
                    self.assertEqual(after["revision"], before["revision"])
                    self.assertEqual(after["fen"], before["fen"])
                    self.assertEqual(len(self.record()["clock"]["adjustments"]), 2)
                    self.replay()

    def test_human_overtime_can_be_refilled_without_spurious_result(self):
        self.new("white", sparring=True)
        self.now.advance(620)
        state = self.edit("human", 60000)
        self.assertEqual(state["result"], "*")
        self.assertEqual(state["clock"]["white_ms"], 60000)
        self.assertEqual(state["clock"]["turn_elapsed_ms"], 620000)
        self.assertEqual(state["clock"]["overtime_ms"], 0)
        self.human("e2e4")
        self.replay()

    def test_engine_zero_is_final_and_saved_as_time_forfeit(self):
        self.new("black", sparring=True)
        self.now.advance(590)
        state = self.edit("engine", -10000)
        self.assertEqual((state["result"], state["termination"]), ("0-1", "time_forfeit"))
        self.assertFalse(state["clock"]["editable"])
        with self.assertRaises(ValueError):
            self.edit("engine", 60000)
        self.assertEqual(self.replay()["summary"]["termination"], "time_forfeit")

    def test_undo_invalidates_old_clock_revision_and_preserves_earlier_edits(self):
        self.new("black", sparring=True)
        self.edit("human", 10000)
        self.script("e2e4 e7e5")
        self.engine()
        self.human("e7e5")
        self.script("g1f3 b8c6")
        self.engine()
        self.edit("engine", 10000)
        old = self.game._clock.edit_revision
        self.game.undo(self.game.revision)
        self.assertGreater(self.game._clock.edit_revision, old)
        self.assertEqual([event["ply"] for event in self.game._clock.adjustments], [0])
        with self.assertRaises(host.RevisionError):
            self.game.adjust_clock("human", 10000, self.game.game_id, old)
        self.replay()

    def test_handicap_preview_rejects_edits_and_cancel_retains_audit(self):
        self.new("black", sparring=True)
        self.edit("human", 10000)
        self.script("e2e4 e7e5")
        self.engine()
        before = self.game._clock.metadata()
        self.game.begin_handicap(1, self.game.revision)
        with self.assertRaises(ValueError):
            self.edit("engine", 10000)
        self.game.cancel_handicap(self.game.revision)
        self.assertEqual(self.game._clock.metadata()["adjustments"], before["adjustments"])
        self.replay()

    def retarget_wait(self, delta, target):
        self.new("black", sparring=True)
        self.script("e2e4 e7e5")
        edits = []
        def wait(seconds, cancel_event):
            if not edits:
                self.test_target = target
                edits.append(self.edit("engine", delta))
            self.advance_wait(seconds, cancel_event)
        self.game._pacing_wait = wait
        revision = self.game.revision
        state = self.engine()
        self.assertEqual(edits[0]["revision"], revision)
        self.assertEqual(state["last_move"], "e2e4")
        self.assertAlmostEqual(self.game._clock.history[1]["elapsed_ms"], target, delta=1)
        self.assertEqual(self.game._engine_pacing_decisions[1]["target_total_ms"], target)
        self.assertEqual(len(self.peer.searches), 1)
        self.replay()

    def test_subtracting_during_wait_shortens_this_turn(self):
        self.retarget_wait(-60000, 2000)

    def test_adding_during_wait_extends_this_turn(self):
        self.retarget_wait(60000, 20000)

    def test_search_edit_keeps_position_and_stops_at_new_target(self):
        self.new("black", sparring=True)
        entered, released, stopped = threading.Event(), threading.Event(), threading.Event()
        self.script("e2e4 e7e5", cost_ms=0)
        self.peer.scripts[0]["gate"] = released
        analysis = self.peer.analysis
        def stream(*args, **kwargs):
            result = analysis(*args, **kwargs)
            def stop():
                stopped.set()
                released.set()
            result.stop = stop
            entered.set()
            return result
        self.peer.analysis = stream
        revision = self.game.revision
        with ThreadPoolExecutor(max_workers=1) as pool:
            moving = pool.submit(self.engine)
            try:
                self.assertTrue(entered.wait(2))
                self.now.advance(2)
                self.test_target = 2050
                state = self.edit("engine", -60000)
                self.assertEqual(state["revision"], revision)
                self.assertTrue(state["engine_thinking"])
                self.assertTrue(stopped.wait(2))
            finally:
                released.set()
            final = moving.result(timeout=4)
        self.assertEqual(final["last_move"], "e2e4")
        self.assertEqual(len(self.peer.searches), 1)
        self.replay()

    def test_fresh_watchdog_honors_added_time_during_search(self):
        self.new("black", sparring=True)
        self.now.advance(599.8)
        released = threading.Event()
        self.script("e2e4 e7e5", cost_ms=0)
        self.peer.scripts[0]["gate"] = released
        with ThreadPoolExecutor(max_workers=1) as pool:
            moving = pool.submit(self.engine)
            try:
                self.assertTrue(self.peer.waiting.wait(2))
                self.edit("engine", 60000)
                self.now.advance(.25)
                # Outlive both the original virtual expiry and a stale
                # fixed 200 ms real Timer; only the fresh clock may decide.
                self.assertFalse(released.wait(.3))
                self.assertFalse(self.peer.closed)
                self.assertIsNone(self.game._clock.flagged_color)
            finally:
                released.set()
            final = moving.result(timeout=4)
        self.assertEqual(final["result"], "*")
        self.assertEqual(final["last_move"], "e2e4")
        self.replay()

    def test_simultaneous_buttons_apply_once_per_clock_revision(self):
        self.new("white", sparring=True)
        revision = self.game._clock.edit_revision
        gate = threading.Barrier(2)
        def submit():
            gate.wait(timeout=2)
            try:
                self.game.adjust_clock("human", 10000, self.game.game_id, revision)
                return "accepted"
            except host.RevisionError:
                return "stale"
        with ThreadPoolExecutor(max_workers=2) as pool:
            attempts = [pool.submit(submit) for _ in range(2)]
            results = [attempt.result(timeout=3) for attempt in attempts]
        self.assertCountEqual(results, ["accepted", "stale"])
        self.assertEqual(len(self.game._clock.adjustments), 1)
        self.assertEqual(self.game._clock.snapshot()["white_ms"], 610000)

    def test_zero_edit_during_search_never_applies_late_move(self):
        self.new("black", sparring=True)
        self.now.advance(590)
        released = threading.Event()
        self.script("e2e4 e7e5", cost_ms=0)
        self.peer.scripts[0]["gate"] = released
        with ThreadPoolExecutor(max_workers=1) as pool:
            moving = pool.submit(self.engine)
            try:
                self.assertTrue(self.peer.waiting.wait(2))
                state = self.edit("engine", -10000)
                self.assertEqual(state["termination"], "time_forfeit")
                self.assertTrue(released.wait(2))
            finally:
                released.set()
            final = moving.result(timeout=4)
        self.assertEqual(final["result"], "0-1")
        self.assertEqual(final["moves"], [])
        self.assertEqual(self.game._engine_pacing_decisions, {})
        self.replay()


if __name__ == "__main__":
    unittest.main()
