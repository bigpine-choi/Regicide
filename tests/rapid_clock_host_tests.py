"""Production HumanGame clock boundary, storage and race tests with fake time."""
from pathlib import Path
import io
import json
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

import chess
import chess.engine
import chess.pgn

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from play_human import HumanGame, RevisionError
from game_archive import GameArchive
from human_play_tests import FakeFactory
from game_clock_tests import Now
import decision_support
import engine_pacing


class HostClockTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(); self.root = Path(self.folder.name)
        self.binary = self.root / "fake.exe"; self.binary.write_bytes(b"fake peer")
        self.factory = FakeFactory(); self.now = Now()
        self.game = HumanGame(self.binary, self.root / "games", self.factory,
                              opening_variety_enabled=False, monotonic=self.now,
                              pacing_wait=lambda seconds, event: self.now.advance(seconds))
        self.addCleanup(self.folder.cleanup); self.addCleanup(self.game.close)

    def new(self, color="white", control="10+5", **kwargs):
        return self.game.new_game(color, 1000, self.game.revision, time_control=control, **kwargs)
    def human(self, uci): return self.game.human_move(uci, self.game.revision)
    def engine(self, uci=None):
        if uci: self.factory.instances[-1].replies.append(uci)
        return self.game.engine_move(self.game.revision)
    def expected_engine_balance(self):
        snapshot = self.game._clock.snapshot()
        recommendation = engine_pacing.plan(self.game.board, snapshot, seed=self.game._opening_seed)
        color = "white" if self.game.board.turn else "black"
        extra = max(0, recommendation["target_total_ms"] - snapshot["turn_elapsed_ms"])
        return snapshot[color + "_ms"] + snapshot["increment_ms"] - extra

    def record(self): return json.loads((self.root / "games" / (self.game.game_id + ".json")).read_text(encoding="utf-8"))

    def test_new_game_both_colors_white_runs_first(self):
        for color in ("white", "black"):
            state = self.new(color)
            self.assertEqual(state["clock"]["running_color"], "white")
            self.now.advance(7)
            self.assertEqual(self.game.state()["clock"]["white_ms"], 593000)
            self.assertEqual(self.game.state()["clock"]["black_ms"], 600000)

    def test_legal_move_increment_saved_and_pgn_clock(self):
        self.new(); self.now.advance(12); state = self.human("e2e4")
        self.assertEqual(state["clock"]["white_ms"], 593000)
        self.now.advance(2); expected_black = self.expected_engine_balance(); self.engine("e7e5")
        record = self.record(); self.assertEqual(len(record["clock"]["history"]), 3)
        pgn = chess.pgn.read_game(io.StringIO(self.game.pgn()))
        self.assertEqual(pgn.headers["TimeControl"], "600+5")
        self.assertEqual(pgn.variations[0].clock(), 593)
        self.assertAlmostEqual(pgn.end().clock(), expected_black / 1000, places=3)

    def test_illegal_and_stale_do_not_grant_increment(self):
        self.new(); self.now.advance(5)
        with self.assertRaises(ValueError): self.human("e2e5")
        with self.assertRaises(RevisionError): self.game.human_move("e2e4", self.game.revision - 1)
        self.assertEqual(self.game.state()["clock"]["white_ms"], 595000)
        self.assertEqual(len(self.game._clock.history), 1)

    def test_human_zero_clock_continues_without_result_or_revision_change(self):
        self.new(); revision = self.game.revision; self.now.advance(600)
        state = self.game.state()
        self.assertEqual(state["result"], "*")
        self.assertTrue(state["human_to_move"]); self.assertEqual(state["revision"], revision)
        self.assertEqual(state["clock"]["white_ms"], 0)
        self.now.advance(400); self.game.state(); self.assertEqual(self.game.revision, revision)
        state = self.human("e2e4")
        self.assertEqual(state["result"], "*")
        self.assertEqual(state["clock"]["white_ms"], 5000)
        self.assertEqual(len(self.game.board.move_stack), 1)
        GameArchive(self.root / "games").get_game(self.game.game_id)

    def test_engine_timeout_is_loss_even_against_bare_king_in_practice(self):
        self.new("black"); self.game.board = chess.Board("7k/8/8/8/8/8/8/KQ6 w - - 0 1")
        self.now.advance(600)
        state = self.game.state()
        self.assertEqual((state["result"], state["termination"]), ("0-1", "time_forfeit"))

    def test_timeout_single_knight_can_mate_with_opponent_pawn(self):
        self.new("black"); self.game.board = chess.Board("7k/8/8/8/8/8/P5n1/K7 w - - 0 1")
        self.now.advance(600)
        self.assertEqual(self.game.state()["result"], "0-1")

    def test_timeout_does_not_override_existing_checkmate(self):
        self.new()
        for uci in ("f2f3", "e7e5", "g2g4", "d8h4"): self.game.board.push_uci(uci)
        self.now.advance(601)
        state = self.game.state()
        self.assertEqual(state["termination"], "checkmate")
        self.assertIsNone(state["clock"]["flagged_color"])

    def test_engine_late_reply_never_applied(self):
        self.new("black"); peer = self.factory.instances[-1]
        def late(board, limit, **kwargs):
            self.now.advance(601)
            return chess.engine.PlayResult(chess.Move.from_uci("e2e4"), None, {})
        peer.play = late
        state = self.engine()
        self.assertEqual(state["result"], "0-1")
        self.assertEqual(state["termination"], "time_forfeit")
        self.assertIsNone(state["error"])
        self.assertEqual(state["moves"], [])

    def test_engine_poll_timeout_race_preserves_result(self):
        self.new("black"); peer = self.factory.instances[-1]; entered = threading.Event(); release = threading.Event()
        def waiting(board, limit, **kwargs):
            entered.set(); release.wait(5)
            return chess.engine.PlayResult(chess.Move.from_uci("e2e4"), None, {})
        peer.play = waiting
        with ThreadPoolExecutor(max_workers=1) as pool:
            work = pool.submit(self.engine)
            self.assertTrue(entered.wait(3)); self.now.advance(601)
            self.assertEqual(self.game.state()["termination"], "time_forfeit")
            release.set(); state = work.result(4)
        self.assertEqual(state["result"], "0-1"); self.assertIsNone(state["error"])
        self.assertFalse(state["engine_thinking"]); self.assertEqual(state["moves"], [])

    def test_low_engine_clock_caps_search_and_skips_sparring_aux(self):
        self.new("black", sparring_mode=True); self.now.advance(599.8)
        state = self.engine("e2e4")
        limit = self.factory.instances[0].calls[-1][1]
        self.assertLessEqual(limit.time, .191)
        self.assertEqual(state["result"], "*")
        self.assertEqual(len(self.factory.instances[0].calls),1)
        receipt=self.game._sparring_decisions[1]
        self.assertEqual(receipt["reason"],"short_budget")
        self.assertEqual(receipt["quality_guard"]["status"],"skipped")
        self.assertFalse(receipt["quality_guard"]["repair_attempted"])
        self.assertFalse(receipt["quality_guard"]["applied"])

    def test_undo_restores_clock_and_clears_timeout(self):
        self.new(); self.now.advance(10); self.human("e2e4")
        self.now.advance(3); self.engine("e7e5")
        self.now.advance(600); self.game.state()
        state = self.game.undo(self.game.revision)
        self.assertEqual(state["result"], "*")
        self.assertEqual(state["clock"]["white_ms"], 600000)
        self.assertEqual(state["clock"]["black_ms"], 600000)
        self.assertIsNone(state["clock"]["flagged_color"])

    def test_handicap_cancel_resumes_original_without_editing_charge(self):
        self.new(); self.now.advance(10); self.human("e2e4"); self.engine("e7e5")
        self.now.advance(5); before = self.game.state()["clock"]
        state = self.game.begin_handicap(2, self.game.revision)
        self.assertEqual(state["clock"]["status"], "paused")
        self.now.advance(1000); state = self.game.cancel_handicap(self.game.revision)
        self.assertEqual(state["clock"]["white_ms"], before["white_ms"])
        self.assertEqual(state["clock"]["turn_elapsed_ms"], before["turn_elapsed_ms"])

    def test_forced_branch_preserves_original_completed_move_clock(self):
        self.new(); self.human("e2e4"); self.engine("e7e5")
        completed = dict(self.game._clock.history[2])
        self.game.begin_handicap(2, self.game.revision)
        self.now.advance(100)
        state = self.game.forced_move("c7c5", self.game.revision)
        self.assertTrue(state["clock"]["enabled"])
        self.assertEqual(state["clock"]["control_id"], "10+5")
        self.assertEqual(state["clock"]["running_color"], "white")
        self.assertIsNone(state["clock"]["pause_reason"])
        self.assertEqual(self.game._clock.history[2], completed)
        self.assertEqual(chess.pgn.read_game(io.StringIO(self.game.pgn())).headers["TimeControl"], "600+5")
        GameArchive(self.root / "games").get_game(self.game.game_id)

    def test_resignation_freezes_clock_and_new_game_resets(self):
        self.new(); self.now.advance(8); state = self.game.resign(self.game.revision)
        self.assertEqual(state["clock"]["white_ms"], 592000)
        self.now.advance(100); self.assertEqual(self.game.state()["clock"]["white_ms"], 592000)
        state = self.new(control="15+10")
        self.assertEqual(state["clock"]["white_ms"], 900000)

    def test_close_adjudicates_unpolled_timeout(self):
        self.new("black"); self.now.advance(601); self.game.close()
        record = self.record()
        self.assertEqual(record["state"]["termination"], "time_forfeit")
        self.assertEqual(record["record_status"], "completed")
        self.assertEqual(record["clock"]["snapshot"]["flagged_color"], "white")

    def test_pgn_export_adjudicates_unpolled_timeout(self):
        self.new("black"); self.now.advance(601)
        pgn = chess.pgn.read_game(io.StringIO(self.game.pgn()))
        self.assertEqual(pgn.headers["Result"], "0-1")
        self.assertEqual(pgn.headers["Termination"], "time forfeit")

    def test_engine_error_pauses_without_fake_time_forfeit(self):
        self.new("black"); self.factory.instances[-1].replies.append(RuntimeError("test error"))
        state = self.engine()
        self.assertEqual(state["result"], "*")
        self.assertEqual(state["clock"]["status"], "paused")
        self.assertEqual(state["clock"]["pause_reason"], "engine_error")
        self.now.advance(1000)
        self.assertEqual(self.game.state()["result"], "*")

    def test_invalid_time_control_preserves_current_game(self):
        self.new(); before = self.game.game_id
        for control in ("1+0", True, {}, 600):
            with self.assertRaises(ValueError): self.new(control=control)
            self.assertEqual(self.game.game_id, before)
            self.assertEqual(self.game._clock.control_id, "10+5")

    def test_production_records_replay_with_clocks_and_terminal_result(self):
        self.new(); self.now.advance(12); self.human("e2e4")
        self.now.advance(2); expected_black = self.expected_engine_balance(); self.engine("e7e5")
        archive = GameArchive(self.root / "games")
        replay = archive.get_game(self.game.game_id, self.game.game_id)
        self.assertEqual(replay["frames"][-1]["clock"]["white_ms"], 593000)
        self.assertAlmostEqual(replay["frames"][-1]["clock"]["black_ms"], expected_black, delta=1)
        self.now.advance(600); self.game.state()
        self.human("g1f3")
        replay = archive.get_game(self.game.game_id)
        self.assertEqual(replay["frames"][-1]["result"], "*")
        self.assertIsNone(replay["frames"][-1]["clock"]["flagged_color"])
        self.assertEqual(replay["frames"][-1]["clock"]["white_ms"], 5000)
        self.assertEqual(replay["frames"][1]["clock"]["white_ms"], 593000)


if __name__ == "__main__": unittest.main()
