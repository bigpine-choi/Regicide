"""Authenticated analysis/statistics API and isolation from the live engine."""

from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

import chess
import chess.pgn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from human_play_tests import FakeFactory
import play_human


class SlowExplorer:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def query(self, fen, source):
        self.entered.set()
        self.release.wait(3)
        return {"fen": fen, "source": source, "status": "empty", "total": 0,
                "moves": [], "message": "표본 없음"}


class OpeningStatsHTTPTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="regicide-analysis-http-")
        self.root = Path(self.folder.name)
        binary = self.root / "test-engine.exe"
        binary.write_bytes(b"fake engine, never executed")
        self.factory = FakeFactory()
        self.game = play_human.HumanGame(binary, self.root / "games", self.factory)
        self.server = play_human.HumanServer(self.game)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.folder.cleanup)
        self.addCleanup(self.game.close)
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.assertFalse(self.thread.is_alive())

    def request(self, path, data=None, *, token=True, headers=None, body=None, timeout=3):
        actual_headers = {"X-Regicide-Token": self.server.token} if token else {}
        if data is not None:
            body = json.dumps(data).encode()
        if body is not None:
            actual_headers["Content-Type"] = "application/json"
        actual_headers.update(headers or {})
        req = urllib.request.Request(self.server.origin + path, body, actual_headers)
        try:
            response = urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            payload = response.read().decode()
            if "application/json" in response.headers.get("Content-Type", ""):
                payload = json.loads(payload)
            return response.status, response.headers, payload

    def analysis(self):
        code, _, state = self.request("/api/analysis")
        self.assertEqual(code, 200)
        return state

    def move(self, uci):
        revision = self.analysis()["revision"]
        code, _, state = self.request("/api/analysis/move", {"move": uci, "revision": revision})
        self.assertEqual(code, 200)
        return state

    def test_routes_require_existing_local_token_host_and_origin(self):
        live = self.game.state()
        analysis = self.analysis()
        stats = "/api/opening-stats?" + urllib.parse.urlencode({"fen": chess.STARTING_FEN})
        for path in ("/api/analysis", "/api/analysis/pgn", stats):
            self.assertEqual(self.request(path, token=False)[0], 403)
            self.assertEqual(self.request(path, headers={"Origin": "https://attacker.invalid"})[0], 403)
            self.assertEqual(self.request(path, headers={"Host": "attacker.invalid"})[0], 403)
        self.assertEqual(self.request("/api/analysis/start", {}, token=False)[0], 403)
        self.assertEqual(self.analysis(), analysis)
        self.assertEqual(self.game.state(), live)

    def test_analysis_moves_and_stats_do_not_mutate_live_game_or_its_files(self):
        before = self.game.state()
        files = {p.name: p.read_bytes() for p in self.game.save_dir.iterdir() if p.is_file()}
        self.assertEqual(self.request("/api/analysis/start", {"fen": before["fen"]})[0], 200)
        self.move("e2e4")
        state = self.move("e7e5")
        self.assertEqual(state["turn"], "white")
        self.assertFalse(state["engine_to_move"])
        code, _, previous = self.request("/api/analysis/undo", {"revision": state["revision"]})
        self.assertEqual(code, 200)
        code, _, restored = self.request("/api/analysis/redo", {"revision": previous["revision"]})
        self.assertEqual(code, 200)
        self.assertEqual(restored["fen"], state["fen"])
        path = "/api/opening-stats?" + urllib.parse.urlencode({"fen": state["fen"], "source": "local"})
        code, _, stats = self.request(path)
        self.assertEqual(code, 200)
        self.assertEqual(stats["status"], "empty")
        self.assertEqual(self.game.state(), before)
        self.assertEqual(files, {p.name: p.read_bytes() for p in self.game.save_dir.iterdir() if p.is_file()})
        self.assertTrue(all(not peer.calls for peer in self.factory.instances))

    def test_stale_and_illegal_analysis_requests_return_analysis_position(self):
        before = self.analysis()
        for payload, expected in (({"move": "e2e4", "revision": before["revision"] - 1}, 409),
                                  ({"move": "e2e5", "revision": before["revision"]}, 400),
                                  ({"move": "e2e4", "revision": True}, 409)):
            code, _, error = self.request("/api/analysis/move", payload)
            self.assertEqual(code, expected)
            self.assertEqual(error["state"], before)
        self.assertEqual(self.request("/api/analysis/start", {"fen": "bad"})[0], 400)
        self.assertEqual(self.analysis(), before)
        self.assertEqual(self.request("/api/analysis/reset", {"revision": -1})[0], 409)
        self.assertEqual(self.analysis(), before)

    def test_analysis_pgn_retains_custom_root_and_black_move_number(self):
        fen = "7k/8/8/8/8/8/7p/5K2 b - - 4 28"
        code, _, state = self.request("/api/analysis/start", {"fen": fen})
        self.assertEqual(code, 200)
        self.assertEqual(state["root_fen"], fen)
        state = self.move("h2h1q")
        code, headers, text = self.request("/api/analysis/pgn")
        self.assertEqual(code, 200)
        self.assertIn("application/x-chess-pgn", headers["Content-Type"])
        self.assertIn("28...", text)
        game = chess.pgn.read_game(io.StringIO(text))
        self.assertFalse(game.errors)
        self.assertEqual(game.headers["Result"], "*")
        self.assertEqual(game.end().board().fen(en_passant="fen"), state["fen"])

    def test_stats_reject_bad_queries_before_calling_provider(self):
        class NeverExplorer:
            def query(self, *_args):
                raise AssertionError("invalid requests must not reach provider")
        self.server.opening_explorer = NeverExplorer()
        for query in ("", "fen=bad", "source=local", "fen=", "fen=bad&fen=bad",
                      "fen=bad&unknown=x", "fen=bad&source=local&source=masters",
                      urllib.parse.urlencode({"fen": chess.STARTING_FEN, "source": "custom"}),
                      "fen=" + "x" * 1100):
            self.assertEqual(self.request("/api/opening-stats?" + query)[0], 400)

    def test_stats_provider_failure_is_separate_from_analysis(self):
        class BrokenExplorer:
            def query(self, *_args):
                raise RuntimeError("private provider error")
        self.server.opening_explorer = BrokenExplorer()
        before = self.analysis()
        path = "/api/opening-stats?" + urllib.parse.urlencode({"fen": before["fen"]})
        code, _, error = self.request(path)
        self.assertEqual(code, 503)
        self.assertNotIn("private", json.dumps(error))
        self.assertEqual(self.move("e2e4")["turn"], "black")

    def test_slow_stats_lookup_does_not_block_live_state_or_analysis_moves(self):
        provider = SlowExplorer()
        self.server.opening_explorer = provider
        path = "/api/opening-stats?" + urllib.parse.urlencode({"fen": chess.STARTING_FEN})
        with ThreadPoolExecutor(max_workers=1) as pool:
            waiting = pool.submit(self.request, path)
            try:
                self.assertTrue(provider.entered.wait(1))
                started = time.monotonic()
                self.assertEqual(self.request("/api/state", timeout=1)[0], 200)
                self.assertEqual(self.move("e2e4")["turn"], "black")
                self.assertLess(time.monotonic() - started, 1)
                self.assertFalse(waiting.done())
            finally:
                provider.release.set()
            self.assertEqual(waiting.result(timeout=2)[0], 200)

    def test_malformed_analysis_payload_does_not_mutate_either_board(self):
        before = self.analysis()
        live = self.game.state()
        for body in (b"{", b"[]", b"null"):
            code, _, error = self.request("/api/analysis/start", body=body)
            self.assertEqual(code, 400)
            self.assertEqual(error["state"], before)
        self.assertEqual(self.analysis(), before)
        self.assertEqual(self.game.state(), live)


if __name__ == "__main__":
    unittest.main()
