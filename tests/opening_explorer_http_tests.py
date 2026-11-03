"""Authenticated explorer routes stay separate from game state and secrets."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

import chess

import human_play_tests as baseline


class ExplorerPeer:
    def __init__(self):
        self.state = {"status": "idle", "configured": False, "moves": [], "games": []}
        self.queries = []
        self.closed = False
        self.failure = None

    def snapshot(self):
        return dict(self.state)

    def query(self, fen, source, ratings, speeds):
        if self.failure:
            raise self.failure
        if not isinstance(fen, str):
            raise ValueError("위치가 필요합니다.")
        chess.Board(fen)
        self.queries.append((fen, source, ratings, speeds))
        self.state.update(status="loading", fen=fen, source=source)
        return self.snapshot()

    def set_token(self, token, remember):
        if self.failure:
            raise self.failure
        if not isinstance(token, str) or type(remember) is not bool:
            raise ValueError("토큰 설정을 확인해 주세요.")
        self.state.update(configured=bool(token), status="idle")
        return self.snapshot()

    def close(self):
        self.closed = True


class ExplorerHttpTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="regicide-explorer-http-")
        root = Path(self.folder.name)
        binary = root / "fake.exe"
        binary.write_bytes(b"not executed")
        self.game = baseline.play_human.HumanGame(
            binary, root / "games", baseline.FakeFactory(), opening_variety_enabled=False)
        self.peer = ExplorerPeer()
        self.server = baseline.play_human.HumanServer(self.game, explorer=self.peer)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.folder.cleanup)
        self.addCleanup(self.game.close)
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, payload=None, *, token=True, origin=None, raw=None, path="/api/opening-explorer"):
        headers = {"Origin": origin or self.server.origin}
        if token:
            headers["X-Regicide-Token"] = self.server.token
        if payload is not None or raw is not None:
            headers["Content-Type"] = "application/json"
        body = raw if raw is not None else json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(self.server.origin + path, data=body, headers=headers)
        try:
            response = urllib.request.urlopen(request, timeout=3)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            content = response.read()
            return response.status, json.loads(content) if "application/json" in response.headers.get("Content-Type", "") else content

    def test_authentication_and_same_origin_required(self):
        self.assertEqual(self.request(token=False)[0], 403)
        self.assertEqual(self.request({"action": "query", "fen": chess.STARTING_FEN}, token=False)[0], 403)
        self.assertEqual(self.request(origin="https://example.com")[0], 403)
        self.assertEqual(self.peer.queries, [])

    def test_query_and_poll_do_not_change_game_analysis_or_durable_files(self):
        before = self.game.state()
        analysis = self.game.analysis.snapshot()
        files = {p.name: p.read_bytes() for p in self.game.save_dir.glob("*.*")}
        payload = {"action": "query", "fen": chess.STARTING_FEN, "source": "lichess",
                   "ratings": [1600, 1800], "speeds": ["rapid"]}
        code, state = self.request(payload)
        self.assertEqual((code, state["status"]), (200, "loading"))
        self.assertEqual(self.peer.queries, [(chess.STARTING_FEN, "lichess", [1600, 1800], ["rapid"])])
        self.assertEqual(self.request()[1], state)
        self.assertEqual(self.game.state(), before)
        self.assertEqual(self.game.analysis.snapshot(), analysis)
        self.assertEqual(files, {p.name: p.read_bytes() for p in self.game.save_dir.glob("*.*")})

    def test_token_response_never_contains_token(self):
        secret = "lip_test_example_only_do_not_use"
        code, state = self.request({"action": "token", "token": secret, "remember": False})
        self.assertEqual(code, 200)
        self.assertTrue(state["configured"])
        self.assertNotIn(secret, json.dumps(state))
        self.assertNotIn(secret, json.dumps(self.request()[1]))

    def test_invalid_requests_are_isolated_and_cannot_supply_remote_url(self):
        before = self.game.state()
        for payload in ({"action": "unknown"}, {"action": "query"},
                        {"action": "query", "fen": chess.STARTING_FEN, "url": "https://example.com"},
                        {"action": "token", "token": "test", "remember": "yes"}):
            with self.subTest(payload=payload):
                code, state = self.request(payload)
                self.assertEqual(code, 400)
                self.assertIn("explorer", state)
                self.assertNotIn("state", state)
        for raw in (b"{", b"[]", b"x" * 4097):
            self.assertEqual(self.request(raw=raw)[0], 400)
        self.assertEqual(self.game.state(), before)

    def test_transport_failure_cannot_log_or_return_a_secret(self):
        secret = "lip_example_failure_secret"
        self.peer.failure = RuntimeError(secret)
        stream = io.StringIO()
        with contextlib.redirect_stderr(stream):
            code, state = self.request({"action": "token", "token": secret, "remember": False})
        self.assertEqual(code, 500)
        self.assertNotIn(secret, json.dumps(state))
        self.assertNotIn(secret, stream.getvalue())
        self.assertNotIn("state", state)

    def test_browser_module_is_served_and_close_releases_worker(self):
        code, data = self.request(path="/explorer.js")
        self.assertEqual(code, 200)
        self.assertIn(b"RegicideExplorer", data)
        code, sidebar = self.request(path="/sidebar_tabs.js")
        self.assertEqual(code, 200)
        self.assertIn(b"RegicideSidebar", sidebar)
        self.server.shutdown()
        self.server.server_close()
        self.assertTrue(self.peer.closed)


if __name__ == "__main__":
    unittest.main()
