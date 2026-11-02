"""Context and label-calibration invariants; no private games or Elo claims."""
import copy
from contextlib import closing
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import tempfile
import unittest

import chess

from sparring_rating_tests import fixture, rating


def position(*moves):
    board = chess.Board()
    for move in moves:
        board.push_uci(move)
    return board


def evidence(played, *, best="e2e4", depth=8, best_cp=30, played_cp=30,
             best_mate=None, played_mate=None, kind="cp_loss", reliability="stable"):
    return {"method": rating.EVIDENCE_METHOD, "valid": True, "kind": kind,
            "depth": depth, "best_cp": best_cp, "played_cp": played_cp,
            "best_mate": best_mate, "played_mate": played_mate,
            "best_uci": best, "played_uci": played, "reliability": reliability,
            "root_changes": 0, "cp_span": 0, "reason": "controlled_test"}


def observe(before, played, info):
    board = before.copy(stack=True)
    human = board.turn
    board.push_uci(played)
    reply = next(iter(board.legal_moves))
    row = {"policy": rating.POLICIES[-1], "ply": len(board.move_stack) + 1,
           "history_uci": [m.uci() for m in board.move_stack], "before_fen": board.fen(en_passant="fen"),
           "selected_uci": reply.uci(), "opponent_observation": {
               "valid": False, "forced": False, "ply": len(board.move_stack),
               "history_before": [m.uci() for m in before.move_stack],
               "played_uci": played, "before_fen": before.fen(en_passant="fen"),
               "rating_evidence": copy.deepcopy(info)}}
    board.push(reply)
    return board, row, rating.observations(board, [row], human)


def sample(losses, *, method=rating.METHOD):
    board, rows = fixture(losses)
    features = rating.metadata(board, rows, chess.WHITE, rating.prior("synthetic-engine"))
    if method == rating.LEGACY_METHOD:
        keys = ("human_ply", "available_ply", "history_before", "played_uci", "before_fen", "loss_cp", "depth")
        legacy_rows = [{key: row[key] for key in keys} for row in features["curve"]]
        features.update(method=method, calibration=rating.prior("synthetic-engine", method=method))
        features["curve"] = rating._legacy_curve(legacy_rows, features["calibration"])
    return board, {"completed": True, "handicap": False, "fingerprint": "synthetic-only", "features": features}


class ContextTests(unittest.TestCase):
    def test_long_games_are_not_trapped_in_the_old_1304_to_2054_band(self):
        for loss, bound, lower in ((1200, 1000, True), (0, 2200, False)):
            board, rows = fixture([loss] * 160)
            result = rating.snapshot(board, rows, chess.WHITE, rating.prior())
            self.assertLess(result["estimate"], bound) if lower else self.assertGreater(result["estimate"], bound)
            self.assertEqual(result["evidence_level"], "supported")

    def test_shallow_or_unstable_evidence_has_less_influence(self):
        values = []
        for depth, reliability in ((4, "limited"), (4, "stable"), (8, "stable")):
            _board, _row, rows = observe(chess.Board(), "g1f3", evidence("g1f3", depth=depth,
                                       reliability=reliability, played_cp=-250))
            values.append(rows[0]["information_weight"])
        self.assertEqual(values, sorted(values))
        self.assertEqual(values[-1], 1.)

    def test_easy_book_success_is_weak_evidence_but_its_large_error_is_not(self):
        _board, _row, good = observe(chess.Board(), "e2e4", evidence("e2e4"))
        _board, _row, bad = observe(chess.Board(), "e2e4", evidence("e2e4", best="d2d4", played_cp=-400))
        self.assertIn("simple_success", good[0]["weight_reasons"])
        self.assertNotIn("simple_success", bad[0]["weight_reasons"])
        self.assertLess(good[0]["information_weight"], bad[0]["information_weight"])

    def test_obvious_queen_capture_is_weak_success_evidence(self):
        before = chess.Board("6k1/5ppp/8/8/3q4/8/3R4/5K2 w - - 0 30")
        _board, _row, rows = observe(before, "d2d4", evidence("d2d4", best="d2d4"))
        self.assertEqual(rows[0]["clarity_kind"], "clear_queen_gain")
        self.assertIn("simple_success", rows[0]["weight_reasons"])

    def test_safe_recapture_success_is_weak_evidence(self):
        before = position("e2e4", "d7d5", "e4d5")
        _board, _row, rows = observe(before, "d8d5", evidence("d8d5", best="d8d5"))
        self.assertIn("simple_success", rows[0]["weight_reasons"])

    def test_decided_position_discount_does_not_hide_a_reversal(self):
        for best, played, discounted in ((1000, 650, True), (-650, -1100, True), (1000, -700, False)):
            _board, _row, rows = observe(chess.Board(), "g1f3", evidence("g1f3", best_cp=best, played_cp=played))
            self.assertEqual("already_decided" in rows[0]["weight_reasons"], discounted)

    def test_invalid_new_channel_cannot_borrow_a_valid_old_loss(self):
        board, rows = fixture([500])
        for bad in (None, {"method": rating.EVIDENCE_METHOD, "valid": False},
                    evidence("g1f3", best="e2e5"), evidence("g1f3", depth=3), evidence("d2d4"),
                    evidence("g1f3", best="g1f3", played_cp=-500), evidence("g1f3", played_cp=100)):
            changed = copy.deepcopy(rows)
            changed[0]["opponent_observation"]["rating_evidence"] = bad
            self.assertEqual(rating.observations(board, changed, chess.WHITE), [])

    def test_live_legacy_observation_requires_explicit_valid_flag(self):
        board, rows = fixture([20])
        rows[0]["opponent_observation"].pop("valid")
        self.assertEqual(rating.observations(board, rows, chess.WHITE), [])

    def test_allowed_mate_is_observed_even_when_the_old_cp_channel_is_invalid(self):
        before = position("f2f3", "e7e5")
        board, row, rows = observe(before, "g2g4", evidence("g2g4", best="g1h3", best_cp=8,
                                      played_cp=None, played_mate=-1, kind="allowed_mate"))
        curve = rating.metadata(board, [row], chess.WHITE, rating.prior())["curve"]
        self.assertEqual(rows[0]["evidence_kind"], "allowed_mate")
        self.assertIsNone(rows[0]["loss_cp"])
        self.assertEqual(curve[0]["smoothed_loss_cp"], 80)
        self.assertGreater(curve[0]["mate_penalty"], 0)
        self.assertLess(curve[0]["estimate"], 1756)

    def test_missed_mate_has_a_separate_bounded_penalty(self):
        before = position("f2f3", "e7e5", "g2g4")
        board, row, _ = observe(before, "d7d6", evidence("d7d6", best="d8h4", best_cp=None,
                               best_mate=1, played_cp=20, kind="missed_mate"))
        curve = rating.metadata(board, [row], chess.BLACK, rating.prior())["curve"]
        self.assertEqual(curve[0]["evidence_kind"], "missed_mate")
        self.assertIsNone(curve[0]["loss_cp"])
        self.assertGreater(curve[0]["mate_penalty"], 0)
        self.assertLessEqual(curve[0]["mate_penalty"], 600)

    def test_preserved_or_already_lost_mate_does_not_count_as_new_skill(self):
        for kind, value in (("mate_preserved", 3), ("already_lost", -3)):
            _board, _row, rows = observe(chess.Board(), "g1f3", evidence("g1f3", best_cp=None,
                                      played_cp=None, best_mate=value, played_mate=value, kind=kind))
            self.assertEqual(rows, [])

    def test_ignored_mate_event_does_not_age_evidence_or_advance_calibration_stage(self):
        board, decisions = fixture([0, 100, 300])
        middle = decisions[1]["opponent_observation"]
        middle["rating_evidence"] = evidence(middle["played_uci"], best="e2e4", best_cp=None,
                                             played_cp=None, best_mate=-3, played_mate=-3, kind="already_lost")
        with_ignored = rating.snapshot(board, decisions, chess.WHITE, rating.prior())
        without = rating.snapshot(board, [decisions[0], decisions[2]], chess.WHITE, rating.prior())
        self.assertEqual(with_ignored, without)
        self.assertEqual(with_ignored["observations"], 2)

    def test_saved_context_weights_and_future_estimates_are_not_trusted(self):
        board, rows = fixture([0, 350, 0])
        data = rating.metadata(board, rows, chess.WHITE, rating.prior())
        original = rating.archive_snapshot(board, data)
        for row in data["curve"]:
            row.update(information_weight=999, estimate=3999, prior_estimate=3999, effective_observations=999)
        self.assertEqual(rating.archive_snapshot(board, data), original)
        board.pop(); board.pop()
        self.assertEqual(rating.archive_snapshot(board, data)["observations"], 2)

    def test_legacy_archive_replays_the_original_formula_and_offset(self):
        board, data = sample([0, 300, 20], method=rating.LEGACY_METHOD)
        features = data["features"]
        features["calibration"].update(offset=-250.5, calibration_games=2)
        value = rating.archive_snapshot(board, features)
        weights = [.94 ** 2, .94, 1.]
        mean = (8 * 80 + sum(w * x for w, x in zip(weights, (0, 300, 20)))) / (8 + sum(weights))
        expected = round(2400 - 400 * math.log1p(mean / 20) - 250.5)
        self.assertEqual(value["estimate"], expected)
        self.assertEqual(value["method"], rating.LEGACY_METHOD)
        self.assertNotIn("effective_observations", value)


class CalibrationTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="regicide-rating-v2-synthetic-")
        self.addCleanup(folder.cleanup)
        self.store = rating.RatingStore(folder.name)

    def test_legacy_labels_are_recomputed_without_mutating_the_database(self):
        board, data = sample([0, 300, 20] * 3, method=rating.LEGACY_METHOD)
        self.store.upsert("synthetic-legacy", 1200, data)
        before = self.store.path.read_bytes()
        model = self.store.calibration("synthetic-engine")
        self.assertEqual(model["calibration_games"], 1)
        self.assertEqual(before, self.store.path.read_bytes())
        self.assertEqual(rating.archive_snapshot(board, data["features"])["method"], rating.LEGACY_METHOD)
        with closing(sqlite3.connect(self.store.path)) as conn:
            stored = json.loads(conn.execute("SELECT sample_json FROM labels").fetchone()[0])
        self.assertEqual(stored["features"]["method"], rating.LEGACY_METHOD)

    def test_stage_specific_offsets_correct_early_and_late_game_bias_separately(self):
        _board, data = sample([0] * 8 + [1200] * 8)
        self.store.upsert("synthetic-long", 1200, data)
        model = self.store.calibration("synthetic-engine")
        self.assertNotEqual(model["offset_by_observation"][0], model["offset"])
        early_raw = data["features"]["curve"][0]["prior_estimate"]
        early = rating._curve(rating._label_rows(data["features"])[:1], model)[0]["estimate"]
        self.assertLess(abs(early - 1200), abs(early_raw + model["offset"] - 1200))
        self.assertEqual(len(model["offset_by_observation"]), 40)

    def test_a_single_short_game_is_eligible_but_less_influential(self):
        _board, short = sample([0])
        self.assertTrue(self.store.upsert("synthetic-one", 1200, short)["eligible_for_calibration"])
        first = self.store.calibration("synthetic-engine")
        _board, long = sample([0] * 8)
        self.store.upsert("synthetic-one", 1200, long)
        later = self.store.calibration("synthetic-engine")
        self.assertEqual(first["calibration_games"], 1)
        self.assertEqual(later["calibration_games"], 1)
        self.assertLess(later["offset_by_observation"][0], first["offset_by_observation"][0])
        self.assertLess(first["calibration_weight"], later["calibration_weight"])

    def test_one_extreme_label_does_not_overrule_two_agreeing_labels(self):
        _board, data = sample([50] * 8)
        for name, value in (("synthetic-a", 1600), ("synthetic-b", 1650), ("synthetic-c", 4000)):
            self.store.upsert(name, value, data)
        model = self.store.calibration("synthetic-engine")
        raw = data["features"]["curve"][-1]["prior_estimate"]
        self.assertLess(raw + model["offset"], 1900)
        self.assertEqual(model["calibration_games"], 3)
        self.assertGreater(model["calibration_spread"], 0)
        self.assertNotIn("ratings", model)
        self.assertNotIn("game_ids", model)

    def test_invalid_label_history_and_other_engine_do_not_train(self):
        _board, data = sample([10] * 8, method=rating.LEGACY_METHOD)
        self.store.upsert("synthetic-good", 1200, data)
        self.assertEqual(self.store.calibration("other-engine")["calibration_games"], 0)
        broken = copy.deepcopy(data)
        broken["features"]["curve"][-1]["history_before"][0] = "0000"
        self.assertFalse(self.store.upsert("synthetic-bad", 2500, broken)["eligible_for_calibration"])
        self.assertEqual(self.store.calibration("synthetic-engine")["calibration_games"], 1)

    def test_saved_arbitrary_rating_numbers_do_not_become_training_features(self):
        _board, data = sample([0, 100, 300] * 3)
        self.store.upsert("synthetic-a", 1200, data)
        expected = self.store.calibration("synthetic-engine")
        for point in data["features"]["curve"]:
            point.update(prior_estimate=3999, estimate=3999, information_weight=999)
        self.store.upsert("synthetic-a", 1200, data)
        self.assertEqual(self.store.calibration("synthetic-engine"), expected)

    def test_calibration_and_observation_inputs_remain_detached(self):
        board, data = sample([20] * 8)
        self.store.upsert("synthetic-a", 1200, data)
        model = self.store.calibration("synthetic-engine")
        original = copy.deepcopy(model)
        rows = rating._label_rows(data["features"])
        before_rows = copy.deepcopy(rows)
        rating._curve(rows, model)
        self.assertEqual(rows, before_rows)
        self.assertEqual(model, original)
        invalid = copy.deepcopy(model)
        invalid["offset_by_observation"][0] = float("nan")
        self.assertEqual(rating._model(invalid)["calibration_games"], 0)


if __name__ == "__main__":
    unittest.main()
