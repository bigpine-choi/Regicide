"""Deterministic live clock edits and independently validated archive evidence."""
import copy
import io
import json
from pathlib import Path
import random
import sys
import tempfile
import unittest

import chess
import chess.pgn

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from game_archive import GameArchive, _recorded_clock
from game_clock import GameClock, MAX_ADJUSTED_MS


class Now:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class ClockAdjustmentTests(unittest.TestCase):
    def setUp(self):
        self.now = Now()
        self.clock = GameClock("10+5", monotonic=self.now,
                               timeout_policy="engine_only", soft_timeout_color="white")
        self.board = chess.Board()

    def move(self, uci, seconds=0):
        self.now.advance(seconds)
        color = "white" if self.board.turn else "black"
        self.assertTrue(self.clock.complete_move(color, len(self.board.move_stack) + 1))
        self.board.push_uci(uci)

    def record(self, result="*", termination=None):
        return {"game_id": "clock-edit-test", "initial_fen": self.board.root().fen(),
                "uci_moves": [move.uci() for move in self.board.move_stack],
                "started_at": "2026-11-03T01:00:00+09:00", "clock": self.clock.metadata(),
                "state": {"human_color": "white", "result": result,
                          "termination": termination, "engine": {"name": "test", "movetime_ms": 100}}}

    def validate(self, record=None):
        return _recorded_clock(record or self.record(), self.board)

    def load(self, record=None):
        with tempfile.TemporaryDirectory(prefix="regicide-clock-edits-") as folder:
            path = Path(folder) / "clock-edit-test.json"
            path.write_text(json.dumps(record or self.record()), encoding="utf-8")
            return GameArchive(folder).get_game("clock-edit-test")

    def test_active_edit_preserves_elapsed_and_does_not_grant_increment(self):
        self.now.advance(2)
        event = self.clock.adjust("white", 60000)
        self.assertEqual((event["before_ms"], event["after_ms"], event["turn_elapsed_ms"]),
                         (598000, 658000, 2000))
        self.assertEqual(self.clock.ply, 0)
        self.assertEqual(len(self.clock.history), 1)
        self.move("e2e4", 3)
        self.assertEqual(self.clock.history[-1]["white_ms"], 660000)
        self.assertEqual(self.clock.history[-1]["elapsed_ms"], 5000)
        self.assertEqual(self.clock.history[0]["white_ms"], 600000)
        self.validate()

    def test_inactive_edit_only_debits_running_color_once(self):
        self.now.advance(2)
        self.clock.adjust("black", -10000)
        snapshot = self.clock.snapshot()
        self.assertEqual((snapshot["white_ms"], snapshot["black_ms"]), (598000, 590000))
        self.assertEqual(snapshot["turn_elapsed_ms"], 2000)
        self.move("e2e4", 3)
        self.assertEqual((self.clock.history[-1]["white_ms"], self.clock.history[-1]["black_ms"]),
                         (600000, 590000))
        self.validate()

    def test_multiple_edits_pause_and_resume_keep_full_turn_time(self):
        self.now.advance(3)
        self.clock.adjust("white", 10000)
        self.now.advance(2)
        self.clock.adjust("black", -60000)
        self.now.advance(1)
        self.clock.pause("preview")
        self.now.advance(100)
        self.clock.resume("white")
        self.move("e2e4", 4)
        self.assertEqual(self.clock.history[-1]["elapsed_ms"], 10000)
        self.assertEqual(self.clock.history[-1]["white_ms"], 605000)
        self.assertEqual(self.clock.history[-1]["black_ms"], 540000)
        self.validate()

    def test_adjust_uses_one_instant_at_flag_boundary(self):
        instants = iter((0.0, 599.999, 600.001))
        clock = GameClock("10+0", monotonic=lambda: next(instants))
        clock.adjust("white", 10000)
        self.assertAlmostEqual(clock.snapshot()["white_ms"], 9999, places=3)
        expired = GameClock("10+0", monotonic=self.now)
        self.now.advance(600)
        with self.assertRaises(ValueError):
            expired.adjust("white", 60000)
        self.assertEqual(expired.flagged_color, "white")
        self.assertEqual(expired.adjustments, [])
        self.assertEqual(expired.edit_revision, 0)

    def test_invalid_requests_have_no_side_effects(self):
        before = self.clock.metadata()
        for color, delta in (("white", True), ("white", 1.2), ("white", float("nan")),
                             ("white", 0), ("white", MAX_ADJUSTED_MS + 1),
                             ("white", -MAX_ADJUSTED_MS - 1), ("human", 10000), ([], 10000)):
            with self.subTest(color=color, delta=delta), self.assertRaises(ValueError):
                self.clock.adjust(color, delta)
            self.assertEqual(self.clock.metadata(), before)

    def test_clamp_records_effective_delta_and_detaches_audit(self):
        event = self.clock.adjust("white", MAX_ADJUSTED_MS)
        self.assertEqual(event["after_ms"], MAX_ADJUSTED_MS)
        self.assertEqual(event["applied_delta_ms"], MAX_ADJUSTED_MS - 600000)
        self.clock.adjust("black", -MAX_ADJUSTED_MS)
        self.assertEqual(self.clock.adjustments[-1]["applied_delta_ms"], -600000)
        event["after_ms"] = 1
        metadata = self.clock.metadata()
        metadata["adjustments"][0]["after_ms"] = 2
        self.assertEqual(self.clock.adjustments[0]["after_ms"], MAX_ADJUSTED_MS)
        self.validate()

    def test_human_zero_continues_and_new_time_does_not_repay_old_overtime(self):
        self.now.advance(610)
        self.assertEqual(self.clock.snapshot()["overtime_ms"], 10000)
        self.clock.adjust("white", 60000)
        snapshot = self.clock.snapshot()
        self.assertEqual((snapshot["white_ms"], snapshot["turn_elapsed_ms"]), (60000, 610000))
        self.assertEqual(snapshot["overtime_ms"], 0)
        self.assertIsNone(snapshot["overtime_color"])
        self.now.advance(65)
        self.assertEqual(self.clock.snapshot()["overtime_ms"], 5000)
        self.validate()
        self.move("e2e4")
        self.assertEqual(self.clock.history[-1]["elapsed_ms"], 675000)
        self.assertEqual(self.clock.history[-1]["white_ms"], 5000)
        self.validate()

    def test_editing_other_clock_preserves_human_overtime(self):
        self.now.advance(610)
        self.clock.adjust("black", 10000)
        self.now.advance(3)
        self.assertEqual(self.clock.snapshot()["overtime_ms"], 13000)
        self.clock.pause("preview")
        self.now.advance(100)
        self.clock.resume("white")
        self.now.advance(2)
        self.assertEqual(self.clock.snapshot()["overtime_ms"], 15000)
        self.validate()
        self.clock.finish()
        self.validate()

    def test_manual_human_zero_starts_overtime_now(self):
        self.now.advance(2)
        self.clock.adjust("white", -MAX_ADJUSTED_MS)
        self.assertEqual(self.clock.snapshot()["overtime_ms"], 0)
        self.now.advance(3)
        self.assertEqual(self.clock.snapshot()["overtime_ms"], 3000)
        self.validate()

    def test_active_engine_zero_flags_and_cannot_be_resurrected(self):
        self.move("e2e4", 1)
        self.clock.adjust("black", -MAX_ADJUSTED_MS)
        self.assertEqual(self.clock.expire(), "black")
        with self.assertRaises(ValueError):
            self.clock.adjust("black", 60000)
        self.assertEqual(self.load(self.record("1-0", "time_forfeit"))["summary"]["result"], "1-0")

    def test_inactive_engine_zero_waits_for_its_turn(self):
        self.clock.adjust("black", -MAX_ADJUSTED_MS)
        self.assertIsNone(self.clock.expire())
        self.clock.adjust("black", 10000)
        self.move("e2e4", 1)
        self.assertIsNone(self.clock.expire())
        self.move("e7e5", 1)
        self.validate()

    def test_unreplenished_inactive_engine_zero_flags_on_next_turn(self):
        self.clock.adjust("black", -MAX_ADJUSTED_MS)
        self.move("e2e4", 1)
        self.assertEqual(self.clock.expire(), "black")
        self.load(self.record("1-0", "time_forfeit"))

    def test_non_running_clocks_reject_edits(self):
        for state in ("disabled", "paused", "finished"):
            clock = GameClock(None if state == "disabled" else "10+0", monotonic=self.now)
            if state == "paused":
                clock.pause()
            elif state == "finished":
                clock.finish()
            before = clock.metadata()
            with self.subTest(state=state), self.assertRaises(ValueError):
                clock.adjust("black", 10000)
            self.assertEqual(clock.metadata(), before)

    def test_undo_restores_completed_ply_and_truncates_next_turn_edits(self):
        self.clock.adjust("black", 10000)
        self.move("e2e4", 1)
        retained = copy.deepcopy(self.clock.history[-1])
        self.clock.adjust("white", -60000)
        self.move("e7e5", 2)
        self.clock.adjust("black", 60000)
        before_revision = self.clock.edit_revision
        self.board.pop()
        self.clock.restore_ply(1, "black")
        self.assertEqual(self.clock.edit_revision, before_revision + 1)
        self.assertEqual([event["ply"] for event in self.clock.adjustments], [0])
        self.assertEqual(self.clock.snapshot()["white_ms"], retained["white_ms"])
        self.assertEqual(self.clock.snapshot()["black_ms"], retained["black_ms"])
        self.assertEqual(self.clock.snapshot()["turn_elapsed_ms"], 0)
        self.clock.adjust("black", 10000)
        self.move("c7c5", 3)
        self.validate()
        self.board.pop()
        self.board.pop()
        self.clock.restore_ply(0, "white")
        self.assertEqual(self.clock.adjustments, [])
        self.validate()

    def test_archive_keeps_old_completed_move_clocks_after_later_edits(self):
        self.move("e2e4", 12)
        self.move("e7e5", 2)
        historical = copy.deepcopy(self.clock.history)
        self.now.advance(3)
        self.clock.adjust("white", 60000)
        self.clock.adjust("black", -10000)
        data = self.load()
        pgn = chess.pgn.read_game(io.StringIO(data["pgn"]))
        self.assertEqual([node.clock() for node in pgn.mainline()], [593, 603])
        self.assertEqual(pgn.headers["ClockAdjustments"], "2")
        self.assertEqual(self.clock.history, historical)
        self.assertEqual(data["frames"][1]["clock"]["white_ms"], 593000)
        self.assertEqual(data["frames"][-1]["clock"]["white_ms"], 650000)
        self.assertEqual(data["frames"][-1]["clock"]["black_ms"], 593000)
        self.assertTrue(all(not row["clock"]["editable"] for row in data["frames"]))

    def test_archive_rejects_tampered_adjustment_fields(self):
        self.now.advance(2)
        self.clock.adjust("white", 60000)
        self.move("e2e4", 3)
        record = self.record()
        for field, value in (("revision", 0), ("revision", True), ("ply", 1), ("ply", -1),
                             ("color", "black"), ("delta_ms", 60001), ("delta_ms", True),
                             ("before_ms", 598001), ("after_ms", 658001),
                             ("applied_delta_ms", 60001), ("turn_elapsed_ms", 5001),
                             ("turn_elapsed_ms", float("nan"))):
            changed = copy.deepcopy(record)
            changed["clock"]["adjustments"][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.validate(changed)

    def test_archive_rejects_missing_reordered_future_or_mismatched_events(self):
        self.now.advance(1)
        self.clock.adjust("white", 60000)
        self.now.advance(1)
        self.clock.adjust("black", -10000)
        self.move("e2e4", 3)
        record = self.record()
        for mutation in ("missing", "reordered", "future", "snapshot_revision", "invalid_revision"):
            changed = copy.deepcopy(record)
            if mutation == "missing":
                changed["clock"]["adjustments"].pop(0)
            elif mutation == "reordered":
                changed["clock"]["adjustments"].reverse()
            elif mutation == "future":
                changed["clock"]["adjustments"][-1]["ply"] = 2
            elif mutation == "snapshot_revision":
                changed["clock"]["snapshot"]["edit_revision"] += 1
            else:
                changed["clock"]["edit_revision"] = -1
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.validate(changed)

    def test_archive_rejects_edit_after_strict_flag_fall(self):
        self.move("e2e4")
        self.clock.adjust("black", 60000)
        record = self.record()
        event = record["clock"]["adjustments"][0]
        event.update(before_ms=0, after_ms=60000, turn_elapsed_ms=600000)
        record["clock"]["snapshot"].update(black_ms=60000, turn_elapsed_ms=600000)
        with self.assertRaises(ValueError):
            self.validate(record)

    def test_archive_rejects_move_after_manual_engine_flag(self):
        self.move("e2e4")
        self.clock.adjust("black", -MAX_ADJUSTED_MS)
        record = self.record()
        self.board.push_uci("e7e5")
        record["uci_moves"].append("e7e5")
        record["clock"]["history"].append({"ply": 2, "white_ms": 605000,
                                            "black_ms": 5000, "elapsed_ms": 0, "mover": "black"})
        record["clock"]["snapshot"].update(ply=2, black_ms=5000, running_color="white")
        with self.assertRaises(ValueError):
            self.validate(record)

    def test_legacy_overdue_live_snapshot_keeps_old_v1_compatibility(self):
        self.move("e2e4")
        self.move("e7e5", 100)
        self.move("d2d4")
        self.now.advance(506)
        record = self.record()
        record["clock"].pop("adjustments")
        record["clock"].pop("edit_revision")
        record["clock"]["snapshot"].pop("edit_revision")
        self.assertEqual(self.load(record)["frames"][-1]["clock"]["black_ms"], 0)

    def test_legacy_v1_records_without_edit_fields_still_load(self):
        self.move("e2e4", 12)
        record = self.record()
        record["clock"].pop("adjustments")
        record["clock"].pop("edit_revision")
        record["clock"]["snapshot"].pop("edit_revision")
        data = self.load(record)
        self.assertNotIn("ClockAdjustments", data["pgn"])
        self.assertEqual(data["frames"][-1]["clock"]["white_ms"], 593000)

    def test_1000_fractional_edits_round_trip_with_overtime_and_increment(self):
        rng = random.Random(11032026)
        count = 0
        for soft in ("white", "black"):
            self.now = Now()
            self.clock = GameClock("10+5", monotonic=self.now,
                                   timeout_policy="engine_only", soft_timeout_color=soft)
            self.board = chess.Board()
            for ply in range(20):
                mover = "white" if self.board.turn else "black"
                for step in range(25):
                    self.now.advance(rng.uniform(.000001, .037999))
                    before = self.clock.snapshot()
                    if step == 0 and mover == soft:
                        # A deliberate expiry/refill must not turn earlier
                        # overtime into a debit against the newly added time.
                        self.now.advance((before[soft + "_ms"] + 33.333333) / 1000)
                        color, delta = soft, 10000
                    elif ply == 0 and step == 1:
                        color, delta = soft, MAX_ADJUSTED_MS
                    else:
                        color = rng.choice(("white", "black"))
                        delta = rng.choice((-60000, -10000, 10000, 60000))
                        if color != soft and before[color + "_ms"] + delta < 5000:
                            delta = 60000
                    self.clock.adjust(color, delta)
                    count += 1
                move = rng.choice(list(self.board.legal_moves))
                self.move(move.uci(), rng.uniform(.000001, .019999))
                record = self.record()
                record["state"]["human_color"] = soft
                self.validate(record)
            self.assertEqual(len(self.clock.adjustments), 500)
            data = self.load(record)
            self.assertEqual(len(data["moves"]), 20)
            parsed = chess.pgn.read_game(io.StringIO(data["pgn"]))
            self.assertEqual(parsed.headers["ClockAdjustments"], "500")
            self.assertEqual(len(list(parsed.mainline())), 20)
        self.assertEqual(count, 1000)


if __name__ == "__main__":
    unittest.main()
