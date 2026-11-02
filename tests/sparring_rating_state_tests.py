"""Actual HumanGame/HTTP rating lifecycle; deterministic peers choose test moves."""
from contextlib import closing
import json
import sqlite3
import unittest
from unittest.mock import patch

import chess
import sparring_state_tests as sparring_base
import human_play_tests as base
from opening_variety_tests import info

host = base.play_human


class RatingStateTests(unittest.TestCase):
    setUp = sparring_base.SparringStateTests.setUp
    prepare = sparring_base.SparringStateTests.prepare
    decision = sparring_base.SparringStateTests.decision

    def observe(self):
        self.prepare()
        with patch.object(host.sparring_play, "choose", return_value=(self.decision(self.game.board), False)):
            return self.game.engine_move(self.game.revision)

    def test_verified_response_updates_rating_without_new_search_and_saves_curve(self):
        self.assertIsNone(self.game.state()["engine"]["sparring_rating"]["estimate"])
        self.prepare()
        self.assertIsNone(self.game.state()["engine"]["sparring_rating"]["estimate"])
        with patch.object(host.sparring_play, "choose", return_value=(self.decision(self.game.board), False)):
            state = self.game.engine_move(self.game.revision)
        value = state["engine"]["sparring_rating"]
        self.assertEqual(value["observations"], 1)
        self.assertEqual(value["updated_human_ply"], 1)
        self.assertEqual(value["status"], "provisional_uncalibrated")
        self.assertEqual(len(self.peer.searches), 1)
        record = json.loads((self.game.save_dir / (self.game.game_id + ".json")).read_text(encoding="utf-8"))
        self.assertEqual(record["sparring_rating"]["curve"][0]["estimate"], value["estimate"])
        self.assertIsNone(record["state"]["engine"]["sparring_rating"]["label"])

    def test_unknown_quality_holds_and_undo_reconstructs(self):
        previous = self.observe()["engine"]["sparring_rating"]
        self.game.human_move("g1f3", self.game.revision)
        self.assertEqual(self.game.state()["engine"]["sparring_rating"], previous)
        board = self.game.board
        self.peer.scripts = [{"move": "b8c6", "packets": [info(board, "b8c6", 20, 4)]}]
        decision = self.decision(board, "b8c6")
        decision["opponent_observation"] = {"valid": False, "reason": "insufficient_depth"}
        with patch.object(host.sparring_play, "choose", return_value=(decision, False)):
            self.game.engine_move(self.game.revision)
        self.assertEqual(self.game.state()["engine"]["sparring_rating"], previous)
        self.game.undo(self.game.revision)
        self.assertEqual(self.game.state()["engine"]["sparring_rating"], previous)
        self.game.undo(self.game.revision)
        self.assertIsNone(self.game.state()["engine"]["sparring_rating"]["estimate"])

    def test_handicap_preview_cancel_restores_curve(self):
        original = self.observe()["engine"]["sparring_rating"]
        self.game.begin_handicap(2, self.game.revision)
        self.assertIsNone(self.game.state()["engine"]["sparring_rating"]["estimate"])
        self.game.cancel_handicap(self.game.revision)
        self.assertEqual(self.game.state()["engine"]["sparring_rating"], original)

    def test_completed_label_correction_is_private_revisioned_and_next_game_only(self):
        original = self.observe()["engine"]["sparring_rating"]
        self.game.resign(self.game.revision)
        before_revision = self.game.revision
        state = self.game.label_sparring_rating(2500, self.game.game_id, before_revision)
        value = state["engine"]["sparring_rating"]
        self.assertEqual(state["revision"], before_revision + 1)
        self.assertEqual(value["estimate"], original["estimate"])
        self.assertEqual(value["calibration_games"], 0)
        self.assertEqual(value["label"]["rating"], 2500)
        self.assertTrue(value["label"]["eligible_for_calibration"])
        path = self.game.save_dir / (self.game.game_id + ".json")
        record = json.loads(path.read_text(encoding="utf-8"))
        self.assertIsNone(record["state"]["engine"]["sparring_rating"]["label"])
        self.assertNotIn("user_reported", path.read_text(encoding="utf-8"))
        self.game.label_sparring_rating(1600, self.game.game_id, self.game.revision)
        with closing(sqlite3.connect(self.game._rating_store.path)) as connection:
            self.assertEqual(connection.execute("SELECT rating FROM labels").fetchall(), [(1600,)])
        self.game.new_game("white", 1000, self.game.revision, sparring_mode=True)
        value = self.game.state()["engine"]["sparring_rating"]
        self.assertEqual(value["calibration_games"], 1)
        self.assertEqual(value["source"], "local_regularized_calibration")
        self.assertIsNone(value["estimate"])

    def test_live_stale_and_invalid_payloads_cannot_write(self):
        game_id, revision = self.game.game_id, self.game.revision
        with self.assertRaises(ValueError):
            self.game.label_sparring_rating(1800, game_id, revision)
        self.game.resign(revision)
        with self.assertRaises(host.RevisionError):
            self.game.label_sparring_rating(1800, game_id, revision)
        with self.assertRaises(host.RevisionError):
            self.game.label_sparring_rating(1800, "other-game", self.game.revision)
        for bad in (True, 99, 4001, 1500.0, "1500"):
            with self.assertRaises(ValueError):
                self.game.label_sparring_rating(bad, game_id, self.game.revision)
        self.assertFalse(self.game._rating_store.path.exists())

    def test_zero_observation_game_can_be_labeled_but_not_trained(self):
        self.game.resign(self.game.revision)
        value = self.game.label_sparring_rating(1700, self.game.game_id, self.game.revision)["engine"]["sparring_rating"]
        self.assertFalse(value["label"]["eligible_for_calibration"])
        self.game.new_game("black", 1000, self.game.revision, sparring_mode=True)
        self.assertEqual(self.game._rating_calibration["calibration_games"], 0)

    def test_undo_of_labeled_game_revokes_training_and_stale_form(self):
        self.observe()
        self.game.resign(self.game.revision)
        self.game.label_sparring_rating(1700, self.game.game_id, self.game.revision)
        old_revision = self.game.revision
        self.game.undo(old_revision)
        self.assertIsNone(self.game.state()["engine"]["sparring_rating"]["label"])
        key = self.game._rating_calibration["evaluation_key"]
        self.assertEqual(self.game._rating_store.calibration(key)["calibration_games"], 0)
        with self.assertRaises(host.RevisionError):
            self.game.label_sparring_rating(1800, self.game.game_id, old_revision)

    def test_failed_store_preserves_revision_label_and_game(self):
        self.observe()
        self.game.resign(self.game.revision)
        before = self.game.state()
        with patch.object(self.game._rating_store, "upsert", side_effect=ValueError("disk failed")):
            with self.assertRaises(ValueError):
                self.game.label_sparring_rating(1900, self.game.game_id, self.game.revision)
        self.assertEqual(self.game.state(), before)

    def test_normal_mode_has_no_rating_and_rejects_labels(self):
        self.game.new_game("white", 1000, self.game.revision)
        self.game.resign(self.game.revision)
        self.assertIsNone(self.game.state()["engine"]["sparring_rating"])
        with self.assertRaises(ValueError):
            self.game.label_sparring_rating(1800, self.game.game_id, self.game.revision)

    def _quality_only_turn(self, expected_reason):
        board = self.game.board
        baseline = next(iter(board.legal_moves)).uci()
        self.peer.scripts = [{"move": baseline, "packets": [info(board, baseline, 30, 4)]}]
        observation = self.decision(board, baseline)["opponent_observation"]
        observed = []
        def observe(position, search, close, *, budget_ms, forced):
            self.assertEqual(position.fen(en_passant="fen"), board.fen(en_passant="fen"))
            self.assertFalse(forced)
            self.assertEqual(self.game.state()["engine"]["active_extra_budget_ms"], 500)
            observed.append(budget_ms)
            return observation, False
        before = self.game.state()
        self.assertEqual(before["engine"]["extra_budget_ms"], 500)
        self.assertTrue(before["engine"]["sparring_analysis_only"])
        self.assertEqual(before["engine"]["extra_budget_purpose"], "rating_observation")
        with patch.object(host.sparring_play.boss_play, "observe_human_move", side_effect=observe):
            state = self.game.engine_move(self.game.revision)
        self.assertIsNone(state["error"])
        self.assertEqual(state["last_move"], baseline)
        self.assertEqual(observed, [500])
        self.assertEqual(len(self.peer.searches), 1)
        self.assertEqual(self.peer.searches[0]["limit"].time, 1)
        row = self.game._sparring_decisions[len(self.game.board.move_stack)]
        self.assertEqual(row["reason"], expected_reason)
        self.assertTrue(row["analysis_only"])
        self.assertEqual(row["candidate_budget_ms"], 0)
        self.assertEqual(row["quality_budget_ms"], 500)
        self.assertEqual(row["probes"], [])
        self.assertEqual(row["discovery"], [])
        self.assertEqual(row["baseline_uci"], baseline)
        self.assertEqual(row["selected_uci"], baseline)
        self.assertEqual(state["engine"]["sparring_rating"]["observations"], 1)
        self.assertEqual(state["engine"]["sparring_rating"]["updated_human_ply"], len(board.move_stack) - 1)

    def test_each_colors_move50_keeps_baseline_and_continues_rating_observations(self):
        for ply in (98, 99):
            with self.subTest(engine_color="white" if ply == 98 else "black"):
                board = sparring_base.late_game_board(ply)
                self.assertEqual(board.fullmove_number, 50)
                self.assertFalse(board.is_game_over(claim_draw=True))
                self.game.new_game("black" if board.turn else "white", 1000,
                                   self.game.revision, sparring_mode=True)
                self.game.board = board
                self.game._sync_evaluation()
                self.peer.searches.clear()
                self._quality_only_turn("finish_full_strength")
                row = self.game._sparring_decisions[ply + 1]
                self.assertEqual(row["phase"], "finish")
                self.assertFalse(row["band_control"]["active"])
                self.assertEqual(row["profile"]["target_loss_cp"], 0)
                self.assertEqual(self.game.state()["engine"]["sparring_rating"]["updated_human_ply"], ply)

    def test_move70_fixture_still_updates_rating_without_changing_full_strength_move(self):
        # Fixed legal, nonterminal 139-ply history. No engine or random search
        # creates this fixture; it reaches Black's 70th move from standard start.
        board = sparring_base.late_game_board(139)
        self.assertEqual(len(board.move_stack), 139)
        self.assertFalse(board.is_game_over(claim_draw=True))
        self.game.board = board
        self.game._sync_evaluation()
        self._quality_only_turn("finish_full_strength")

    def test_resource_guard_quality_updates_rating_without_candidate_search(self):
        self.game.human_move("e2e4", self.game.revision)
        guard = {"active": True, "reason": "controlled_critical", "material_concessions_blocked": True}
        with patch.object(host.sparring_play.sparring_pacing, "resource_guard", return_value=guard):
            self._quality_only_turn("winning_resources_guard")


class RatingHTTPTests(unittest.TestCase):
    setUp = base.HumanHTTPTests.setUp
    stop_server = base.HumanHTTPTests.stop_server
    request = base.HumanHTTPTests.request

    def test_endpoint_requires_current_terminal_sparring_game_and_corrects(self):
        self.game.new_game("white", 1000, self.game.revision, sparring_mode=True)
        request = {"game_id": self.game.game_id, "revision": self.game.revision, "rating": 1500}
        self.assertEqual(self.request("/api/sparring/rating-label", request)[0], 400)
        self.game.resign(self.game.revision)
        request["revision"] = self.game.revision
        code, _, body = self.request("/api/sparring/rating-label", request)
        self.assertEqual(code, 200)
        state = json.loads(body)
        self.assertEqual(state["engine"]["sparring_rating"]["label"]["rating"], 1500)
        self.assertEqual(self.request("/api/sparring/rating-label", request)[0], 409)
        request.update(revision=state["revision"], rating=1600)
        self.assertEqual(self.request("/api/sparring/rating-label", request)[0], 200)


if __name__ == "__main__":
    unittest.main()
