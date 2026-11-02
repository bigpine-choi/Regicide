"""Live practice time changes, including concurrent requests; fake UCI only."""

from concurrent.futures import ThreadPoolExecutor
import json
import socket
import threading
import time
import unittest

import human_play_tests as baseline


host = baseline.play_human


class ThinkTimeTests(baseline.HumanPlayTests):
    def setting(self, ms, state=None):
        state = state or self.game.state()
        return self.game.set_think_time(ms, state["game_id"], state["engine"]["settings_revision"])

    def saved(self):
        return json.loads((self.game.save_dir / f"{self.game.game_id}.json").read_text(encoding="utf-8"))

    def files(self):
        return {path.name: path.read_bytes() for path in self.game.save_dir.iterdir()}

    def test_setting_persists_without_changing_game_position_or_search_token(self):
        self.new(ms=1000)
        self.human("e2e4")
        self.engine("e7e5")
        before, token = self.game.state(), self.game._game_token
        changed = self.setting(3000)
        for name in ("revision", "game_id", "board", "turn", "human_color", "moves", "result", "can_undo"):
            self.assertEqual(changed[name], before[name], name)
        self.assertIs(self.game._game_token, token)
        self.assertEqual(changed["engine"]["movetime_ms"], 3000)
        self.assertEqual(changed["engine"]["settings_revision"], 1)
        record = self.saved()
        self.assertEqual(record["state"]["engine"]["movetime_ms"], 3000)
        self.assertEqual(record["time_settings_events"][-1]["from_ms"], 1000)
        self.assertEqual(record["time_settings_events"][-1]["after_ply"], 2)
        self.human("g1f3")
        self.engine("b8c6")
        calls = self.factory.instances[-1].calls
        self.assertEqual([call[1].time for call in calls], [1.0, 3.0])
        self.assertIs(calls[0][2], calls[1][2])
        self.assertEqual(self.saved()["engine_movetimes_ms"], {"2": 1000, "4": 3000})
        for pgn in (self.game.pgn(), host.GameArchive(self.game.save_dir).get_pgn(self.game.game_id)):
            parsed, _ = baseline.read_pgn(pgn)
            nodes = list(parsed.mainline())
            self.assertEqual(parsed.headers["EngineMoveTimeMS"], "3000")
            self.assertIn("1000 ms", nodes[1].comment)
            self.assertIn("3000 ms", nodes[3].comment)
            self.assertEqual(nodes[0].comment, "")

    def test_invalid_and_stale_settings_preserve_board_files_and_settings(self):
        self.new()
        for value in (None, True, False, "3000", 3000.0, 99, 5001, float("inf"), float("nan")):
            before, files = self.game.state(), self.files()
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.setting(value)
            self.assertEqual(self.game.state(), before)
            self.assertEqual(self.files(), files)
        old = self.game.state()
        self.setting(3000)
        for game_id, revision in ((old["game_id"], 0), ("old-game", 1), (old["game_id"], True)):
            before, files = self.game.state(), self.files()
            with self.assertRaises(host.RevisionError):
                self.game.set_think_time(5000, game_id, revision)
            self.assertEqual(self.game.state(), before)
            self.assertEqual(self.files(), files)
        self.new()
        with self.assertRaises(host.RevisionError):
            self.game.set_think_time(5000, old["game_id"], 0)

    def test_same_value_is_idempotent(self):
        self.new(ms=3000)
        before, files = self.game.state(), self.files()
        self.assertEqual(self.setting(3000), before)
        self.assertEqual(self.files(), files)

    def blocked_search(self):
        entered, release = threading.Event(), threading.Event()
        peer = self.factory.instances[-1]
        original = peer.play

        def delayed(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test search release timed out")
            return original(*args, **kwargs)

        peer.play = delayed
        return entered, release

    def test_change_during_search_is_saved_immediately_and_applies_to_next_search(self):
        self.new(ms=5000)
        self.human("e2e4")
        entered, release = self.blocked_search()
        with ThreadPoolExecutor(max_workers=2) as workers:
            running = workers.submit(self.engine, "e7e5")
            try:
                self.assertTrue(entered.wait(2))
                before = self.game.state()
                self.assertTrue(before["engine_thinking"])
                self.assertEqual(before["engine"]["active_movetime_ms"], 5000)
                changed = workers.submit(self.setting, 100).result(timeout=1)
                self.assertFalse(running.done())
                self.assertEqual(changed["revision"], before["revision"])
                self.assertEqual(changed["engine"]["active_movetime_ms"], 5000)
                self.assertEqual(self.saved()["state"]["engine"]["movetime_ms"], 100)
                self.assertEqual(self.saved()["time_settings_events"][-1]["active_search_ms"], 5000)
                for operation in (lambda: self.new("black"), lambda: self.game.undo(before["revision"]),
                                  lambda: self.game.resign(before["revision"]),
                                  lambda: self.game.begin_handicap(1, before["revision"]),
                                  lambda: self.game.forced_move("a7a6", before["revision"]),
                                  lambda: self.game.cancel_handicap(before["revision"]),
                                  lambda: self.human("d2d4")):
                    with self.assertRaises(ValueError):
                        operation()
                self.assertEqual(self.game.state()["board"], before["board"])
            finally:
                release.set()
            done = running.result(timeout=2)
        self.assertFalse(done["engine_thinking"])
        self.assertIsNone(done["engine"]["active_movetime_ms"])
        self.human("g1f3")
        self.engine("b8c6")
        self.assertEqual([call[1].time for call in self.factory.instances[-1].calls], [5.0, .1])

    def test_close_waits_for_search_without_deadlock_and_rejects_late_setting(self):
        self.new()
        self.human("e2e4")
        entered, release = self.blocked_search()
        close_entered = threading.Event()

        def close():
            close_entered.set()
            self.game.close()

        with ThreadPoolExecutor(max_workers=2) as workers:
            search = workers.submit(self.engine, "e7e5")
            try:
                self.assertTrue(entered.wait(2))
                closing = workers.submit(close)
                self.assertTrue(close_entered.wait(2))
                self.assertFalse(closing.done())
            finally:
                release.set()
            search.result(timeout=2)
            closing.result(timeout=2)
        self.assertTrue(self.factory.instances[-1].closed)
        # Closing preserves the submitted human move, never a later engine reply.
        self.assertEqual(self.saved()["uci_moves"], ["e2e4"])
        self.assertEqual(self.saved()["engine_movetimes_ms"], {})
        with self.assertRaises(host.RevisionError):
            self.setting(3000)

    def test_closed_game_rejects_queued_requests_without_restarting_engine(self):
        self.new()
        self.human("e2e4")
        revision = self.game.revision
        self.game.close()
        instances = len(self.factory.instances)
        before = self.game.state()
        self.assertFalse(before["engine_to_move"])
        for operation in (lambda: self.game.engine_move(revision),
                          lambda: self.game.new_game("black", 100, revision),
                          lambda: self.game.undo(revision), lambda: self.game.resign(revision)):
            with self.assertRaises(ValueError):
                operation()
        self.assertEqual(len(self.factory.instances), instances)
        self.assertEqual(self.game.state(), before)

    def test_undo_and_handicap_keep_time_setting_and_correct_move_annotations(self):
        self.new(ms=1000)
        self.human("e2e4")
        self.engine("e7e5")
        self.setting(3000)
        self.human("g1f3")
        self.engine("b8c6")
        before_files = self.files()
        self.game.begin_handicap(2, self.game.revision)
        with self.assertRaises(ValueError):
            self.setting(5000)
        self.assertEqual(self.files(), before_files)
        self.game.cancel_handicap(self.game.revision)
        self.assertEqual(self.saved()["engine_movetimes_ms"], {"2": 1000, "4": 3000})
        self.game.begin_handicap(2, self.game.revision)
        self.game.forced_move("a7a6", self.game.revision)
        self.assertEqual(self.saved()["engine_movetimes_ms"], {})
        self.assertEqual(self.game.state()["engine"]["movetime_ms"], 3000)
        self.human("g1f3")
        self.engine("b8c6")
        self.game.undo(self.game.revision)
        self.assertEqual(self.saved()["engine_movetimes_ms"], {})
        self.assertEqual(self.game.state()["engine"]["movetime_ms"], 3000)
        parsed, _ = baseline.read_pgn(self.game.pgn())
        self.assertIn("handicap", list(parsed.mainline())[1].comment)


class ThinkTimeHTTPTests(unittest.TestCase):
    setUp = baseline.HumanHTTPTests.setUp
    stop_server = baseline.HumanHTTPTests.stop_server
    request = baseline.HumanHTTPTests.request

    def payload(self, ms):
        state = self.game.state()
        return {"game_id": state["game_id"], "settings_revision": state["engine"]["settings_revision"],
                "movetime_ms": ms}

    def test_api_validates_auth_session_revision_and_value(self):
        payload = self.payload(3000)
        before = self.game.state()
        self.assertEqual(self.request("/api/think-time", payload, token=False)[0], 403)
        self.assertEqual(self.game.state(), before)
        for replacement in ({"movetime_ms": True}, {"movetime_ms": 7000},
                            {"game_id": "old-session"}, {"settings_revision": "0"}):
            code, _, _ = self.request("/api/think-time", {**payload, **replacement})
            self.assertIn(code, (400, 409))
            self.assertEqual(self.game.state(), before)
        code, _, data = self.request("/api/think-time", payload)
        self.assertEqual(code, 200)
        changed = json.loads(data)
        self.assertEqual(changed["engine"]["movetime_ms"], 3000)
        self.assertEqual(changed["revision"], before["revision"])
        self.assertEqual(self.request("/api/think-time", payload)[0], 409)

    def test_forbidden_post_bodies_receive_403_without_mutating_the_game(self):
        before = self.game.state()
        payload = self.payload(3000)
        for _ in range(20):
            self.assertEqual(self.request("/api/think-time", payload,
                                         headers={"Host": "attacker.example"})[0], 403)
        # A peer that promises a body and stops sending cannot hold the
        # unauthorized request handler indefinitely.
        with socket.create_connection(self.server.server_address, timeout=2) as client:
            client.sendall(b"POST /api/think-time HTTP/1.0\r\nHost: attacker.example\r\n"
                           b"Content-Type: application/json\r\nContent-Length: 20\r\n\r\nx")
            chunks = []
            while data := client.recv(4096):
                chunks.append(data)
            response = b"".join(chunks)
        self.assertIn(b"403", response.split(b"\r\n", 1)[0])
        self.assertEqual(self.game.state(), before)

    def test_http_setting_and_state_respond_while_engine_request_is_pending(self):
        self.game.human_move("e2e4", self.game.revision)
        entered, release = threading.Event(), threading.Event()
        peer = self.factory.instances[-1]
        original = peer.play

        def delayed(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test release timed out")
            return original(*args, **kwargs)

        peer.play = delayed
        with ThreadPoolExecutor(max_workers=2) as workers:
            search = workers.submit(self.request, "/api/engine", {"revision": self.game.revision})
            try:
                self.assertTrue(entered.wait(2))
                code, _, data = workers.submit(self.request, "/api/think-time", self.payload(5000)).result(timeout=2)
                self.assertEqual(code, 200)
                self.assertTrue(json.loads(data)["engine_thinking"])
                self.assertFalse(search.done())
                code, _, data = self.request("/api/state")
                self.assertEqual(code, 200)
                self.assertEqual(json.loads(data)["engine"]["movetime_ms"], 5000)
                self.assertEqual(self.request("/api/shutdown", {"revision": self.game.revision})[0], 400)
            finally:
                release.set()
            self.assertEqual(search.result(timeout=2)[0], 200)

    def test_forbidden_slow_body_has_a_total_wait_deadline(self):
        before = self.game.state()
        stop = threading.Event()
        with socket.create_connection(self.server.server_address, timeout=2) as client:
            def stream():
                for _ in range(20):
                    if stop.wait(.04):
                        return
                    try:
                        client.sendall(b"x")
                    except OSError:
                        return

            writer = threading.Thread(target=stream, daemon=True)
            start = time.monotonic()
            client.sendall(b"POST /api/think-time HTTP/1.0\r\nHost: attacker.example\r\n"
                           b"Content-Type: application/json\r\nContent-Length: 20\r\n\r\n")
            writer.start()
            try:
                chunks = []
                try:
                    while data := client.recv(4096):
                        chunks.append(data)
                except ConnectionResetError:
                    pass  # An incomplete attacker body need not get a graceful TCP close.
                self.assertIn(b"403", b"".join(chunks).split(b"\r\n", 1)[0])
                self.assertLess(time.monotonic() - start, .5,
                                "A slow body must not extend the drain timeout per received byte")
            finally:
                stop.set()
                writer.join(timeout=2)
        self.assertFalse(writer.is_alive())
        self.assertEqual(self.game.state(), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
