"""Mode isolation, exact-position evaluation and replay of adaptive decisions."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import chess

import human_play_tests as base
from opening_variety_tests import Peer, info
from game_archive import GameArchive

host = base.play_human

# Fixed legal nonterminal history retained from the move-70 rating fixture.
# Prefixes 96/97 and 98/99 isolate both colors' 49th/50th decisions without
# executing a 50-move match or inventing earlier rating observations.
LATE_GAME_HISTORY_UCI = """b2b3 g7g5 a2a3 f8g7 d2d3 g7a1 e1d2 a1e5 h2h3 g8f6 d2e1 b8c6
f2f4 d7d5 c1b2 g5f4 b1d2 f6g8 d2c4 e7e6 c4d2 c6b4 d1b1 b4a6 e2e4 d5e4
b2a1 f7f6 c2c4 e5d4 b3b4 g8e7 b1c2 a6b8 c2c1 c7c6 g2g4 h8g8 a1b2 d4e5
b2c3 g8f8 g4g5 b8a6 c1b1 f4f3 c3a1 f8g8 b1d1 g8g6 a1b2 a8b8 d1c2 g6g7
c2a4 d8a5 d2f3 c8d7 f3d4 e8d8 g1f3 a5c7 d4b5 e5f4 f1g2 d7e8 g5g6 b8c8
b2c3 f4e3 c3a1 c8a8 h1f1 e4f3 f1f2 e3h6 f2a2 c7d6 b5d6 h6g5 c4c5 g7g6
d6b7 d8c7 g2h1 c7b7 a2h2 h7h6 a1c3 f3f2 e1e2 e8f7 c3a1 b7b8 h1e4 g5e3
e4d5 e7g8 h2f2 g6g2 a4b3 g2g5 b3c2 f7g6 c2b1 g6f5 f2f5 h6h5 f5g5 e3d4
g5f5 b8c7 f5f4 g8h6 a3a4 a8g8 d5c6 g8g6 f4f3 h6f7 a1b2 g6g7 e2d2 a6c5
b4c5 g7g1 c6b7 g1c1 b2d4 c7d8 d4f2 f7d6 f2g3 c1g1 b1b4 a7a5 f3f2 g1a1 b4c3""".split()


def late_game_board(ply):
    if not 0 <= ply <= len(LATE_GAME_HISTORY_UCI):
        raise ValueError("Prefix is outside the fixed late-game history")
    board = chess.Board()
    for uci in LATE_GAME_HISTORY_UCI[:ply]:
        board.push_uci(uci)
    return board


class SparringStateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="regicide-sparring-state-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        binary = root / "peer.exe"
        binary.write_bytes(b"controlled peer; never executed")
        self.peer = Peer()
        self.game = host.HumanGame(binary, root / "games", lambda _: self.peer,
                                  hint_engine_factory=base.FakeFactory(),
                                  evaluation_engine_factory=base.FakeFactory(), dynamic_play_enabled=True)
        self.addCleanup(self.game.close)
        self.game.new_game("white", 1000, self.game.revision, sparring_mode=True)

    def prepare(self):
        self.game.human_move("e2e4", self.game.revision)
        board = self.game.board
        self.peer.scripts = [{"move": "c7c5", "packets": [info(board, "c7c5", 40, depth)
                                                         for depth in (4, 5, 6)]}]

    def decision(self, board, selected="e7e5"):
        before = board.copy(stack=True)
        move = before.pop()
        return {"policy": host.sparring_play.POLICY, "ply": len(board.move_stack) + 1,
                "history_uci": [m.uci() for m in board.move_stack],
                "before_fen": board.fen(en_passant="fen"), "selected_uci": selected,
                "baseline_uci": "c7c5", "reason": "controlled_adaptation", "reused": False,
                "opponent_observation": {"valid": True, "forced": False, "ply": len(board.move_stack),
                    "history_before": [m.uci() for m in before.move_stack], "played_uci": move.uci(),
                    "before_fen": before.fen(en_passant="fen"), "loss_cp": 100, "depth": 6}}

    def test_new_mode_is_exclusive_and_invalid_requests_preserve_game(self):
        state = self.game.state()
        self.assertTrue(state["engine"]["sparring_mode"])
        self.assertFalse(state["engine"]["boss_mode"])
        self.assertFalse(state["engine"]["dynamic_play_enabled"])
        self.assertFalse(state["engine"]["opening_variety_enabled"])
        for bad in (1, "true", None, [], {}):
            with self.assertRaises(ValueError):
                self.game.new_game("white", 1000, self.game.revision, sparring_mode=bad)
            self.assertEqual(self.game.state(), state)
        with self.assertRaises(ValueError):
            self.game.new_game("white", 1000, self.game.revision, True, True)
        self.assertEqual(self.game.state(), state)
        self.game.new_game("white", 1000, self.game.revision)
        self.assertFalse(self.game.sparring_mode)
        self.assertTrue(self.game.state()["engine"]["dynamic_play_enabled"])

    def test_live_selector_keeps_full_baseline_and_does_not_publish_probe_scores(self):
        self.prepare()
        board = self.game.board.copy(stack=True)
        self.peer.scripts.append({"move": "e7e5", "packets": [info(board, "e7e5", -300, 4)]})
        def choose(position, best, final, packets, search, close, **options):
            self.assertEqual(options["movetime_ms"], 1000)
            self.assertEqual(best.uci(), "c7c5")
            self.assertEqual(sorted(packets), [4, 5, 6])
            self.assertEqual(self.game.evaluation()["reliability"], "stable")
            search(100, root_moves=[chess.Move.from_uci("e7e5")], publish=False, fresh=True, depth=4)
            self.assertEqual(self.game.evaluation()["cp"], -40)
            return self.decision(position), False
        with patch.object(host.sparring_play, "choose", side_effect=choose):
            state = self.game.engine_move(self.game.revision)
        self.assertIsNone(state["error"])
        self.assertEqual(state["last_move"], "e7e5")
        self.assertEqual(self.peer.searches[0]["limit"].time, 1)
        self.assertIsNone(self.peer.searches[0]["root_moves"])
        self.assertEqual(self.peer.searches[1]["root_moves"], [chess.Move.from_uci("e7e5")])
        self.assertEqual(len(self.game._sparring_decisions), 1)
        self.assertEqual(self.game._boss_decisions, {})
        self.assertEqual(self.game._dynamic_decisions, {})

    def test_live_host_switches_both_colors_only_on_their_fiftieth_move(self):
        for ply in (96, 97, 98, 99):
            with self.subTest(ply=ply):
                board = late_game_board(ply)
                self.assertFalse(board.is_game_over(claim_draw=True))
                self.assertFalse(host.sparring_play.sparring_pacing.resource_guard(board)["active"])
                self.game.new_game("black" if board.turn else "white", 1000,
                                   self.game.revision, sparring_mode=True)
                self.game.board = board
                self.game._sync_evaluation()
                state = self.game.state()
                finishing = ply >= 98
                self.assertTrue(state["engine_to_move"])
                self.assertEqual(board.fullmove_number, 50 if finishing else 49)
                self.assertEqual(state["engine"]["sparring"]["phase"], "finish" if finishing else "ramp")
                self.assertEqual(state["engine"]["sparring_analysis_only"], finishing)
                self.assertEqual(state["engine"]["extra_budget_ms"], 500 if finishing else 2000)
                self.assertEqual(state["engine"]["extra_budget_purpose"],
                                 "rating_observation" if finishing else "sparring_control_and_rating")
                self.assertAlmostEqual(host.sparring_play._ramp_fraction(board), 1 if finishing else 34 / 35)

    def test_current_and_previous_policy_archives_keep_fiftieth_move_distinct(self):
        for ply in (98, 99):
            with self.subTest(ply=ply):
                self.game.board = late_game_board(ply)
                self.game._sync_evaluation()
                self.game._save()
                path = self.game.save_dir / (self.game.game_id + ".json")
                record = json.loads(path.read_text(encoding="utf-8"))
                for policy, expected in ((host.sparring_play.POLICY, "finish"),
                                         ("regicide-sparring-play-v4", "ramp")):
                    record["sparring_play"]["policy"] = policy
                    path.write_text(json.dumps(record), encoding="utf-8")
                    replay = GameArchive(self.game.save_dir).get_game(self.game.game_id)
                    self.assertEqual(replay["frames"][-1]["engine"]["sparring"]["phase"], expected)
                    self.assertEqual(replay["frames"][-1]["engine"]["sparring"]["policy"], policy)

    def test_save_replay_undo_and_handicap_preserve_only_actual_observations(self):
        self.prepare()
        with patch.object(host.sparring_play, "choose", return_value=(self.decision(self.game.board), False)):
            self.game.engine_move(self.game.revision)
        path = self.game.save_dir / (self.game.game_id + ".json")
        record = json.loads(path.read_text(encoding="utf-8"))
        self.assertTrue(record["sparring_play"]["enabled"])
        self.assertFalse(record["boss_play"]["enabled"])
        self.assertIn('[SparringMode "1"]', self.game.pgn())
        archive = GameArchive(self.game.save_dir)
        replay = archive.get_game(self.game.game_id)
        self.assertTrue(replay["summary"]["sparring_mode"])
        self.assertEqual([frame["engine"]["sparring"]["observations"] for frame in replay["frames"]], [0, 0, 1])
        ratings = [frame["engine"]["sparring_rating"]["estimate"] for frame in replay["frames"]]
        self.assertEqual(ratings[:2], [None, None])
        self.assertIsInstance(ratings[2], int)
        saved = copy.deepcopy(self.game._sparring_decisions)
        self.game.begin_handicap(2, self.game.revision)
        self.assertEqual(self.game._sparring_decisions, {})
        self.game.cancel_handicap(self.game.revision)
        self.assertEqual(self.game._sparring_decisions, saved)
        self.game.undo(self.game.revision)
        self.assertEqual(self.game._sparring_decisions, {})
        self.assertEqual(self.game.state()["engine"]["sparring"]["observations"], 0)

    def test_old_game_without_metadata_replays_as_normal(self):
        path = self.game.save_dir / (self.game.game_id + ".json")
        record = json.loads(path.read_text(encoding="utf-8"))
        record.pop("sparring_play")
        path.write_text(json.dumps(record), encoding="utf-8")
        replay = GameArchive(self.game.save_dir).get_game(self.game.game_id)
        self.assertFalse(replay["summary"]["sparring_mode"])
        self.assertNotIn("SparringMode", replay["pgn"])

    def test_previous_policy_archive_preserves_identity_and_observations(self):
        self.prepare()
        with patch.object(host.sparring_play, "choose", return_value=(self.decision(self.game.board), False)):
            self.game.engine_move(self.game.revision)
        path = self.game.save_dir / (self.game.game_id + ".json")
        record = json.loads(path.read_text(encoding="utf-8"))
        for policy in host.sparring_play.SUPPORTED_POLICIES:
            record["sparring_play"]["policy"] = policy
            for row in record["sparring_play"]["decisions"]:
                row["policy"] = policy
            path.write_text(json.dumps(record), encoding="utf-8")
            replay = GameArchive(self.game.save_dir).get_game(self.game.game_id)
            self.assertTrue(replay["summary"]["sparring_mode"])
            self.assertIn(f'[SparringPolicy "{policy}"]', replay["pgn"])
            self.assertEqual(replay["frames"][-1]["engine"]["sparring"]["policy"], policy)
            self.assertEqual(replay["frames"][-1]["engine"]["sparring"]["observations"], 1)
        record["sparring_play"]["decisions"][0]["policy"] = "unsupported-policy"
        path.write_text(json.dumps(record), encoding="utf-8")
        replay = GameArchive(self.game.save_dir).get_game(self.game.game_id)
        self.assertEqual(replay["frames"][-1]["engine"]["sparring"]["observations"], 0)

    def test_stale_search_cannot_append_move_or_observation(self):
        self.prepare()
        def choose(board, *_args, **_kwargs):
            self.game._game_token = object()
            return self.decision(board), False
        with patch.object(host.sparring_play, "choose", side_effect=choose):
            state = self.game.engine_move(self.game.revision)
        self.assertIsNone(state["error"], "obsolete search does not attach an error to the current state")
        self.assertEqual([m.uci() for m in self.game.board.move_stack], ["e2e4"])
        self.assertEqual(self.game._sparring_decisions, {})

    def test_time_setting_does_not_change_current_search_budget_or_profile(self):
        self.prepare()
        def choose(board, *_args, **options):
            self.assertEqual(options["movetime_ms"], 1000)
            self.game.set_think_time(2000, self.game.game_id, self.game._settings_revision)
            return self.decision(board), False
        with patch.object(host.sparring_play, "choose", side_effect=choose):
            state = self.game.engine_move(self.game.revision)
        self.assertIsNone(state["error"])
        self.assertEqual(self.game._engine_movetimes[2], 1000)
        self.assertEqual(state["engine"]["movetime_ms"], 2000)
        self.assertEqual(state["engine"]["sparring"]["observations"], 1)


class SparringHTTPTests(unittest.TestCase):
    setUp = base.HumanHTTPTests.setUp
    stop_server = base.HumanHTTPTests.stop_server
    request = base.HumanHTTPTests.request

    def test_mode_payload_validation(self):
        request = {"revision": self.game.revision, "color": "white", "movetime_ms": 1000,
                   "sparring_mode": True, "boss_mode": False}
        code, _, body = self.request("/api/new", request)
        self.assertEqual(code, 200)
        state = json.loads(body)
        self.assertTrue(state["engine"]["sparring_mode"])
        request.update(revision=state["revision"], boss_mode=True)
        self.assertEqual(self.request("/api/new", request)[0], 400)
        self.assertEqual(self.game.game_id, state["game_id"])


if __name__ == "__main__":
    unittest.main()
