"""Verified-observation replay and private, regularized local rating labels."""
import copy
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

import chess

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import sparring_rating as rating


def fixture(losses, human_color=chess.WHITE):
    board, rows = chess.Board(), []
    moves = ("g1f3", "g8f6", "f3g1", "f6g8")
    if not human_color:
        board.push_uci(moves[0])
    for loss in losses:
        before = board.copy(stack=True)
        played = moves[len(board.move_stack) % 4]
        board.push_uci(played)
        row = {"policy": rating.POLICIES[-1], "ply": len(board.move_stack) + 1,
               "history_uci": [m.uci() for m in board.move_stack],
               "before_fen": board.fen(en_passant="fen"),
               "selected_uci": moves[len(board.move_stack) % 4],
               "opponent_observation": {"valid": True, "forced": False,
                   "ply": len(board.move_stack), "history_before": [m.uci() for m in before.move_stack],
                   "played_uci": played, "before_fen": before.fen(en_passant="fen"),
                   "loss_cp": loss, "depth": 5}}
        rows.append(row)
        board.push_uci(row["selected_uci"])
    return board, rows


class RatingTests(unittest.TestCase):
    def test_v1_v2_and_v3_verified_observations_keep_the_same_rating_scale(self):
        board, rows = fixture([0, 150, 30])
        expected = rating.snapshot(board, rows, chess.WHITE, rating.prior())
        self.assertEqual(rating.POLICIES, ("regicide-sparring-play-v1", "regicide-sparring-play-v2", "regicide-sparring-play-v3"))
        for policy in rating.POLICIES:
            versioned = copy.deepcopy(rows)
            for row in versioned:
                row["policy"] = policy
            self.assertEqual(rating.snapshot(board, versioned, chess.WHITE, rating.prior()), expected, policy)

    def test_no_observation_means_no_invented_rating(self):
        value = rating.snapshot(chess.Board(), [], chess.WHITE, rating.prior("native"))
        self.assertIsNone(value["estimate"])
        self.assertIsNone(value["delta"])
        self.assertEqual(value["status"], "awaiting_observations")
        self.assertEqual(value["target"], "chess_com_rapid")

    def test_actual_observations_change_curve_and_unknown_holds(self):
        board, rows = fixture([0, 300, 0])
        curve = rating.metadata(board, rows, chess.WHITE, rating.prior())["curve"]
        self.assertLess(curve[1]["estimate"], curve[0]["estimate"])
        self.assertGreater(curve[2]["estimate"], curve[1]["estimate"])
        current = rating.snapshot(board, rows, chess.WHITE, rating.prior())
        board.push_uci("f3g1")
        self.assertEqual(rating.snapshot(board, rows, chess.WHITE, rating.prior()), current)
        self.assertEqual(current["updated_human_ply"], 5)
        self.assertEqual(current["status"], "provisional_uncalibrated")

    def test_black_observation_pov_and_invalid_evidence(self):
        board, rows = fixture([100], chess.BLACK)
        self.assertEqual(rating.snapshot(board, rows, chess.BLACK, rating.prior())["observations"], 1)
        self.assertEqual(rating.snapshot(board, rows, chess.WHITE, rating.prior())["observations"], 0)
        for key, value in (("forced", True), ("valid", False), ("depth", 3),
                           ("loss_cp", float("nan")), ("loss_cp", -1), ("before_fen", "bad")):
            bad = copy.deepcopy(rows)
            bad[0]["opponent_observation"][key] = value
            self.assertEqual(rating.snapshot(board, bad, chess.BLACK, rating.prior())["observations"], 0)

    def test_duplicate_conflict_and_undo_do_not_manufacture_samples(self):
        board, rows = fixture([0, 100])
        self.assertEqual(len(rating.observations(board, rows + rows, chess.WHITE)), 2)
        bad = copy.deepcopy(rows[0])
        bad["opponent_observation"]["loss_cp"] = 40
        self.assertEqual(len(rating.observations(board, rows + [bad], chess.WHITE)), 1)
        board.pop()
        board.pop()
        self.assertEqual(len(rating.observations(board, rows, chess.WHITE)), 1)

    def test_archive_uses_only_available_prefix_and_recomputes_scores(self):
        board, rows = fixture([0, 300])
        metadata = rating.metadata(board, rows, chess.WHITE, rating.prior())
        metadata["curve"][-1]["estimate"] = 3999
        self.assertNotEqual(rating.archive_snapshot(board, metadata)["estimate"], 3999)
        board.pop()
        self.assertEqual(rating.archive_snapshot(board, metadata)["observations"], 1)
        metadata["curve"][0]["history_before"] = ["e2e4"]
        self.assertIsNone(rating.archive_snapshot(board, metadata))

    def test_wrong_bot_reply_and_wrong_policy_are_rejected(self):
        board, rows = fixture([10])
        rows[0]["selected_uci"] = "e7e5"
        self.assertEqual(rating.observations(board, rows, chess.WHITE), [])
        rows[0]["selected_uci"] = "g8f6"
        rows[0]["policy"] = "unknown"
        self.assertEqual(rating.observations(board, rows, chess.WHITE), [])

    def test_malformed_archive_curve_fails_closed(self):
        board, rows = fixture([20])
        data = rating.metadata(board, rows, chess.WHITE, rating.prior())
        for curve in (None, {}, 10, "invalid"):
            self.assertIsNone(rating.archive_snapshot(board, {**data, "curve": curve}))


class StoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = rating.RatingStore(temporary.name)
        board, rows = fixture([0, 30, 120])
        self.sample = {"completed": True, "handicap": False, "fingerprint": "fixed",
                       "features": rating.metadata(board, rows, chess.WHITE, rating.prior("native-a")),
                       "runtime": {"policy": rating.POLICIES[-1], "binary_sha256": "test"}}

    def test_atomic_upsert_correction_is_one_game_and_strongly_regularized(self):
        self.assertEqual(self.store.calibration("native-a")["calibration_games"], 0)
        self.store.upsert("game-a", 2500, self.sample)
        first = self.store.calibration("native-a")
        self.assertEqual(first["calibration_games"], 1)
        residual = 2500 - self.sample["features"]["curve"][-1]["prior_estimate"]
        self.assertAlmostEqual(first["offset"], residual / 11, delta=.001)
        self.store.upsert("game-a", 900, self.sample)
        second = self.store.calibration("native-a")
        self.assertEqual(second["calibration_games"], 1)
        self.assertLess(second["offset"], first["offset"])
        with closing(sqlite3.connect(self.store.path)) as connection:
            stored = connection.execute("SELECT rating,sample_json FROM labels").fetchall()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0][0], 900)
        self.assertEqual(json.loads(stored[0][1])["source"], "user_reported")
        self.assertEqual((self.store.directory / ".gitignore").read_text(), "*\n")

    def test_zero_observations_handicaps_and_other_runtime_do_not_train(self):
        self.store.upsert("one", 1600, self.sample)
        self.assertEqual(self.store.calibration("native-b")["calibration_games"], 0)
        empty = copy.deepcopy(self.sample)
        empty["features"]["curve"] = []
        self.assertFalse(self.store.upsert("empty", 1600, empty)["eligible_for_calibration"])
        modified = {**self.sample, "handicap": True}
        self.assertFalse(self.store.upsert("modified", 1600, modified)["eligible_for_calibration"])
        self.assertEqual(self.store.calibration("native-a")["calibration_games"], 1)

    def test_reopening_invalidates_training_without_deleting_reported_label(self):
        self.store.upsert("one", 1600, self.sample)
        self.store.invalidate("one")
        self.assertEqual(self.store.calibration("native-a")["calibration_games"], 0)
        self.store.upsert("one", 1650, self.sample)
        self.assertEqual(self.store.calibration("native-a")["calibration_games"], 1)

    def test_many_low_labels_can_correct_large_prior_bias_without_fixed_600_cap(self):
        for number in range(20):
            self.store.upsert(str(number), 400, self.sample)
        raw = self.sample["features"]["curve"][-1]["prior_estimate"]
        model = self.store.calibration("native-a")
        self.assertEqual(model["calibration_games"], 20)
        self.assertAlmostEqual(model["offset"], (400 - raw) * 20 / 30, delta=.001)
        self.assertLess(model["offset"], -600)

    def test_corrupt_sample_shapes_are_ignored_without_breaking_new_game(self):
        self.store.upsert("one", 1600, self.sample)
        for encoded in ("null", "[]", '{"features":null}', '{"features":{"curve":42}}'):
            with closing(sqlite3.connect(self.store.path)) as connection, connection:
                connection.execute("UPDATE labels SET sample_json=?", (encoded,))
            self.assertEqual(self.store.calibration("native-a")["calibration_games"], 0)

    def test_invalid_ratings_incomplete_and_corruption(self):
        for bad in (True, 99, 4001, 1800.0, "1800", None):
            with self.assertRaises(ValueError):
                self.store.upsert("bad", bad, self.sample)
        with self.assertRaises(ValueError):
            self.store.upsert("bad", 1800, {**self.sample, "completed": False})
        self.store.directory.mkdir()
        self.store.path.write_bytes(b"invalid database")
        model = self.store.calibration("native-a")
        self.assertEqual(model["calibration_games"], 0)
        self.assertEqual(model["storage_warning"], "local_calibration_unavailable")
        with self.assertRaises(ValueError):
            self.store.upsert("bad", 1800, self.sample)


if __name__ == "__main__":
    unittest.main()
