"""Authoritative replay and authenticated read-only active-game history."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

import chess

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from live_history import live_history
import human_play_tests as baseline


class HistoryFramesTests(unittest.TestCase):
    def test_all_actual_frames_are_exact_and_board_history_is_untouched(self):
        cases = [
            (chess.STARTING_FEN, "e2e4 e7e5 g1f3 b8c6 f1c4 g8f6 e1g1"),
            (chess.STARTING_FEN, "e2e4 a7a6 e4e5 d7d5 e5d6"),
            ("7k/P7/8/8/8/8/8/7K w - - 0 1", "a7a8n"),
        ]
        for fen, history in cases:
            with self.subTest(history=history):
                board = chess.Board(fen)
                for move in history.split():
                    board.push_uci(move)
                saved = board.copy(stack=True)
                result = live_history(board, game_id="fixture", revision=17, human_color=chess.WHITE)
                self.assertEqual(len(result["frames"]), len(board.move_stack) + 1)
                replay = chess.Board(fen)
                for ply, frame in enumerate(result["frames"]):
                    self.assertEqual(frame["fen"], replay.fen(en_passant="fen"))
                    self.assertEqual(frame["board"], {chess.square_name(s): p.symbol() for s, p in replay.piece_map().items()})
                    self.assertFalse(frame["human_to_move"])
                    self.assertFalse(frame["engine_to_move"])
                    self.assertEqual(frame["legal_moves"], [])
                    if ply < len(board.move_stack):
                        self.assertEqual(result["moves"][ply]["san"], replay.san(board.move_stack[ply]))
                        replay.push(board.move_stack[ply])
                self.assertEqual(board.fen(), saved.fen())
                self.assertEqual(board.move_stack, saved.move_stack)


class HistoryHttpTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="regicide-live-history-")
        root = Path(self.folder.name)
        binary = root / "fake.exe"
        binary.write_bytes(b"not executed")
        self.game = baseline.play_human.HumanGame(binary, root / "games", baseline.FakeFactory(), opening_variety_enabled=False)
        self.server = baseline.play_human.HumanServer(self.game)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.folder.cleanup)
        self.addCleanup(self.game.close)
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, token=True, origin=None):
        headers = {"Origin": origin or self.server.origin}
        if token:
            headers["X-Regicide-Token"] = self.server.token
        request = urllib.request.Request(self.server.origin + "/api/history", headers=headers)
        try:
            response = urllib.request.urlopen(request, timeout=3)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.loads(response.read())

    def test_history_requires_token_and_same_origin(self):
        self.assertEqual(self.request(token=False)[0], 403)
        self.assertEqual(self.request(origin="https://example.com")[0], 403)
        self.assertEqual(self.request()[0], 200)

    def test_active_snapshot_keeps_clock_search_board_and_files_running(self):
        self.game.new_game("white", 1000, self.game.revision, time_control="10+0")
        self.game.human_move("e2e4", self.game.revision)
        self.game._engine_searching = True
        before = self.game.board.copy(stack=True)
        identity = (self.game.game_id, self.game.revision)
        clock = self.game._clock.snapshot()
        files = {p.name: p.read_bytes() for p in self.game.save_dir.glob("*.*")}
        code, detail = self.request()
        time.sleep(.025)
        self.assertEqual(code, 200)
        self.assertEqual((detail["game_id"], detail["revision"]), identity)
        self.assertEqual(detail["frames"][-1]["fen"], before.fen(en_passant="fen"))
        self.assertEqual(self.game.board.fen(), before.fen())
        self.assertEqual(self.game.board.move_stack, before.move_stack)
        self.assertTrue(self.game._engine_searching)
        after = self.game._clock.snapshot()
        self.assertEqual(after["status"], "running")
        self.assertLess(after[after["running_color"] + "_ms"], clock[clock["running_color"] + "_ms"])
        self.assertEqual(files, {p.name: p.read_bytes() for p in self.game.save_dir.glob("*.*")})
        self.game._engine_searching = False


if __name__ == "__main__":
    unittest.main()
