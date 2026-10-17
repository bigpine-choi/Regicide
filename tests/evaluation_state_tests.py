"""Live evaluation checks using the actual move search, never an extra engine.

Usage: python tests/evaluation_state_tests.py
Fake UCI analysis streams make partial packets, sign conventions, cancellation
boundaries and HTTP responsiveness deterministic without an engine binary.
"""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

import chess
import chess.engine

import human_play_tests as baseline


play_human = baseline.play_human


class AnalysisStream:
    def __init__(self, owner, board):
        self.owner = owner
        self.board = board.copy(stack=True)
        move = self.owner.next_move or next(iter(self.board.legal_moves))
        self.move = chess.Move.from_uci(move) if isinstance(move, str) else move
        self.info = {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def __iter__(self):
        for packet in self.owner.packets:
            if isinstance(packet, BaseException):
                raise packet
            packet = dict(packet)
            # Regicide's completed-depth UCI records include their principal
            # variation. SimpleAnalysisResult.info accumulates partial records.
            if "score" in packet and packet.get("depth", 0) >= 1:
                packet.setdefault("pv", [self.move])
            self.info.update(packet)
            yield packet
        # A real UCI search may continue thinking after its latest completed
        # depth. Keep the game lock occupied while independent readers poll.
        if self.owner.gate is not None:
            self.owner.waiting.set()
            if not self.owner.gate.wait(5):
                raise TimeoutError("test stream gate was not released")

    def wait(self):
        return chess.engine.BestMove(self.move, None)


class StreamingEngine(baseline.FakeEngine):
    def __init__(self):
        super().__init__()
        self.packets = [{"depth": 2,
                         "score": chess.engine.PovScore(chess.engine.Cp(80), chess.BLACK)}]
        self.next_move = None
        self.gate = None
        self.waiting = threading.Event()
        self.analysis_calls = []

    def analysis(self, board, limit, *, game=None, info=None):
        self.analysis_calls.append((board.copy(stack=True), limit, game, info))
        return AnalysisStream(self, board)

    def play(self, *_args, **_kwargs):
        raise AssertionError("streaming move must use exactly its analysis search")


class EvaluationStateTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="regicide-evaluation-tests-")
        self.root = Path(self.folder.name)
        self.binary = self.root / "test-peer.exe"
        self.binary.write_bytes(b"test UCI peer; never executed")
        self.peer = StreamingEngine()
        self.game = play_human.HumanGame(self.binary, self.root / "games", lambda _: self.peer)
        self.addCleanup(self.folder.cleanup)
        self.addCleanup(self.game.close)

    def revision(self):
        return self.game.state()["revision"]

    def human(self, move="e2e4"):
        return self.game.human_move(move, self.revision())

    def engine(self, move="e7e5"):
        self.peer.next_move = move
        return self.game.engine_move(self.revision())

    def assert_unknown(self, evaluation):
        self.assertIsNone(evaluation["cp"])
        self.assertIsNone(evaluation["mate"])
        self.assertIsNone(evaluation["white_share"])

    def test_initial_state_is_unknown_and_identity_matches_board(self):
        state = self.game.state()
        evaluation = self.game.evaluation()
        self.assertEqual(evaluation["status"], "waiting")
        self.assert_unknown(evaluation)
        self.assertEqual(evaluation["game_id"], state["game_id"])
        self.assertEqual(evaluation["revision"], state["revision"])
        self.assertEqual(evaluation["fen"], state["fen"])
        self.assertEqual(self.peer.analysis_calls, [])

    def test_white_pov_and_root_provenance_survive_engine_move(self):
        before = self.human()
        state = self.engine()
        evaluation = self.game.evaluation()
        self.assertEqual(len(self.peer.analysis_calls), 1)
        self.assertEqual(evaluation["status"], "ready")
        self.assertEqual(evaluation["source"], "previous_search")
        self.assertEqual(evaluation["source_fen"], before["fen"])
        self.assertEqual(evaluation["fen"], state["fen"])
        self.assertNotEqual(evaluation["source_fen"], evaluation["fen"])
        self.assertEqual(evaluation["revision"], state["revision"])
        self.assertEqual(evaluation["cp"], -80)
        self.assertIsNone(evaluation["mate"])
        self.assertEqual(evaluation["bound"], "exact")
        self.assertEqual(evaluation["depth"], 2)
        self.assertAlmostEqual(evaluation["white_share"], .5 - .5 * 80 / 480)
        board, limit, _, _ = self.peer.analysis_calls[0]
        self.assertEqual(board.fen(en_passant="fen"), before["fen"])
        self.assertEqual(limit.time, self.game.movetime_ms / 1000)

    def test_white_engine_score_keeps_positive_sign(self):
        self.game.new_game("black", 250, self.revision())
        self.peer.packets = [{"depth": 3,
                              "score": chess.engine.PovScore(chess.engine.Cp(125), chess.WHITE)}]
        state = self.engine("e2e4")
        self.assertEqual(self.game.evaluation()["cp"], 125)
        self.assertGreater(self.game.evaluation()["white_share"], .5)
        self.assertEqual(self.peer.analysis_calls[-1][1].time, .25)

    def test_bounds_are_reversed_with_black_to_white_pov(self):
        self.human()
        self.peer.packets = [{"depth": 4, "lowerbound": True,
                              "score": chess.engine.PovScore(chess.engine.Cp(140), chess.BLACK)}]
        self.engine()
        evaluation = self.game.evaluation()
        self.assertEqual(evaluation["cp"], -140)
        self.assertEqual(evaluation["bound"], "upper")
        self.human("g1f3")
        self.peer.packets = [{"depth": 4, "upperbound": True,
                              "score": chess.engine.PovScore(chess.engine.Cp(-60), chess.BLACK)}]
        self.engine("b8c6")
        evaluation = self.game.evaluation()
        self.assertEqual(evaluation["cp"], 60)
        self.assertEqual(evaluation["bound"], "lower")

    def test_partial_packets_preserve_latest_completed_score_and_depth(self):
        self.human()
        self.peer.packets = [
            {"depth": 1, "score": chess.engine.PovScore(chess.engine.Cp(30), chess.BLACK)},
            {"depth": 4, "score": chess.engine.PovScore(chess.engine.Cp(-75), chess.BLACK)},
            {"string": "tt probes 20 hits 4"},
            {"nodes": 1000, "depth": 5},
        ]
        self.engine()
        evaluation = self.game.evaluation()
        self.assertEqual(evaluation["cp"], 75)
        self.assertEqual(evaluation["depth"], 4)

    def test_depth_zero_fallback_is_not_published_as_a_searched_score(self):
        self.human()
        self.peer.packets = [{"depth": 0,
                              "score": chess.engine.PovScore(chess.engine.Cp(0), chess.BLACK)}]
        self.engine()
        evaluation = self.game.evaluation()
        self.assert_unknown(evaluation)

    def test_mate_score_is_separate_from_centipawns_and_uses_white_pov(self):
        self.human()
        self.peer.packets = [{"depth": 6,
                              "score": chess.engine.PovScore(chess.engine.Mate(3), chess.BLACK)}]
        self.engine()
        evaluation = self.game.evaluation()
        self.assertIsNone(evaluation["cp"])
        self.assertEqual(evaluation["mate"], -3)
        self.assertEqual(evaluation["white_share"], 0)

    def test_mate_zero_keeps_winner_when_sign_is_lost_in_json_integer(self):
        self.human()
        self.peer.packets = [{"depth": 6,
                              "score": chess.engine.PovScore(chess.engine.Mate(0), chess.BLACK)}]
        self.engine()
        evaluation = self.game.evaluation()
        self.assertEqual(evaluation["mate"], 0)
        self.assertEqual(evaluation["winner"], "white")
        self.assertEqual(evaluation["white_share"], 1)

    def test_plain_play_peer_fallback_uses_its_single_actual_search(self):
        factory = baseline.FakeFactory()
        game = play_human.HumanGame(self.binary, self.root / "fallback-games", factory)
        self.addCleanup(game.close)
        state = game.human_move("e2e4", game.state()["revision"])
        peer = factory.instances[-1]
        peer.replies.append(chess.engine.PlayResult(
            chess.Move.from_uci("e7e5"), None,
            {"depth": 3, "score": chess.engine.PovScore(chess.engine.Cp(-50), chess.BLACK),
             "pv": [chess.Move.from_uci("e7e5")]}))
        game.engine_move(state["revision"])
        self.assertEqual(len(peer.calls), 1)
        self.assertEqual(game.evaluation()["cp"], 50)
        self.assertEqual(game.evaluation()["source"], "previous_search")

    def test_returned_evaluation_is_an_independent_snapshot(self):
        snapshot = self.game.evaluation()
        snapshot["revision"] = -1
        snapshot["cp"] = 9999
        self.assertEqual(self.game.evaluation()["revision"], self.revision())
        self.assertIsNone(self.game.evaluation()["cp"])

    def test_score_for_different_pv_is_not_attached_to_played_position(self):
        self.human()
        self.peer.packets = [{"depth": 2,
                              "score": chess.engine.PovScore(chess.engine.Cp(100), chess.BLACK),
                              "pv": [chess.Move.from_uci("b8c6")]}]
        self.engine("e7e5")
        self.assert_unknown(self.game.evaluation())

    def test_human_move_undo_new_game_and_handicap_invalidate_score(self):
        self.human()
        self.engine()
        self.human("g1f3")
        self.assert_unknown(self.game.evaluation())
        self.engine("b8c6")
        self.game.undo(self.revision())
        self.assert_unknown(self.game.evaluation())
        self.game.begin_handicap(2, self.revision())
        self.assert_unknown(self.game.evaluation())
        self.game.forced_move("a7a6", self.revision())
        self.assert_unknown(self.game.evaluation())
        old_id = self.game.game_id
        state = self.game.new_game("black", 100, self.revision())
        evaluation = self.game.evaluation()
        self.assert_unknown(evaluation)
        self.assertNotEqual(evaluation["game_id"], old_id)
        self.assertEqual(evaluation["revision"], state["revision"])

    def test_terminal_checkmate_draw_and_resignation_override_search_score(self):
        cases = (
            ("white", "7k/5K2/6Q1/8/8/8/8/8 w - - 0 1", "g6g7", "white", 1, "1-0"),
            ("black", "8/8/8/8/8/6q1/5k2/7K b - - 0 1", "g3g2", "black", 0, "0-1"),
            ("white", "7k/8/8/8/8/8/r7/1K6 w - - 0 1", "b1a2", None, .5, "1/2-1/2"),
        )
        for color, fen, move, winner, white_share, result in cases:
            with self.subTest(result=result):
                self.game.new_game(color, 100, self.revision())
                self.game.board = chess.Board(fen)
                self.human(move)
                evaluation = self.game.evaluation()
                self.assertEqual(evaluation["status"], "terminal")
                self.assertEqual(evaluation["source"], "result")
                self.assertEqual(evaluation["result"], result)
                self.assertEqual(evaluation["winner"], winner)
                self.assertEqual(evaluation["white_share"], white_share)
        self.game.new_game("white", 100, self.revision())
        self.game.resign(self.revision())
        evaluation = self.game.evaluation()
        self.assertEqual(evaluation["status"], "terminal")
        self.assertEqual(evaluation["winner"], "black")
        self.assertIsNone(evaluation["mate"])

    def test_stream_failure_clears_partial_evaluation(self):
        self.human()
        self.peer.packets.append(RuntimeError("synthetic stream failure"))
        state = self.engine()
        self.assertIn("synthetic stream failure", state["error"])
        self.assertEqual(self.game.evaluation()["status"], "unavailable")
        self.assert_unknown(self.game.evaluation())

    def test_hanging_stream_times_out_without_applying_move_or_holding_lock(self):
        self.game.new_game("white", 100, self.revision())
        before = self.human()
        self.peer.next_move = "e7e5"
        self.peer.timeout = .01
        self.peer.gate = threading.Event()
        original_close = self.peer.close

        def close_stream():
            original_close()
            # Closing a real SimpleEngine terminates the waiting stream.
            # Release this deterministic fake's corresponding blocking read.
            self.peer.gate.set()

        self.peer.close = close_stream
        with ThreadPoolExecutor(max_workers=1) as pool:
            move = pool.submit(self.game.engine_move, before["revision"])
            try:
                after = move.result(timeout=2)
            finally:
                self.peer.gate.set()
        self.assertTrue(self.peer.closed)
        self.assertIn("TimeoutError", after["error"])
        self.assertEqual(after["fen"], before["fen"])
        self.assertEqual(after["moves"], before["moves"])
        self.assertEqual(after["revision"], before["revision"] + 1)
        self.assertEqual(self.game.evaluation()["status"], "unavailable")
        self.assert_unknown(self.game.evaluation())
        self.assertEqual(self.game.state()["revision"], after["revision"])

    def test_evaluation_http_is_responsive_while_game_lock_is_held(self):
        state = self.human()
        self.peer.next_move = "e7e5"
        self.peer.gate = threading.Event()
        server = play_human.HumanServer(self.game)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with ThreadPoolExecutor(max_workers=2) as pool:
            move = pool.submit(self.game.engine_move, state["revision"])
            try:
                self.assertTrue(self.peer.waiting.wait(3))
                request = urllib.request.Request(server.origin + "/api/evaluation",
                                                 headers={"X-Regicide-Token": server.token})
                with urllib.request.urlopen(request, timeout=1) as response:
                    evaluation = json.load(response)
                self.assertEqual(evaluation["status"], "searching")
                self.assertEqual(evaluation["source"], "search")
                self.assertEqual(evaluation["revision"], state["revision"])
                self.assertEqual(evaluation["game_id"], state["game_id"])
                self.assertEqual(evaluation["fen"], state["fen"])
                self.assertEqual(evaluation["cp"], -80)
                self.assertFalse(move.done())
                with self.assertRaises(urllib.error.HTTPError) as denied:
                    urllib.request.urlopen(server.origin + "/api/evaluation", timeout=1)
                self.assertEqual(denied.exception.code, 403)
                denied.exception.close()
            finally:
                self.peer.gate.set()
            final = move.result(timeout=3)
            self.assertEqual(self.game.evaluation()["revision"], final["revision"])
            self.assertEqual(self.game.evaluation()["source"], "previous_search")


if __name__ == "__main__":
    unittest.main()
