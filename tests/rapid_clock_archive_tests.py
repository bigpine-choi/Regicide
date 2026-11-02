"""Saved rapid clocks, timeout evidence and frozen replay; temporary files only."""
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

import chess
import chess.pgn

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from game_archive import GameArchive
from game_clock import GameClock


class Now:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


class RapidClockArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="regicide-clock-archive-")
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.archive = GameArchive(self.folder)
        self.now = Now()
        self.clock = GameClock("10+5", monotonic=self.now)
        self.board = chess.Board()

    def move(self, uci, seconds):
        self.now.value += seconds
        color = "white" if self.board.turn else "black"
        self.assertTrue(self.clock.complete_move(color, len(self.board.move_stack) + 1))
        self.board.push_uci(uci)

    def record(self, result="*", termination=None):
        return {"game_id": "clock-test", "initial_fen": self.board.root().fen(),
                "uci_moves": [move.uci() for move in self.board.move_stack],
                "started_at": "2026-11-03T01:00:00+09:00", "clock": self.clock.metadata(),
                "state": {"human_color": "white", "result": result,
                          "termination": termination, "engine": {"name": "test", "movetime_ms": 100}}}

    def load(self, record):
        path = self.folder / "clock-test.json"
        path.write_text(json.dumps(record), encoding="utf-8")
        return self.archive.get_game("clock-test")

    def test_replay_is_frozen_and_has_no_future_clock(self):
        self.move("e2e4", 12)
        self.move("e7e5", 2)
        self.move("d2d4", 3)
        self.now.value += 4
        data = self.load(self.record())
        self.assertEqual([r["clock"]["white_ms"] for r in data["frames"]], [600000, 593000, 593000, 595000])
        self.assertEqual(data["frames"][-1]["clock"]["black_ms"], 599000)
        for frame in data["frames"]:
            self.assertEqual(frame["clock"]["status"], "replay")
            self.assertIsNone(frame["clock"]["running_color"])
            self.assertEqual(frame["decision_support"]["status"], "inactive")
        self.now.value += 100
        self.assertEqual(self.archive.get_game("clock-test"), data)

    def test_pgn_retains_increment_and_each_completed_move_balance(self):
        self.move("e2e4", 12)
        self.move("e7e5", 2)
        data = self.load(self.record())
        pgn = chess.pgn.read_game(io.StringIO(data["pgn"]))
        self.assertFalse(pgn.errors)
        self.assertEqual(pgn.headers["TimeControl"], "600+5")
        self.assertEqual([n.clock() for n in pgn.mainline()], [593, 603])
        self.assertEqual(data["summary"]["time_control"], "10+5")

    def test_timeout_requires_clock_evidence_and_correct_winner(self):
        self.now.value = 600
        self.assertEqual(self.clock.expire(), "white")
        record = self.record("0-1", "time_forfeit")
        data = self.load(record)
        self.assertEqual(data["summary"]["result"], "0-1")
        self.assertEqual(data["frames"][0]["clock"]["white_ms"], 0)
        self.assertIn('[Termination "time forfeit"]', data["pgn"])

        for mutation in ("missing", "wrong_winner", "wrong_flag", "positive_clock", "no_flag"):
            changed = copy.deepcopy(record)
            if mutation == "missing":
                changed.pop("clock")
            elif mutation == "wrong_winner":
                changed["state"]["result"] = "1-0"
            elif mutation == "wrong_flag":
                changed["clock"]["snapshot"]["flagged_color"] = "black"
            elif mutation == "positive_clock":
                changed["clock"]["snapshot"]["white_ms"] = 1
            else:
                changed["clock"]["snapshot"]["flagged_color"] = None
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.load(changed)

    def test_pgn_preserves_recorded_timing_version_without_relabelling_old_games(self):
        record = self.record()
        self.assertNotIn('EnginePacingPolicy', self.load(record)["pgn"])
        for policy in ("regicide-engine-pacing-v1", "regicide-engine-pacing-v2", "regicide-engine-pacing-v3",
                       "regicide-engine-pacing-v4", "regicide-engine-pacing-v5", "regicide-engine-pacing-v6",
                       "regicide-engine-pacing-v7", "regicide-engine-pacing-v8", "regicide-engine-pacing-v9"):
            record["engine_pacing"] = {"enabled": True, "policy": policy}
            self.assertIn(f'[EnginePacingPolicy "{policy}"]', self.load(record)["pgn"])
        for invalid in ({"enabled": False, "policy": "regicide-engine-pacing-v3"},
                        {"enabled": True, "policy": "unrecognized"}):
            record["engine_pacing"] = invalid
            self.assertNotIn('EnginePacingPolicy', self.load(record)["pgn"])

    def test_timeout_bare_king_draw_and_not_false_win(self):
        self.board = chess.Board("7k/8/8/8/8/8/1R6/K7 w - - 0 1")
        self.now.value = 601
        self.clock.expire()
        record = self.record("1/2-1/2", "time_forfeit_insufficient_material")
        self.assertEqual(self.load(record)["summary"]["result"], "1/2-1/2")
        record["state"].update(result="0-1", termination="time_forfeit")
        with self.assertRaises(ValueError):
            self.load(record)

    def test_invalid_history_cannot_invent_increment_or_rewrite_opponent(self):
        self.move("e2e4", 12)
        record = self.record()
        for key, value in (("white_ms", 598000), ("black_ms", 599999), ("elapsed_ms", -1),
                           ("elapsed_ms", float("nan")), ("mover", "black"), ("ply", True)):
            changed = copy.deepcopy(record)
            changed["clock"]["history"][1][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                self.load(changed)

    def test_inconsistent_current_snapshot_rejected(self):
        self.move("e2e4", 12)
        record = self.record()
        for key, value in (("white_ms", 592000), ("black_ms", 601000), ("turn_elapsed_ms", float("inf")),
                           ("running_color", "white"), ("control_id", "15+10")):
            changed = copy.deepcopy(record)
            changed["clock"]["snapshot"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.load(changed)

    def test_missing_middle_entry_and_wrong_control_rejected(self):
        self.move("e2e4", 12)
        self.move("e7e5", 2)
        record = self.record()
        changed = copy.deepcopy(record)
        changed["clock"]["history"].pop(1)
        with self.assertRaises(ValueError):
            self.load(changed)
        for key, value in (("control_id", "1+0"), ("increment_ms", 10000), ("initial_ms", True)):
            changed = copy.deepcopy(record)
            changed["clock"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.load(changed)

    def test_undo_truncation_and_pause_preserve_valid_records(self):
        self.move("e2e4", 12)
        self.move("e7e5", 2)
        self.board.pop()
        self.board.pop()
        self.clock.restore_ply(0, "white")
        self.now.value += 5
        self.clock.pause("handicap_preview")
        self.now.value += 20
        self.clock.resume("white")
        self.move("d2d4", 3)
        data = self.load(self.record())
        self.assertEqual(data["frames"][-1]["clock"]["white_ms"], 597000)

    def test_legacy_untimed_and_disabled_practice_still_load(self):
        self.move("e2e4", 12)
        record = self.record()
        record.pop("clock")
        data = self.load(record)
        self.assertNotIn('[TimeControl ', data["pgn"])
        self.assertFalse(data["frames"][0]["clock"]["enabled"])
        record["clock"] = {"enabled": False}
        self.assertFalse(self.load(record)["frames"][-1]["clock"]["enabled"])

    def test_finished_game_snapshot_and_resignation(self):
        self.move("e2e4", 12)
        self.move("e7e5", 2)
        self.now.value += 7
        self.clock.finish()
        data = self.load(self.record("0-1", "resignation"))
        self.assertEqual(data["frames"][-1]["clock"]["white_ms"], 586000)
        self.assertEqual(data["summary"]["termination"], "resignation")

    def test_short_engine_budget_near_flag_is_not_relabelled(self):
        self.move("e2e4", 12)
        self.move("e7e5", 2)
        record = self.record()
        record["engine_movetimes_ms"] = {"2": 50}
        data = self.load(record)
        self.assertEqual(data["frames"][1]["engine"]["movetime_ms"], 50)
        self.assertIn("Regicide think time: 50 ms", data["pgn"])

    def test_human_overtime_replays_and_increment_recovers(self):
        self.clock = GameClock("10+5", monotonic=self.now, timeout_policy="engine_only", soft_timeout_color="white")
        self.move("e2e4", 700)
        data = self.load(self.record())
        self.assertEqual(data["frames"][-1]["clock"]["white_ms"], 5000)
        self.assertEqual(data["summary"]["result"], "*")
        self.assertIn('[TimeForfeitPolicy "engine_only"]', data["pgn"])
        self.move("e7e5", 1)
        self.now.value += 15
        data = self.load(self.record())
        self.assertEqual(data["frames"][-1]["clock"]["overtime_color"], "white")
        self.clock.finish()
        self.assertEqual(self.load(self.record())["frames"][-1]["clock"]["white_ms"], 0)

    def test_engine_only_clock_rejects_forged_human_timeout_and_policy(self):
        self.clock = GameClock("10+0", monotonic=self.now, timeout_policy="engine_only", soft_timeout_color="white")
        self.now.value += 700
        record = self.record()
        self.assertEqual(self.load(record)["summary"]["result"], "*")
        for change in ("flag_human", "wrong_side", "wrong_snapshot", "overtime_lie"):
            forged = copy.deepcopy(record)
            if change == "flag_human":
                forged["clock"]["snapshot"].update(flagged_color="white", status="finished", running_color=None)
                forged["state"].update(result="0-1", termination="time_forfeit")
            elif change == "wrong_side":
                forged["clock"]["soft_timeout_color"] = "black"
            elif change == "wrong_snapshot":
                forged["clock"]["snapshot"]["timeout_policy"] = "both"
            else:
                forged["clock"]["snapshot"]["overtime_ms"] += 1
            with self.subTest(change=change), self.assertRaises(ValueError): self.load(forged)

    def test_practice_engine_flag_is_loss_despite_opponent_bare_king(self):
        self.clock = GameClock("10+0", monotonic=self.now, timeout_policy="engine_only", soft_timeout_color="black")
        self.board = chess.Board("7k/8/8/8/8/8/1R6/K7 w - - 0 1")
        self.now.value = 601
        self.clock.expire()
        record = self.record("0-1", "time_forfeit")
        record["state"]["human_color"] = "black"
        self.assertEqual(self.load(record)["summary"]["result"], "0-1")
        record["state"].update(result="1/2-1/2", termination="time_forfeit_insufficient_material")
        with self.assertRaises(ValueError): self.load(record)


if __name__ == "__main__":
    unittest.main()
