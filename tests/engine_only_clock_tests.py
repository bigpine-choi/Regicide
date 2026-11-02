"""Asymmetric practice clocks: human overtime is legal, engine flag is final."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from game_clock import GameClock
from game_clock_tests import Now


class PracticeClockTests(unittest.TestCase):
    def setUp(self): self.now = Now()

    def clock(self, color="white", control="10+0"):
        return GameClock(control, monotonic=self.now, timeout_policy="engine_only", soft_timeout_color=color)

    def test_white_overtime_remains_running_and_accepts_moves(self):
        clock = self.clock(); self.now.advance(610)
        self.assertIsNone(clock.expire())
        state = clock.snapshot()
        self.assertEqual((state["white_ms"], state["status"], state["running_color"]), (0, "running", "white"))
        self.assertEqual((state["overtime_color"], state["overtime_ms"]), ("white", 10000))
        self.assertIsNone(state["flagged_color"])
        self.assertTrue(clock.complete_move("white", 1))
        self.assertEqual(clock.history[-1]["elapsed_ms"], 610000)
        self.assertEqual(clock.snapshot()["overtime_color"], "white")
        self.assertEqual(clock.snapshot()["overtime_ms"], 0)
        self.now.advance(1); clock.complete_move("black", 2)
        self.now.advance(12); self.assertTrue(clock.complete_move("white", 3))
        self.assertEqual(clock.history[-1]["white_ms"], 0)

    def test_increment_recovers_after_human_zero_for_both_colors(self):
        for color in ("white", "black"):
            for control, initial, increment in (("10+5", 600, 5000), ("15+10", 900, 10000)):
                with self.subTest(color=color, control=control):
                    clock = self.clock(color, control)
                    if color == "black": clock.complete_move("white", 1)
                    self.now.advance(initial + 25)
                    self.assertIsNone(clock.expire())
                    self.assertTrue(clock.complete_move(color, clock.ply + 1))
                    self.assertEqual(clock.snapshot()[color + "_ms"], increment)
                    self.assertIsNone(clock.snapshot()["overtime_color"])
                    self.assertEqual(clock.history[-1]["elapsed_ms"], (initial + 25) * 1000)

    def test_engine_exact_zero_never_grants_increment(self):
        for human in ("white", "black"):
            clock = self.clock(human, "10+5")
            if human == "white": clock.complete_move("white", 1)
            engine = "black" if human == "white" else "white"
            self.now.advance(600)
            self.assertFalse(clock.complete_move(engine, clock.ply + 1))
            self.assertEqual(clock.flagged_color, engine)
            self.assertEqual(clock.snapshot()[engine + "_ms"], 0)
            self.assertEqual(clock.snapshot()["status"], "finished")

    def test_pause_overtime_excludes_editor_time_and_restores_elapsed(self):
        clock = self.clock(control="10+5"); self.now.advance(605); clock.pause("preview")
        self.now.advance(1000)
        self.assertEqual(clock.snapshot()["turn_elapsed_ms"], 605000)
        self.assertEqual(clock.snapshot()["overtime_ms"], 5000)
        clock.resume("white"); self.now.advance(2); clock.complete_move("white", 1)
        self.assertEqual(clock.history[-1]["elapsed_ms"], 607000)
        self.assertEqual(clock.history[-1]["white_ms"], 5000)

    def test_finish_freezes_overtime_without_manufacturing_human_flag(self):
        clock = self.clock(); self.now.advance(610); clock.finish(); self.now.advance(200)
        state = clock.snapshot()
        self.assertEqual(state["status"], "finished")
        self.assertEqual(state["turn_elapsed_ms"], 610000)
        self.assertEqual(state["overtime_ms"], 10000)
        self.assertIsNone(state["flagged_color"])

    def test_undo_restores_policy_and_original_balance(self):
        clock = self.clock(); self.now.advance(610); clock.complete_move("white", 1)
        clock.complete_move("black", 2); clock.restore_ply(0, "white")
        state = clock.snapshot()
        self.assertEqual(state["white_ms"], 600000)
        self.assertEqual(state["timeout_policy"], "engine_only")
        self.assertEqual(state["soft_timeout_color"], "white")
        self.assertIsNone(state["overtime_color"])
        self.assertEqual(state["turn_elapsed_ms"], 0)

    def test_default_strict_policy_and_explicit_metadata(self):
        strict = GameClock("10+0", monotonic=self.now)
        soft = self.clock()
        self.now.advance(600)
        self.assertEqual(strict.expire(), "white")
        self.assertIsNone(soft.expire())
        data = soft.metadata()
        self.assertEqual(data["timeout_policy"], data["snapshot"]["timeout_policy"])
        self.assertEqual(data["soft_timeout_color"], "white")
        for kwargs in ({"timeout_policy": "unknown"}, {"timeout_policy": "engine_only"},
                       {"soft_timeout_color": "white"}):
            with self.assertRaises(ValueError): GameClock("10+0", **kwargs)


if __name__ == "__main__": unittest.main()
