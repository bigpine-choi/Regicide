"""Clock preservation when replacing engine moves; fake time, temporary games."""
import copy
import io
import unittest

import chess.pgn

import rapid_clock_host_tests as clocks
from game_archive import GameArchive
from game_clock import CONTROLS
from play_human import RevisionError


class HandicapClockTests(unittest.TestCase):
    setUp = clocks.HostClockTests.setUp
    new = clocks.HostClockTests.new
    human = clocks.HostClockTests.human
    engine = clocks.HostClockTests.engine
    record = clocks.HostClockTests.record

    def edit(self, side, delta):
        return self.game.adjust_clock(side, delta, self.game.game_id, self.game._clock.edit_revision)

    def replay(self):
        return GameArchive(self.game.save_dir).get_game(self.game.game_id)

    def first_engine_move(self, color="white", control="10+5"):
        self.new(color, control, sparring_mode=True)
        if color == "white":
            self.now.advance(2)
            self.human("e2e4")
            self.now.advance(3)
            self.engine("e7e5")
            return 2, "c7c5"
        self.now.advance(3)
        self.engine("e2e4")
        return 1, "d2d4"

    def test_both_colors_all_controls_keep_completed_time_and_increment_once(self):
        for color in ("white", "black"):
            for control, config in CONTROLS.items():
                with self.subTest(color=color, control=control):
                    ply, replacement = self.first_engine_move(color, control)
                    history = copy.deepcopy(self.game._clock.history)
                    self.now.advance(7)
                    self.game.begin_handicap(ply, self.game.revision)
                    preview_revision = self.game._clock.edit_revision
                    self.now.advance(1000)
                    state = self.game.forced_move(replacement, self.game.revision)
                    snapshot = state["clock"]
                    self.assertTrue(snapshot["enabled"])
                    self.assertEqual(snapshot["control_id"], control)
                    self.assertEqual(snapshot["increment_ms"], config["increment_ms"])
                    self.assertEqual(snapshot["timeout_policy"], "engine_only")
                    self.assertEqual(snapshot["soft_timeout_color"], color)
                    self.assertEqual(snapshot["running_color"], color)
                    self.assertEqual(snapshot["status"], "running")
                    self.assertEqual(snapshot["turn_elapsed_ms"], 0)
                    self.assertTrue(state["engine"]["automatic_think_time"])
                    self.assertEqual(state["engine"]["time_management"], "automatic")
                    self.assertGreater(snapshot["edit_revision"], preview_revision)
                    self.assertEqual(self.game._clock.history, history)
                    for side in ("white", "black"):
                        self.assertEqual(snapshot[side + "_ms"], history[ply][side + "_ms"])
                    self.assertTrue(state["human_to_move"])
                    pgn = chess.pgn.read_game(io.StringIO(self.game.pgn()))
                    self.assertEqual(pgn.headers["EngineTimeManagement"], "automatic")
                    self.assertEqual(pgn.headers["TimeControl"],
                                     f'{config["initial_ms"] // 1000}+{config["increment_ms"] // 1000}')
                    mover = history[ply]["mover"]
                    self.assertEqual(pgn.end().clock(), history[ply][mover + "_ms"] / 1000)
                    self.assertIn("User-forced", pgn.end().comment)
                    replay = self.replay()
                    self.assertEqual(replay["frames"][-1]["clock"]["control_id"], control)
                    self.assertEqual(replay["frames"][-1]["fen"], state["fen"])
                    self.now.advance(1.25)
                    self.assertAlmostEqual(self.game.state()["clock"][color + "_ms"],
                                           history[ply][color + "_ms"] - 1250, delta=.001)

    def test_historical_replacement_keeps_same_turn_edits_and_drops_future_edits(self):
        self.new("white", "10+5", sparring_mode=True)
        self.edit("engine", 60000)  # Before the retained human move.
        self.now.advance(3)
        self.human("e2e4")
        self.edit("engine", -10000)  # During the original replaced engine turn.
        self.engine("e7e5")
        retained_history = copy.deepcopy(self.game._clock.history)
        retained_edits = copy.deepcopy(self.game._clock.adjustments)
        self.edit("human", 10000)
        self.now.advance(4)
        self.human("g1f3")
        self.edit("engine", 10000)
        self.engine("b8c6")
        self.edit("human", -10000)
        original = self.game._clock.metadata()
        self.game.begin_handicap(2, self.game.revision)
        preview_revision = self.game._clock.edit_revision
        self.now.advance(500)
        state = self.game.forced_move("c7c5", self.game.revision)
        self.assertEqual([move.uci() for move in self.game.board.move_stack], ["e2e4", "c7c5"])
        self.assertEqual(self.game._clock.history, retained_history)
        self.assertEqual(self.game._clock.adjustments, retained_edits)
        self.assertGreater(state["clock"]["edit_revision"], preview_revision)
        with self.assertRaises(RevisionError):
            self.game.adjust_clock("engine", 10000, self.game.game_id, original["edit_revision"])
        record = self.record()
        self.assertEqual(record["clock"]["history"], retained_history)
        self.assertEqual(record["clock"]["adjustments"], retained_edits)
        self.replay()
        state = self.edit("engine", 10000)
        self.assertEqual(state["clock"]["black_ms"], retained_history[2]["black_ms"] + 10000)
        self.replay()

    def test_cancel_keeps_current_elapsed_edits_and_original_branch(self):
        for color in ("white", "black"):
            with self.subTest(color=color):
                ply, _replacement = self.first_engine_move(color)
                self.edit("human", 60000)
                self.now.advance(5.25)
                original = self.game._clock.metadata()
                moves = [move.uci() for move in self.game.board.move_stack]
                self.game.begin_handicap(ply, self.game.revision)
                self.now.advance(1000)
                state = self.game.cancel_handicap(self.game.revision)
                self.assertEqual(self.game._clock.history, original["history"])
                self.assertEqual(self.game._clock.adjustments, original["adjustments"])
                self.assertEqual([move.uci() for move in self.game.board.move_stack], moves)
                for key in ("white_ms", "black_ms", "turn_elapsed_ms", "control_id", "increment_ms"):
                    self.assertEqual(state["clock"][key], original["snapshot"][key])
                self.replay()

    def test_undo_after_continuation_keeps_replacement_clock_and_no_extra_increment(self):
        for color in ("white", "black"):
            with self.subTest(color=color):
                ply, replacement = self.first_engine_move(color)
                retained = copy.deepcopy(self.game._clock.history)
                self.game.begin_handicap(ply, self.game.revision)
                self.game.forced_move(replacement, self.game.revision)
                self.now.advance(2)
                self.human("g1f3" if color == "white" else "d7d5")
                self.engine("d7d6" if color == "white" else "g1f3")
                state = self.game.undo(self.game.revision)
                self.assertEqual(len(self.game.board.move_stack), ply)
                self.assertEqual(self.game._forced_plies, {ply})
                self.assertEqual(self.game._clock.history, retained)
                self.assertEqual(state["clock"]["running_color"], color)
                for side in ("white", "black"):
                    self.assertEqual(state["clock"][side + "_ms"], retained[ply][side + "_ms"])
                self.replay()

    def test_human_zero_still_continues_and_engine_timeout_still_applies(self):
        self.new("white", "10+0", sparring_mode=True)
        self.now.advance(601)
        self.human("e2e4")
        self.engine("e7e5")
        self.game.begin_handicap(2, self.game.revision)
        self.now.advance(1000)
        state = self.game.forced_move("c7c5", self.game.revision)
        self.assertTrue(state["clock"]["enabled"])
        self.assertEqual(state["clock"]["white_ms"], 0)
        self.assertEqual(state["result"], "*")
        self.assertTrue(state["human_to_move"])
        self.now.advance(5)
        self.human("g1f3")
        self.replay()
        remaining = self.game._clock.snapshot()["black_ms"]
        self.now.advance(remaining / 1000 + .001)
        state = self.game.state()
        self.assertEqual((state["result"], state["termination"]), ("1-0", "time_forfeit"))
        self.assertEqual(self.replay()["summary"]["termination"], "time_forfeit")

    def test_untimed_game_stays_untimed(self):
        ply, replacement = self.first_engine_move("black", None)
        self.game.begin_handicap(ply, self.game.revision)
        state = self.game.forced_move(replacement, self.game.revision)
        self.assertFalse(state["clock"]["enabled"])
        self.assertIsNone(state["clock"]["control_id"])
        self.assertNotIn("TimeControl", chess.pgn.read_game(io.StringIO(self.game.pgn())).headers)
        self.replay()


if __name__ == "__main__":
    unittest.main()
