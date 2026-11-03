"""Undo and user-imposed Regicide moves, checked against independent chess rules.

Usage: python tests/handicap_play_tests.py [path/to/regicide]
Forced moves remain legal chess moves; they are never attributed to the engine's
choice. Fake peers make history, special rules and concurrency cases exact.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest

import chess
import chess.pgn

import human_play_tests as baseline


play_human = baseline.play_human
REAL_BINARY = None


class HandicapPlayTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="regicide-handicap-tests-")
        self.root = Path(self.folder.name)
        self.binary = self.root / "test-peer.exe"
        self.binary.write_bytes(b"test peer; not executed")
        self.factory = baseline.FakeFactory()
        self.game = play_human.HumanGame(self.binary, self.root / "games", self.factory)
        self.addCleanup(self.folder.cleanup)
        self.addCleanup(self.game.close)

    def revision(self):
        return self.game.state()["revision"]

    def new(self, color="white"):
        return self.game.new_game(color, 100, self.revision())

    def human(self, uci):
        return self.game.human_move(uci, self.revision())

    def engine(self, uci):
        self.factory.instances[-1].replies.append(uci)
        return self.game.engine_move(self.revision())

    def begin(self, ply):
        return self.game.begin_handicap(ply, self.revision())

    def force(self, uci):
        return self.game.forced_move(uci, self.revision())

    def history(self):
        return [move.uci() for move in self.game.board.move_stack]

    def opening(self):
        self.new()
        for human, engine in (("e2e4", "e7e5"), ("g1f3", "b8c6"), ("f1c4", "g8f6")):
            self.human(human)
            self.engine(engine)

    def assert_rejected_without_change(self, operation, error=ValueError):
        before = self.game.state()
        fen = self.game.board.fen(en_passant="fen")
        history = self.history()
        files = {path: path.read_bytes() for path in self.game.save_dir.iterdir()}
        with self.assertRaises(error):
            operation()
        self.assertEqual(self.game.state(), before)
        self.assertEqual(self.game.board.fen(en_passant="fen"), fen)
        self.assertEqual(self.history(), history)
        self.assertEqual({path: path.read_bytes() for path in self.game.save_dir.iterdir()}, files)

    def assert_saved_replays(self):
        game, board = baseline.read_pgn(self.game.pgn())
        self.assertEqual(board.fen(en_passant="fen"), self.game.board.fen(en_passant="fen"))
        self.assertEqual([move.uci() for move in game.mainline_moves()], self.history())
        record = json.loads((self.game.save_dir / (self.game.game_id + ".json")).read_text(encoding="utf-8"))
        self.assertEqual(record["uci_moves"], self.history())
        self.assertEqual(record["final_fen"], self.game.board.fen(en_passant="fen"))
        return game, record

    def test_replacement_rewinds_before_target_and_preserves_prefix(self):
        self.opening()
        calls = len(self.factory.instances[-1].calls)
        state = self.begin(4)
        self.assertEqual(self.history(), ["e2e4", "e7e5", "g1f3"])
        self.assertTrue(state["handicap_mode"])
        self.assertFalse(state["human_to_move"])
        self.assertFalse(state["engine_to_move"])
        self.assertEqual(state["turn"], "black")
        self.assertEqual(set(state["legal_moves"]), {move.uci() for move in self.game.board.legal_moves})
        self.assertEqual(len(self.factory.instances[-1].calls), calls)
        state = self.force("a7a6")
        self.assertEqual(self.history(), ["e2e4", "e7e5", "g1f3", "a7a6"])
        self.assertFalse(state["handicap_mode"])
        self.assertTrue(state["human_to_move"])
        self.assertEqual(state["moves"][-1]["actor"], "engine")
        self.assertIs(state["moves"][-1]["forced"], True)
        self.assertEqual(len(self.factory.instances[-1].calls), calls)
        self.human("f1c4")
        self.engine("b8c6")
        received = self.factory.instances[-1].calls[-1][0]
        self.assertEqual([move.uci() for move in received.move_stack],
                         ["e2e4", "e7e5", "g1f3", "a7a6", "f1c4"])
        self.assertEqual(received.root().fen(), chess.STARTING_FEN)
        self.assert_saved_replays()

    def test_invalid_targets_and_nested_mode_preserve_everything(self):
        self.opening()
        for ply in (None, "2", 2.0, True, -1, 0, 1, 3, 5, 7, 100):
            with self.subTest(ply=ply):
                self.assert_rejected_without_change(lambda: self.begin(ply))
        self.begin(2)
        self.assert_rejected_without_change(lambda: self.begin(2))

    def test_stale_and_noninteger_revisions_reject_all_new_actions(self):
        self.opening()
        for revision in (self.revision() - 1, float(self.revision()), str(self.revision()), True, None):
            with self.subTest(revision=revision):
                self.assert_rejected_without_change(
                    lambda: self.game.begin_handicap(2, revision), play_human.RevisionError)
        self.begin(2)
        for revision in (self.revision() - 1, float(self.revision()), str(self.revision()), True, None):
            with self.subTest(revision=revision):
                self.assert_rejected_without_change(
                    lambda: self.game.forced_move("f7f6", revision), play_human.RevisionError)
                self.assert_rejected_without_change(
                    lambda: self.game.cancel_handicap(revision), play_human.RevisionError)

    def test_forced_move_only_in_mode_and_legality_is_mandatory(self):
        self.opening()
        self.assert_rejected_without_change(lambda: self.force("a7a6"))
        self.assert_rejected_without_change(lambda: self.game.cancel_handicap(self.revision()))
        self.begin(2)
        for move in (None, 2, "", "garbage", "0000", "e2e4", "e7e4", "e8e7", "a7a6q"):
            with self.subTest(move=move):
                self.assert_rejected_without_change(lambda: self.force(move))
        self.assert_rejected_without_change(lambda: self.human("a7a6"))
        self.assert_rejected_without_change(lambda: self.game.engine_move(self.revision()))
        self.assert_rejected_without_change(lambda: self.game.undo(self.revision()))
        self.assert_rejected_without_change(lambda: self.new("black"))
        self.assert_rejected_without_change(lambda: self.game.resign(self.revision()))

    def test_cancel_restores_board_history_result_and_forced_annotations(self):
        self.opening()
        self.begin(6)
        self.force("a7a6")
        self.game.resign(self.revision())
        before = self.game.state()
        fen = self.game.board.fen(en_passant="fen")
        history = self.history()
        self.begin(2)
        state = self.game.cancel_handicap(self.revision())
        self.assertEqual(state["revision"], before["revision"] + 2)
        state["revision"] = before["revision"]
        self.assertEqual(state, before)
        self.assertEqual(self.game.board.fen(en_passant="fen"), fen)
        self.assertEqual(self.history(), history)
        self.assert_saved_replays()

    def test_undo_removes_pair_and_only_discarded_forced_annotations(self):
        self.opening()
        self.begin(2)
        self.force("f7f6")
        self.human("d2d4")
        self.engine("a7a6")
        self.begin(4)
        self.force("a7a5")
        self.game.undo(self.revision())
        self.assertEqual(self.history(), ["e2e4", "f7f6"])
        state = self.game.state()
        self.assertTrue(state["moves"][1]["forced"])
        self.assertTrue(state["human_to_move"])
        self.assert_saved_replays()
        self.game.undo(self.revision())
        self.assertEqual(self.history(), [])
        self.assertTrue(self.game.state()["human_to_move"])
        self.assert_saved_replays()

    def test_black_player_can_replace_white_opening_and_undo_preserves_it(self):
        self.new("black")
        self.engine("e2e4")
        self.begin(1)
        state = self.force("f2f3")
        self.assertEqual(state["turn"], "black")
        self.assertTrue(state["human_to_move"])
        self.assertFalse(state["can_undo"])
        self.human("e7e5")
        self.engine("g2g4")
        self.game.undo(self.revision())
        self.assertEqual(self.history(), ["f2f3"])
        self.assertTrue(self.game.state()["moves"][0]["forced"])
        self.assert_saved_replays()

    def test_replace_again_prunes_old_forced_flags_and_new_game_resets_markers(self):
        self.opening()
        self.begin(4)
        self.force("a7a6")
        self.human("f1c4")
        self.engine("g8f6")
        self.begin(2)
        state = self.force("f7f6")
        self.assertEqual(len(state["moves"]), 2)
        self.assertEqual([m["ply"] for m in state["moves"] if m.get("forced")], [2])
        self.begin(2)
        self.assert_rejected_without_change(lambda: self.new("black"))
        self.game.cancel_handicap(self.revision())
        state = self.new("black")
        self.assertFalse(state["handicap_mode"])
        self.assertEqual(state["moves"], [])
        self.assertTrue(state["engine_to_move"])
        self.assert_rejected_without_change(lambda: self.game.cancel_handicap(self.revision()))

    def test_special_moves_replay_exactly_after_replacement(self):
        cases = (
            ("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1", "e1g1", "e1c1", "O-O-O"),
            ("4k3/8/8/3pP3/8/8/8/4K3 w - d6 0 1", "e5e6", "e5d6", "exd6"),
            ("7k/P7/8/8/8/8/8/4K3 w - - 0 1", "a7a8q", "a7a8n", "a8=N"),
        )
        for fen, original, forced, san in cases:
            with self.subTest(forced=forced):
                self.new("black")
                self.game.board = chess.Board(fen)
                self.engine(original)
                state = self.begin(1)
                self.assertIn(forced, state["legal_moves"])
                expected = chess.Board(fen)
                expected.push_uci(forced)
                state = self.force(forced)
                self.assertEqual(self.game.board.fen(en_passant="fen"), expected.fen(en_passant="fen"))
                self.assertEqual(state["moves"][-1]["san"], san)
                self.assert_saved_replays()

    def test_forcing_cannot_leave_checked_king_in_check(self):
        self.new("black")
        self.game.board = chess.Board("4k3/8/8/8/8/8/4r3/4K2R w - - 0 1")
        self.engine("e1d1")
        self.begin(1)
        self.assert_rejected_without_change(lambda: self.force("h1h8"))
        state = self.force("e1f1")
        self.assertFalse(state["in_check"])
        self.assert_saved_replays()

    def test_replacement_keeps_repetition_history_and_adjudicates_draw(self):
        self.new("black")
        for actor, move in (("engine", "g1f3"), ("human", "g8f6"), ("engine", "f3g1"),
                            ("human", "f6g8"), ("engine", "g1f3"), ("human", "g8f6"),
                            ("engine", "e2e4")):
            getattr(self, actor)(move)
        self.begin(7)
        state = self.force("f3g1")
        self.assertEqual(len(self.game.board.move_stack), 7)
        self.assertTrue(self.game.board.can_claim_threefold_repetition())
        self.assertFalse(chess.Board(self.game.board.fen()).can_claim_threefold_repetition())
        self.assertEqual(state["result"], "1/2-1/2")
        self.assertEqual(state["termination"], "threefold_repetition")
        self.assertFalse(state["human_to_move"])
        self.assertFalse(state["engine_to_move"])
        self.assert_saved_replays()

    def test_forced_move_preserves_fifty_move_clock(self):
        self.new("black")
        self.game.board = chess.Board("4k3/1p6/8/8/8/8/P7/4K3 w - - 98 1")
        self.engine("a2a3")
        self.begin(1)
        state = self.force("e1f1")
        self.assertEqual(self.game.board.halfmove_clock, 99)
        self.assertEqual(state["termination"], "fifty_moves")
        self.assertEqual(state["result"], "1/2-1/2")
        self.assert_saved_replays()

    def test_undo_after_first_human_checkmate_restores_exact_root(self):
        self.new()
        self.game.board = chess.Board("7k/5K2/6Q1/8/8/8/8/8 w - - 0 1")
        original = self.game.board.fen(en_passant="fen")
        state = self.human("g6g7")
        self.assertEqual(state["termination"], "checkmate")
        self.assertEqual(state["result"], "1-0")
        self.assertTrue(state["can_undo"])
        state = self.game.undo(self.revision())
        self.assertEqual(self.game.board.fen(en_passant="fen"), original)
        self.assertEqual(self.history(), [])
        self.assertEqual(state["result"], "*")
        self.assertTrue(state["human_to_move"])
        self.assert_saved_replays()

    def test_error_preview_cancel_and_forced_branch_recover_fresh_peer(self):
        self.opening()
        self.human("d2d4")
        failed_peer = self.factory.instances[-1]
        failed_peer.replies.append(chess.engine.EngineTerminatedError("intentional dead peer"))
        state = self.game.engine_move(self.revision())
        self.assertTrue(state["error"])
        self.assertTrue(failed_peer.closed)
        old_token = self.game._game_token
        self.begin(6)
        restored = self.game.cancel_handicap(self.revision())
        self.assertEqual(restored["error"], state["error"])
        self.assertEqual(restored["moves"], state["moves"])
        self.assertFalse(restored["human_to_move"])
        self.assertFalse(restored["engine_to_move"])
        self.begin(6)
        state = self.force("a7a6")
        self.assertIsNone(state["error"])
        self.assertTrue(state["human_to_move"])
        self.assertIsNot(self.game._game_token, old_token)
        self.human("d2d4")
        state = self.game.engine_move(self.revision())
        self.assertIsNone(state["error"])
        self.assertTrue(state["human_to_move"])
        self.assertEqual(len(self.factory.instances), 2)
        received = self.factory.instances[-1].calls[-1][0]
        self.assertEqual([move.uci() for move in received.move_stack],
                         ["e2e4", "e7e5", "g1f3", "b8c6", "f1c4", "a7a6", "d2d4"])
        self.assert_saved_replays()

    def test_preview_does_not_overwrite_durable_game_or_export(self):
        self.opening()
        before_pgn = self.game.pgn()
        files = {path: path.read_bytes() for path in self.game.save_dir.iterdir()}
        self.begin(2)
        self.assertEqual(self.game.pgn(), before_pgn)
        self.assertEqual({path: path.read_bytes() for path in self.game.save_dir.iterdir()}, files)
        self.game.close()
        self.assertEqual({path: path.read_bytes() for path in self.game.save_dir.iterdir()}, files)

    def test_pgn_and_saved_json_identify_user_handicap(self):
        self.opening()
        self.begin(2)
        self.force("f7f6")
        game, record = self.assert_saved_replays()
        forced_node = game.next().next()
        self.assertTrue(forced_node.comment, "PGN must disclose that the user imposed the move")
        self.assertEqual(forced_node.comment, "User-forced Regicide move (handicap)")
        self.assertEqual(game.headers["HandicapMoves"], "1")
        self.assertTrue(record["state"]["moves"][1]["forced"])
        self.assertIn("handicap", json.dumps(record, ensure_ascii=False).lower())
        event = record["handicap_events"][-1]
        self.assertEqual(event["ply"], 2)
        self.assertEqual(event["replacement"], "f7f6")
        self.assertEqual(event["removed_suffix"], ["e7e5", "g1f3", "b8c6", "f1c4", "g8f6"])


class HandicapHTTPTests(unittest.TestCase):
    # Reuse only the fixture and transport, not the baseline's test methods.
    setUp = baseline.HumanHTTPTests.setUp
    stop_server = baseline.HumanHTTPTests.stop_server
    request = baseline.HumanHTTPTests.request

    def prepare(self):
        state = self.game.state()
        self.game.human_move("e2e4", state["revision"])
        self.factory.instances[-1].replies.append("e7e5")
        return self.game.engine_move(self.game.state()["revision"])

    def post(self, route, **payload):
        code, _, body = self.request(route, payload)
        return code, json.loads(body)

    def test_handicap_routes_are_authorized_revision_checked_and_complete(self):
        state = self.prepare()
        for route, payload in (("/api/handicap", {"ply": 2}),
                               ("/api/forced-move", {"move": "f7f6"}),
                               ("/api/handicap-cancel", {})):
            with self.subTest(route=route):
                data = {"revision": state["revision"], **payload}
                self.assertEqual(self.request(route, data, token=False)[0], 403)
                self.assertEqual(self.request(route, data, headers={"Origin": "null"})[0], 403)
                self.assertEqual(self.game.state(), state)
                code, body = self.post(route, **{**data, "revision": state["revision"] - 1})
                self.assertEqual(code, 409)
                self.assertEqual(body["state"], state)
        code, state = self.post("/api/handicap", ply=2, revision=state["revision"])
        self.assertEqual(code, 200)
        self.assertTrue(state["handicap_mode"])
        code, rejected = self.post("/api/forced-move", move="e7e4", revision=state["revision"])
        self.assertEqual(code, 400)
        self.assertEqual(rejected["state"], state)
        code, state = self.post("/api/forced-move", move="f7f6", revision=state["revision"])
        self.assertEqual(code, 200)
        self.assertTrue(state["human_to_move"])
        self.assertTrue(state["moves"][-1]["forced"])
        code, pending = self.post("/api/handicap", ply=2, revision=state["revision"])
        self.assertEqual(code, 200)
        code, restored = self.post("/api/handicap-cancel", revision=pending["revision"])
        self.assertEqual(code, 200)
        self.assertEqual(restored["board"], state["board"])
        self.assertEqual(restored["moves"], state["moves"])

    def test_concurrent_forced_moves_same_revision_apply_exactly_one(self):
        state = self.prepare()
        state = self.game.begin_handicap(2, state["revision"])
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(lambda move: self.request(
                "/api/forced-move", {"revision": state["revision"], "move": move})[0],
                ("f7f6", "a7a6")))
        self.assertEqual(sorted(results), [200, 409])
        state = self.game.state()
        self.assertEqual(len(state["moves"]), 2)
        self.assertTrue(state["moves"][-1]["forced"])
        self.assertFalse(state["handicap_mode"])


class HandicapRealEngineTests(unittest.TestCase):
    def test_real_engine_resumes_from_forced_history(self):
        if REAL_BINARY is None:
            self.skipTest("supply binary path to run actual Regicide")
        with tempfile.TemporaryDirectory(prefix="regicide-handicap-real-") as folder:
            game = play_human.HumanGame(REAL_BINARY, Path(folder))
            try:
                state = game.new_game("white", 100, game.state()["revision"])
                state = game.human_move("e2e4", state["revision"])
                state = game.engine_move(state["revision"])
                self.assertIsNone(state["error"])
                state = game.begin_handicap(2, state["revision"])
                state = game.forced_move("f7f6", state["revision"])
                state = game.human_move("d2d4", state["revision"])
                state = game.engine_move(state["revision"])
                self.assertIsNone(state["error"])
                self.assertEqual([move["uci"] for move in state["moves"][:3]],
                                 ["e2e4", "f7f6", "d2d4"])
                self.assertTrue(state["human_to_move"])
                _, board = baseline.read_pgn(game.pgn())
                self.assertEqual(board.fen(), game.board.fen())
                print("\nActual Regicide resumed after user-imposed f7f6; 100 ms per move.", flush=True)
            finally:
                game.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path, nargs="?")
    args, rest = parser.parse_known_args()
    REAL_BINARY = args.binary.resolve() if args.binary else None
    unittest.main(argv=[str(Path(__file__)), *rest], verbosity=2)
