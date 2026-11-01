"""Position editing validation, isolated advice and HTTP regression checks.

Usage: python tests/analysis_tests.py [path/to/regicide]
Fake peers cover cancellation deterministically; the optional actual binary
must itself return a legal recommendation for each side and a promotion.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

import chess

import human_play_tests as baseline
from hint_state_tests import HintFactory, HintPeer, wait_for

from analysis_session import AnalysisSession, AnalysisRevisionError, validated_position

play_human = baseline.play_human
REAL_BINARY = None


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="regicide-analysis-tests-")
        self.root = Path(self.folder.name)
        self.binary = self.root / "test-peer.exe"
        self.binary.write_bytes(b"fake UCI peer; never executed")
        self.factory = HintFactory()
        self.analysis = AnalysisSession(self.binary, "Regicide fake", self.factory)
        self.addCleanup(self.folder.cleanup)
        self.addCleanup(self.analysis.close)

    def start(self, fen=chess.STARTING_FEN, ms=100):
        return self.analysis.start(fen, ms, self.analysis.revision)

    def ready(self):
        return wait_for(lambda: self.analysis.snapshot()["hint"]
                        if self.analysis.snapshot()["hint"]["status"] == "ready" else None)

    def test_starts_inactive_and_read_only_snapshot(self):
        state = self.analysis.snapshot()
        self.assertEqual(state["status"], "editing")
        self.assertFalse(state["active"])
        self.assertEqual(state["hint"]["status"], "disabled")
        self.assertEqual(self.factory.instances, [])
        state["board"]["e1"] = "q"
        state["hint"]["move"] = "e2e5"
        self.assertEqual(self.analysis.snapshot()["board"]["e1"], "K")
        self.assertIsNone(self.analysis.snapshot()["hint"]["move"])

    def test_recommends_for_both_sides_without_automatically_moving(self):
        state = self.start(ms=250)
        hint = self.ready()
        self.assertEqual(state["turn"], "white")
        self.assertEqual(hint["fen"], state["fen"])
        self.assertEqual(hint["revision"], state["revision"])
        self.assertEqual(hint["game_id"], state["game_id"])
        self.assertIn(hint["move"], state["legal_moves"])
        self.assertEqual(self.analysis.snapshot()["moves"], [])
        self.assertEqual(self.factory.instances[-1].calls[0][1].time, .25)
        state = self.analysis.move("e2e4", state["revision"])
        hint = self.ready()
        self.assertEqual(state["turn"], "black")
        self.assertIn(hint["move"], state["legal_moves"])
        self.assertEqual(hint["fen"], state["fen"])
        self.assertEqual(len(self.analysis.snapshot()["moves"]), 1)
        board = self.factory.instances[-1].calls[0][0]
        self.assertEqual([move.uci() for move in board.move_stack], ["e2e4"])
        self.assertEqual(board.root().fen(), chess.STARTING_FEN)

    def test_bad_fen_is_atomic_and_reports_useful_errors(self):
        state = self.start()
        self.ready()
        before = self.analysis.snapshot()
        cases = [
            ("8/8/8/8/8/8/8/8 w - - 0 1", "왕"),
            ("4k3/8/8/8/8/8/8/4K2P w - - 0 1", "폰"),
            ("4k3/8/8/8/8/8/4k3/4K3 w - - 0 1", "왕"),
            ("4k3/8/8/8/8/8/8/4K3 w K - 0 1", "캐슬링"),
            ("4k3/8/8/8/8/8/8/4K3 w - d6 0 1", "앙파상"),
            ("4k3/8/8/3pP3/8/8/8/4K3 w - d6 1 1", "앙파상"),
            ("4k3/8/8/8/8/8/8/K3R3 w - - 0 1", "차례"),
            ("4k3/8/8/8/8/8/4K3/8 w - - 0 0", "수 번호"),
            ("4k3/8/8/8/8/8/4K3/8 w - - 0 1000000001", "수 번호"),
            ("4k3/8/8/8/8/8/4K3/8 w - - 0", "6개"),
            ("4k3/8/8/8/8/8/4K3/8 w A - 0 1", "형식"),
            ("4k3/8/8/8/8/8/4K3/8 w KK - 0 1", "형식"),
            ("4k3/8/8/8/8/8/4K3/8 w - - -1 1", "반수"),
            ("4k3/8/8/8/8/8/4K3/8 w - - 0.0 1", "반수"),
            (" " * 201, "200자"), (None, "200자"),
        ]
        for fen, message in cases:
            with self.subTest(fen=fen):
                with self.assertRaisesRegex(ValueError, message):
                    self.analysis.start(fen, 100, state["revision"])
                self.assertEqual(self.analysis.snapshot(), before)

    def test_special_moves_and_exact_undo(self):
        for fen, move in [
            ("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1", "e1g1"),
            ("4k3/8/8/3pP3/8/8/8/4K3 w - d6 0 1", "e5d6"),
            ("4k3/P7/8/8/8/8/8/4K3 w - - 0 1", "a7a8q"),
            ("4k3/8/8/8/8/8/7p/4K3 b - - 0 1", "h2h1n"),
        ]:
            with self.subTest(move=move):
                state = self.start(fen)
                self.assertIn(move, state["legal_moves"])
                expected = chess.Board(fen)
                expected.push_uci(move)
                moved = self.analysis.move(move, state["revision"])
                self.assertEqual(moved["fen"], expected.fen(en_passant="fen"))
                self.assertEqual(moved["last_move"], move)
                undone = self.analysis.undo(moved["revision"])
                self.assertEqual(undone["fen"], state["fen"])
                self.assertEqual(undone["moves"], [])
                self.assertFalse(undone["can_undo"])

    def test_terminal_positions_do_not_start_helper(self):
        for fen, status in [
            ("7k/6Q1/5K2/8/8/8/8/8 b - - 0 1", "checkmate"),
            ("7k/5Q2/5K2/8/8/8/8/8 b - - 0 1", "stalemate"),
            ("4k3/8/8/8/8/8/8/4K3 w - - 0 1", "draw"),
        ]:
            with self.subTest(status=status):
                state = self.start(fen)
                self.assertEqual(state["status"], status)
                self.assertEqual(state["legal_moves"], [])
                self.assertIsNone(state["hint"]["move"])
                self.assertEqual(self.factory.instances, [])

    def test_turn_illegality_and_stale_actions_preserve_state(self):
        state = self.start()
        self.ready()
        before = self.analysis.snapshot()
        for move in (None, True, "e7e5", "e2e5", "e2e4x", "0000"):
            with self.assertRaises(ValueError):
                self.analysis.move(move, before["revision"])
        self.assertEqual(self.analysis.snapshot(), before)
        for action in (lambda: self.analysis.move("e2e4", state["revision"] - 1),
                       lambda: self.analysis.start(chess.STARTING_FEN, 100, True),
                       lambda: self.analysis.undo(state["revision"] - 1),
                       lambda: self.analysis.stop(state["revision"] - 1)):
            with self.assertRaises(AnalysisRevisionError):
                action()
        self.assertEqual(self.analysis.snapshot(), before)

    def test_time_budget_type_and_range(self):
        before = self.analysis.snapshot()
        for ms in (True, None, 99, 5001, "100", 100.0):
            with self.assertRaises(ValueError):
                self.start(ms=ms)
            self.assertEqual(self.analysis.snapshot(), before)

    def test_inflight_replaced_position_cannot_publish_old_recommendation(self):
        old = HintPeer("e2e4", blocked=True)
        old.close_releases = False
        latest = HintPeer("e7e5")
        self.factory.peers.extend((old, latest))
        self.start()
        self.assertTrue(old.started.wait(2))
        new_board = chess.Board()
        new_board.push_uci("e2e4")
        state = self.start(new_board.fen(en_passant="fen"))
        self.assertIsNone(state["hint"]["move"])
        self.assertTrue(old.close_event.wait(2))
        old.release.set()
        hint = self.ready()
        self.assertEqual(hint["move"], "e7e5")
        self.assertEqual(hint["game_id"], state["game_id"])
        self.assertEqual(hint["revision"], state["revision"])
        self.assertEqual(hint["fen"], state["fen"])
        self.assertTrue(all(self.factory.closed_before_next))

    def test_stop_cancels_and_rejects_late_result(self):
        peer = HintPeer("e2e4", blocked=True)
        self.factory.peers.append(peer)
        state = self.start()
        self.assertTrue(peer.started.wait(2))
        stopped = self.analysis.stop(state["revision"])
        self.assertFalse(stopped["active"])
        self.assertEqual(stopped["hint"]["status"], "disabled")
        self.assertTrue(peer.close_event.wait(2))
        wait_for(lambda: self.analysis._hints._cancel is None)
        self.assertEqual(self.analysis.snapshot(), stopped)
        with self.assertRaises(ValueError):
            self.analysis.move("e2e4", stopped["revision"])

    def test_analysis_preserves_live_game_evaluation_and_archive(self):
        game = play_human.HumanGame(self.binary, self.root / "games", baseline.FakeFactory(), self.factory)
        self.addCleanup(game.close)
        before = game.state()
        pgn = game.pgn()
        evaluation = game.evaluation()
        records = {path.name: path.read_bytes() for path in game.save_dir.iterdir()}
        analysis = game.analysis
        state = analysis.start(chess.STARTING_FEN, 100, analysis.revision)
        wait_for(lambda: analysis.snapshot()["hint"]["status"] == "ready")
        state = analysis.move("e2e4", state["revision"])
        state = analysis.move("e7e5", state["revision"])
        state = analysis.undo(state["revision"])
        analysis.stop(state["revision"])
        self.assertEqual(game.state(), before)
        self.assertEqual(game.pgn(), pgn)
        self.assertEqual(game.evaluation(), evaluation)
        self.assertEqual({path.name: path.read_bytes() for path in game.save_dir.iterdir()}, records)
        self.assertFalse(game.hint()["enabled"])
        game.close()
        self.assertTrue(analysis._closed)


class AnalysisHTTPTests(baseline.HumanHTTPTests):
    def setUp(self):
        super().setUp()
        # Replace the unused default analysis peer factory with deterministic
        # fake engines; initial setup has never started a helper.
        self.game.analysis._hints._factory = HintFactory()

    def analysis_request(self, action=None, **values):
        code, _, raw = self.request("/api/analysis", {"action": action, **values} if action else None)
        return code, json.loads(raw)

    def test_analysis_authorization_and_action_flow(self):
        self.assertEqual(self.request("/api/analysis", token=False)[0], 403)
        code, state = self.analysis_request()
        self.assertEqual(code, 200)
        code, state = self.analysis_request("start", fen=chess.STARTING_FEN,
                                           movetime_ms=100, revision=state["revision"])
        self.assertEqual(code, 200)
        self.assertTrue(state["active"])
        old_revision = state["revision"]
        code, state = self.analysis_request("move", move="e2e4", revision=old_revision)
        self.assertEqual(code, 200)
        code, error = self.analysis_request("undo", revision=old_revision)
        self.assertEqual(code, 409)
        self.assertEqual(error["analysis"]["fen"], state["fen"])
        self.assertNotIn("state", error)
        code, state = self.analysis_request("undo", revision=state["revision"])
        self.assertEqual(code, 200)
        self.assertEqual(state["fen"], chess.STARTING_FEN)
        code, state = self.analysis_request("stop", revision=state["revision"])
        self.assertEqual(code, 200)
        self.assertFalse(state["active"])

    def test_analysis_concurrent_revision_accepts_only_one_move(self):
        _, state = self.analysis_request("start", fen=chess.STARTING_FEN,
                                         movetime_ms=100, revision=0)
        with ThreadPoolExecutor(max_workers=2) as pool:
            codes = list(pool.map(lambda move: self.analysis_request(
                "move", move=move, revision=state["revision"])[0], ("e2e4", "d2d4")))
        self.assertEqual(sorted(codes), [200, 409])

    def test_analysis_success_and_errors_do_not_wait_for_live_game_lock(self):
        entered, release = threading.Event(), threading.Event()

        def hold_live_game():
            with self.game._lock:
                entered.set()
                release.wait(10)

        thread = threading.Thread(target=hold_live_game, daemon=True)
        thread.start()
        self.assertTrue(entered.wait(2))
        try:
            self.assertEqual(self.analysis_request()[0], 200)
            self.assertEqual(self.analysis_request("unknown")[0], 400)
            self.assertEqual(self.analysis_request("stop", revision=-1)[0], 409)
            self.assertEqual(self.request("/api/analysis", body=b"{")[0], 400)
            self.assertEqual(self.analysis_request("start", fen="bad", movetime_ms=100, revision=0)[0], 400)
        finally:
            release.set()
            thread.join(2)


class RealAnalysisTests(unittest.TestCase):
    def test_real_engine_recommends_legal_moves_for_both_sides_and_promotion(self):
        if REAL_BINARY is None:
            self.skipTest("pass a Regicide binary for actual UCI search")
        analysis = AnalysisSession(REAL_BINARY)
        self.addCleanup(analysis.close)
        for fen in (chess.STARTING_FEN,
                    "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1",
                    "4k3/P7/8/8/8/8/8/4K3 w - - 0 1"):
            with self.subTest(fen=fen):
                state = analysis.start(fen, 100, analysis.revision)
                hint = wait_for(lambda: analysis.snapshot()["hint"]
                                if analysis.snapshot()["hint"]["status"] == "ready" else None,
                                timeout=8)
                self.assertIn(hint["move"], state["legal_moves"])
                self.assertEqual(hint["fen"], state["fen"])
                self.assertEqual(hint["revision"], state["revision"])
                self.assertEqual(analysis.snapshot()["fen"], state["fen"])
                self.assertEqual(analysis.snapshot()["moves"], [])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path, nargs="?")
    args, rest = parser.parse_known_args()
    REAL_BINARY = args.binary.resolve() if args.binary else None
    unittest.main(argv=[str(Path(__file__)), *rest], verbosity=2)
