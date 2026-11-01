"""Optional human-move advice isolation, cancellation and HTTP checks.

Usage: python tests/hint_state_tests.py [path/to/regicide]
An optional actual-binary smoke confirms legal advice using Regicide itself.
"""

from concurrent.futures import ThreadPoolExecutor
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
import chess.engine

import human_play_tests as baseline


play_human = baseline.play_human
REAL_BINARY = None


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.005)
    raise AssertionError("Timed out waiting for deterministic hint state")


class HintPeer(baseline.FakeEngine):
    def __init__(self, move=None, blocked=False, failure=None, depth=4):
        super().__init__()
        self.move = move
        self.failure = failure
        self.depth = depth
        self.started = threading.Event()
        self.release = threading.Event()
        self.close_event = threading.Event()
        self.close_releases = True
        self.blocked = blocked

    def play(self, board, limit, *, game=None, info=None, root_moves=None):
        self.calls.append((board.copy(stack=True), limit, game, info))
        self.started.set()
        if self.blocked and not self.release.wait(5):
            raise TimeoutError("test peer was not released")
        if self.failure:
            raise self.failure
        allowed = list(root_moves) if root_moves is not None else list(board.legal_moves)
        preferred = chess.Move.from_uci(self.move) if self.move else None
        move = preferred if preferred is not None and (preferred in allowed or preferred not in board.legal_moves) else allowed[0]
        # Return even after close to exercise stale-result rejection.
        return chess.engine.PlayResult(move, None, {"depth": limit.depth or self.depth,
              "score": chess.engine.PovScore(chess.engine.Cp(100 if move == preferred else 0), board.turn), "pv": [move]})

    def close(self):
        super().close()
        self.close_event.set()
        if self.close_releases:
            self.release.set()


class HintFactory:
    def __init__(self, peers=()):
        self.peers = list(peers)
        self.instances = []
        self.closed_before_next = []

    def __call__(self, _binary):
        self.closed_before_next.append(all(peer.closed for peer in self.instances))
        peer = self.peers.pop(0) if self.peers else HintPeer()
        self.instances.append(peer)
        return peer


class HintStateTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="regicide-hint-tests-")
        self.root = Path(self.folder.name)
        self.binary = self.root / "test-peer.exe"
        self.binary.write_bytes(b"test UCI peer, never executed")
        self.main_factory = baseline.FakeFactory()
        self.hint_factory = HintFactory()
        self.game = play_human.HumanGame(self.binary, self.root / "games",
                                         self.main_factory, self.hint_factory,
                                         evaluation_engine_factory=baseline.FakeFactory(),
                                         opening_variety_enabled=False)
        self.addCleanup(self.folder.cleanup)
        self.addCleanup(self.game.close)
        # The always-on evaluator is independent of advice. Let its initial
        # generation settle before comparing snapshots around hint-only work.
        wait_for(lambda: self.game.evaluation()["status"] in ("ready", "unavailable"))

    def revision(self):
        return self.game.state()["revision"]

    def ready(self):
        return wait_for(lambda: self.game.hint() if self.game.hint()["status"] == "ready" else None)

    def human(self, move="e2e4"):
        return self.game.human_move(move, self.revision())

    def engine(self, move="e7e5"):
        self.main_factory.instances[-1].replies.append(move)
        return self.game.engine_move(self.revision())

    def assert_empty(self, hint):
        self.assertEqual(hint["candidates"], [])
        for key in ("move", "san", "from", "to", "promotion", "depth"):
            self.assertIsNone(hint[key], key)

    def test_default_off_never_starts_helper_or_alters_game(self):
        original = self.game.state()
        self.assertFalse(original["hint_enabled"])
        self.assertEqual(self.game.hint()["status"], "disabled")
        self.assert_empty(self.game.hint())
        self.human()
        self.engine()
        self.game.undo(self.revision())
        self.assertEqual(self.hint_factory.instances, [])

    def test_advice_is_legal_read_only_and_preserves_archive_and_evaluation(self):
        before = self.game.state()
        evaluation = self.game.evaluation()
        pgn = self.game.pgn()
        records = {path.name: path.read_bytes() for path in self.game.save_dir.iterdir()}
        self.game.set_hint_enabled(True)
        hint = self.ready()
        self.assertTrue(hint["enabled"])
        self.assertIn(chess.Move.from_uci(hint["move"]), self.game.board.legal_moves)
        self.assertEqual(hint["san"], self.game.board.san(chess.Move.from_uci(hint["move"])))
        self.assertEqual(hint["from"], hint["move"][:2])
        self.assertEqual(hint["to"], hint["move"][2:4])
        for key in ("revision", "game_id", "fen", "moves", "result", "updated_at"):
            self.assertEqual(self.game.state()[key], before[key])
        self.assertEqual(self.game.evaluation(), evaluation)
        self.assertEqual(self.game.pgn(), pgn)
        self.assertEqual({path.name: path.read_bytes() for path in self.game.save_dir.iterdir()}, records)
        self.assertGreater(self.hint_factory.instances[0].calls[0][1].time, .9)
        self.assertLessEqual(self.hint_factory.instances[0].calls[0][1].time, 1)
        wait_for(lambda: self.hint_factory.instances[0].closed)
        changed = self.game.hint()
        changed["move"] = "a1a8"
        changed["candidates"][0]["move"] = "a1a8"
        changed["candidates"].clear()
        self.assertEqual(self.game.hint()["move"], hint["move"])
        self.assertEqual(self.game.hint()["candidates"], hint["candidates"])

    def test_recomputes_for_both_colors_with_selected_time_and_full_history(self):
        self.game.new_game("black", 250, self.revision())
        self.game.set_hint_enabled(True)
        self.assertEqual(self.game.hint()["status"], "waiting")
        self.assertEqual(self.hint_factory.instances, [])
        state = self.engine("e2e4")
        hint = self.ready()
        self.assertEqual(hint["revision"], state["revision"])
        self.assertEqual(hint["fen"], state["fen"])
        searched, limit, _, _ = self.hint_factory.instances[-1].calls[0]
        self.assertEqual(searched.turn, chess.BLACK)
        self.assertEqual(searched.move_stack, self.game.board.move_stack)
        self.assertEqual(searched.root().fen(), self.game.board.root().fen())
        self.assertGreater(limit.time, .2)
        self.assertLessEqual(limit.time, .25)
        self.game.human_move(hint["move"], self.revision())
        self.assertEqual(self.game.hint()["status"], "waiting")
        self.assert_empty(self.game.hint())

    def test_off_cancels_and_discards_completion_even_if_peer_returns_move(self):
        peer = HintPeer("e2e4", blocked=True)
        self.hint_factory.peers.append(peer)
        self.game.set_hint_enabled(True)
        self.assertTrue(peer.started.wait(2))
        off = self.game.set_hint_enabled(False)
        self.assertEqual(off["status"], "disabled")
        self.assert_empty(off)
        self.assertTrue(peer.close_event.wait(2))
        wait_for(lambda: self.game._hints._cancel is None)
        self.assertEqual(self.game.hint(), off)

    def test_move_invalidates_inflight_and_recomputes_latest_position(self):
        old = HintPeer("e2e4", blocked=True)
        old.close_releases = False
        latest = HintPeer("g1f3")
        self.hint_factory.peers.extend((old, latest))
        self.game.set_hint_enabled(True)
        self.assertTrue(old.started.wait(2))
        self.human()
        self.assert_empty(self.game.hint())
        self.assertTrue(old.close_event.wait(2))
        state = self.engine()
        self.assertEqual(len(self.hint_factory.instances), 1)
        self.assert_empty(self.game.hint())
        old.release.set()
        hint = self.ready()
        self.assertEqual(hint["move"], "g1f3")
        self.assertEqual(hint["fen"], state["fen"])
        self.assertEqual(hint["revision"], state["revision"])
        self.assertTrue(all(self.hint_factory.closed_before_next))

    def test_rapid_on_off_keeps_only_one_worker_and_pending_latest_request(self):
        peer = HintPeer("e2e4", blocked=True)
        peer.close_releases = False
        self.hint_factory.peers.append(peer)
        self.game.set_hint_enabled(True)
        self.assertTrue(peer.started.wait(2))
        worker = self.game._hints._worker
        for _ in range(25):
            self.game.set_hint_enabled(False)
            self.game.set_hint_enabled(True)
        self.assertEqual(len(self.hint_factory.instances), 1)
        self.assertIs(self.game._hints._worker, worker)
        peer.release.set()
        self.ready()
        self.assertEqual(len(self.hint_factory.instances), 2)
        self.assertTrue(all(self.hint_factory.closed_before_next))

    def test_undo_new_handicap_resign_and_terminal_clear_or_recompute(self):
        self.game.set_hint_enabled(True)
        self.ready()
        self.human()
        self.engine()
        self.ready()
        self.game.begin_handicap(2, self.revision())
        self.assertEqual(self.game.hint()["status"], "waiting")
        self.assert_empty(self.game.hint())
        self.game.cancel_handicap(self.revision())
        self.ready()
        self.game.begin_handicap(2, self.revision())
        self.game.forced_move("a7a6", self.revision())
        self.ready()
        self.game.undo(self.revision())
        self.assertEqual(self.ready()["fen"], chess.STARTING_FEN)
        old_id = self.game.game_id
        self.game.new_game("white", 100, self.revision())
        self.assertNotEqual(self.ready()["game_id"], old_id)
        self.game.resign(self.revision())
        self.assertEqual(self.game.hint()["status"], "unavailable")
        self.assert_empty(self.game.hint())
        self.game.new_game("white", 100, self.revision())
        with self.game._lock:
            self.game.board = chess.Board("7k/5K2/6Q1/8/8/8/8/8 w - - 0 1")
            self.game.revision += 1
            self.game._sync_evaluation()
        self.human("g6g7")
        self.assertEqual(self.game.hint()["status"], "unavailable")
        self.assert_empty(self.game.hint())

    def test_promotion_has_explicit_piece_and_san(self):
        self.hint_factory.peers.append(HintPeer("a7a8q"))
        with self.game._lock:
            self.game.board = chess.Board("7k/P7/8/8/8/8/8/7K w - - 0 1")
            self.game.revision += 1
            self.game._sync_evaluation()
        self.game.set_hint_enabled(True)
        hint = self.ready()
        self.assertEqual(hint["promotion"], "q")
        self.assertEqual(hint["san"], "a8=Q+")
        self.assertEqual(hint["from"], "a7")
        self.assertEqual(hint["to"], "a8")

    def test_failure_and_illegal_advice_are_isolated_and_can_retry(self):
        evaluation = self.game.evaluation()
        pgn = self.game.pgn()
        self.hint_factory.peers.extend((HintPeer(failure=RuntimeError("broken helper")),
                                        HintPeer("a1a8"), HintPeer("e2e4")))
        for _ in range(2):
            self.game.set_hint_enabled(True)
            wait_for(lambda: self.game.hint()["status"] == "unavailable")
            self.assert_empty(self.game.hint())
            self.assertIsNone(self.game.error)
            self.assertEqual(self.game.evaluation(), evaluation)
            self.assertEqual(self.game.pgn(), pgn)
            self.game.set_hint_enabled(False)
        self.game.set_hint_enabled(True)
        self.assertEqual(self.ready()["move"], "e2e4")

    def test_timeout_closes_helper_and_publishes_no_move(self):
        self.game.new_game("white", 100, self.revision())
        self.game._hints._timeout_grace = .01
        peer = HintPeer("e2e4", blocked=True)
        self.hint_factory.peers.append(peer)
        self.game.set_hint_enabled(True)
        wait_for(lambda: self.game.hint()["status"] == "unavailable")
        self.assertTrue(peer.closed)
        self.assertIn("초과", self.game.hint()["message"])
        self.assert_empty(self.game.hint())
        self.assertIsNone(self.game.error)

    def test_off_during_helper_launch_closes_it_without_search(self):
        entered = threading.Event()
        release = threading.Event()
        peer = HintPeer()

        def launch(_binary):
            entered.set()
            self.assertTrue(release.wait(3))
            return peer

        self.game._hints._factory = launch
        self.game.set_hint_enabled(True)
        self.assertTrue(entered.wait(2))
        self.game.set_hint_enabled(False)
        release.set()
        self.assertTrue(peer.close_event.wait(2))
        self.assertEqual(peer.calls, [])
        self.assertEqual(self.game.hint()["status"], "disabled")

    def test_close_cancels_live_helper_and_joins_worker(self):
        peer = HintPeer(blocked=True)
        self.hint_factory.peers.append(peer)
        self.game.set_hint_enabled(True)
        self.assertTrue(peer.started.wait(2))
        self.game.close()
        self.assertTrue(peer.closed)
        self.assertFalse(self.game._hints._worker.is_alive())
        self.assertEqual(self.game.hint()["status"], "disabled")

    def test_invalid_preference_does_not_change_position_or_launch(self):
        before = self.game.hint()
        for invalid in (1, "true", None, [], {}):
            with self.assertRaises(ValueError):
                self.game.set_hint_enabled(invalid)
            self.assertEqual(self.game.hint(), before)
        self.assertEqual(self.hint_factory.instances, [])

    def test_http_auth_and_toggle_stay_responsive_while_opponent_holds_game_lock(self):
        state = self.human()
        peer = HintPeer("e7e5", blocked=True)
        self.game._engine = peer
        server = play_human.HumanServer(self.game)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def request(method="GET", payload=None, token=True, origin=None):
            headers = {"Content-Type": "application/json"}
            if token:
                headers["X-Regicide-Token"] = server.token
            if origin is not None:
                headers["Origin"] = origin
            data = json.dumps(payload).encode() if payload is not None else None
            req = urllib.request.Request(server.origin + "/api/hint", data=data,
                                         headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=1) as response:
                return json.load(response)

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.game.engine_move, state["revision"])
            try:
                self.assertTrue(peer.started.wait(2))
                hint = request("POST", {"enabled": True})  # No revision required.
                self.assertTrue(hint["enabled"])
                self.assertEqual(hint["status"], "waiting")
                self.assertEqual(request()["revision"], state["revision"])
                self.assertFalse(request("POST", {"enabled": False})["enabled"])
                self.assertFalse(future.done())
                for method, payload, token, origin in (
                        ("GET", None, False, None),
                        ("POST", {"enabled": True}, False, None),
                        ("POST", {"enabled": True}, True, "http://evil.invalid")):
                    with self.assertRaises(urllib.error.HTTPError) as denied:
                        request(method, payload, token, origin)
                    self.assertEqual(denied.exception.code, 403)
                    denied.exception.close()
                with self.assertRaises(urllib.error.HTTPError) as invalid:
                    request("POST", {"enabled": "true"})
                self.assertEqual(invalid.exception.code, 400)
                invalid.exception.close()
                self.assertEqual(self.hint_factory.instances, [])
            finally:
                peer.release.set()
            future.result(timeout=3)


@unittest.skipUnless(REAL_BINARY, "actual binary not requested")
class RealHintSmoke(unittest.TestCase):
    def test_actual_regicide_advice_legal_without_playing_and_cancels(self):
        with tempfile.TemporaryDirectory(prefix="regicide-hint-real-") as folder:
            helpers = []
            searching = threading.Event()

            def hint_engine(binary):
                engine = chess.engine.SimpleEngine.popen_uci([str(binary), "uci"], timeout=3)
                original_analysis = engine.analysis

                def tracked_analysis(*args, **kwargs):
                    searching.set()
                    return original_analysis(*args, **kwargs)

                engine.analysis = tracked_analysis
                helpers.append(engine)
                return engine

            game = play_human.HumanGame(REAL_BINARY, Path(folder) / "games",
                                        hint_engine_factory=hint_engine)
            try:
                game.new_game("white", 100, game.state()["revision"])
                before = game.state()
                pgn = game.pgn()
                game.set_hint_enabled(True)
                hint = wait_for(lambda: game.hint() if game.hint()["status"] == "ready" else None, 8)
                self.assertIn(chess.Move.from_uci(hint["move"]), game.board.legal_moves)
                self.assertEqual(len(hint["candidates"]), 3)
                self.assertEqual(len({row["depth"] for row in hint["candidates"]}), 1)
                self.assertEqual(hint["move"], hint["candidates"][0]["move"])
                self.assertEqual(hint["fen"], before["fen"])
                self.assertEqual(game.state()["revision"], before["revision"])
                self.assertEqual(game.pgn(), pgn)
                game.set_hint_enabled(False)
                game.new_game("black", 100, game.state()["revision"])
                game.engine_move(game.state()["revision"])
                game.set_hint_enabled(True)
                black = wait_for(lambda: game.hint() if game.hint()["status"] == "ready" else None, 8)
                self.assertIn(chess.Move.from_uci(black["move"]), game.board.legal_moves)
                self.assertEqual(game.board.turn, chess.BLACK)
                game.set_hint_enabled(False)
                self.assert_empty_result(game.hint())
                for engine in helpers:
                    self.assertIsNotNone(engine.returncode.result(timeout=2))
                # A genuine long helper search is also cancelled/reaped on
                # OFF, without applying advice or corrupting the live board.
                game.new_game("white", 5000, game.state()["revision"])
                searching.clear()
                before = game.state()
                game.set_hint_enabled(True)
                self.assertTrue(searching.wait(3))
                off = game.set_hint_enabled(False)
                self.assert_empty_result(off)
                self.assertIsNotNone(helpers[-1].returncode.result(timeout=2))
                self.assertEqual(game.state()["fen"], before["fen"])
                self.assertEqual(game.state()["revision"], before["revision"])
            finally:
                game.close()

    def assert_empty_result(self, snapshot):
        self.assertEqual(snapshot["status"], "disabled")
        self.assertIsNone(snapshot["move"])
        self.assertEqual(snapshot["candidates"], [])


if __name__ == "__main__":
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        REAL_BINARY = Path(sys.argv.pop(1)).resolve(strict=True)
        # unittest's class decorator is evaluated before command-line parsing.
        RealHintSmoke.__unittest_skip__ = False
    unittest.main()
