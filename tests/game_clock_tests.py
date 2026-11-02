"""Deterministic clock accounting; no real sleeps or engine games."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from game_clock import GameClock, validate_control


class Now:
    def __init__(self): self.value = 100.0
    def __call__(self): return self.value
    def advance(self, seconds): self.value += seconds


class ClockTests(unittest.TestCase):
    def setUp(self): self.now = Now()
    def clock(self, mode="10+5"): return GameClock(mode, monotonic=self.now)

    def test_exact_controls_and_validation(self):
        for mode, initial, increment in [("10+0", 600000, 0), ("10+5", 600000, 5000), ("15+10", 900000, 10000)]:
            snapshot = self.clock(mode).snapshot()
            self.assertEqual((snapshot["white_ms"], snapshot["black_ms"], snapshot["increment_ms"]), (initial, initial, increment))
        for value in [False, 0, "600", {}, [], "5+0"]:
            with self.assertRaises(ValueError): validate_control(value)

    def test_polling_never_restarts_current_turn(self):
        clock = self.clock(); self.now.advance(1.25)
        self.assertEqual(clock.snapshot()["white_ms"], 598750)
        self.now.advance(2.75)
        self.assertEqual(clock.snapshot()["white_ms"], 596000)
        self.assertEqual(clock.snapshot()["turn_elapsed_ms"], 4000)
        self.assertEqual(clock.snapshot()["black_ms"], 600000)

    def test_increment_only_once_on_completed_move(self):
        clock = self.clock(); self.now.advance(12)
        self.assertTrue(clock.complete_move("white", 1))
        self.assertEqual(clock.snapshot()["white_ms"], 593000)
        self.assertEqual(clock.snapshot()["running_color"], "black")
        with self.assertRaises(ValueError): clock.complete_move("white", 1)
        self.now.advance(8)
        self.assertTrue(clock.complete_move("black", 2))
        self.assertEqual(clock.snapshot()["black_ms"], 597000)
        self.assertEqual(clock.history[-1]["elapsed_ms"], 8000)

    def test_exact_zero_is_flag_no_increment(self):
        clock = self.clock(); self.now.advance(600)
        self.assertFalse(clock.complete_move("white", 1))
        self.assertEqual(clock.snapshot()["flagged_color"], "white")
        self.assertEqual(clock.snapshot()["white_ms"], 0)
        self.assertEqual(len(clock.history), 1)
        self.assertIsNone(clock.expire())

    def test_delayed_poll_clamps_zero_and_stops(self):
        clock = self.clock(); self.now.advance(1500)
        self.assertEqual(clock.expire(), "white")
        self.now.advance(500)
        self.assertEqual(clock.snapshot()["white_ms"], 0)
        self.assertEqual(clock.snapshot()["black_ms"], 600000)
        self.assertIsNone(clock.snapshot()["running_color"])

    def test_undo_restores_increment_and_spent_time(self):
        clock = self.clock(); self.now.advance(10); clock.complete_move("white", 1)
        self.now.advance(7); clock.complete_move("black", 2)
        self.now.advance(9); clock.restore_ply(0, "white")
        self.assertEqual(clock.snapshot()["white_ms"], 600000)
        self.assertEqual(clock.snapshot()["black_ms"], 600000)
        self.assertEqual(clock.snapshot()["turn_elapsed_ms"], 0)
        self.assertEqual(len(clock.history), 1)

    def test_pause_resume_excludes_editor_time(self):
        clock = self.clock(); self.now.advance(10); clock.pause("handicap_preview")
        self.now.advance(1000)
        self.assertEqual(clock.snapshot()["white_ms"], 590000)
        clock.resume("white"); self.now.advance(3)
        self.assertEqual(clock.snapshot()["turn_elapsed_ms"], 13000)
        clock.complete_move("white", 1)
        self.assertEqual(clock.history[-1]["elapsed_ms"], 13000)

    def test_pause_crossing_zero_uses_one_instant(self):
        # Old pause read once to check expiry, then again to debit. These
        # instants straddle flag fall and must never create paused zero.
        instants = iter((0.0, 599.999, 600.001, 600.001))
        clock = GameClock("10+0", monotonic=lambda: next(instants))
        clock.pause("handicap_preview")
        snapshot = clock.snapshot()
        self.assertGreater(snapshot["white_ms"], 0)
        self.assertEqual(snapshot["status"], "paused")
        self.assertIsNone(snapshot["flagged_color"])
        expired = GameClock("10+0", monotonic=self.now)
        self.now.advance(600.001); expired.pause("handicap_preview")
        self.assertEqual(expired.snapshot()["flagged_color"], "white")
        self.assertEqual(expired.snapshot()["status"], "finished")

    def test_finish_freezes_and_cannot_resume(self):
        clock = self.clock(); self.now.advance(2); clock.finish(); self.now.advance(5)
        clock.resume("white")
        self.assertEqual(clock.snapshot()["white_ms"], 598000)
        self.assertEqual(clock.snapshot()["status"], "finished")

    def test_untimed_legacy_snapshots_stable(self):
        clock = self.clock(None); before = clock.snapshot(); self.now.advance(2000)
        self.assertEqual(clock.snapshot(), before)
        self.assertTrue(clock.complete_move("white", 1))
        self.assertIsNone(clock.expire())

    def test_metadata_is_detached(self):
        clock = self.clock(); data = clock.metadata(); data["history"][0]["white_ms"] = 1
        self.assertEqual(clock.history[0]["white_ms"], 600000)


if __name__ == "__main__": unittest.main()
