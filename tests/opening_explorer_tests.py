"""Public-statistics boundary, credential isolation, and async query ordering.

All explorer requests use fixtures. No real API token or external call is used.
"""

import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import chess
import lichess_credentials as credentials
import opening_explorer as explorer


TOKEN = "fixture-token-only-not-a-real-credential"


def payload(uci="e2e4"):
    return {"white": 50, "draws": 20, "black": 30,
            "moves": [{"uci": uci, "san": "untrusted", "white": 30, "draws": 10,
                       "black": 10, "averageRating": 2300}],
            "topGames": [{"id": "aBcd1234", "white": {"name": "Fixture White", "rating": 2400},
                          "black": {"name": "Fixture Black", "rating": 2350},
                          "winner": None, "year": 2025, "url": "https://untrusted.invalid"}]}


class MemoryCredentials:
    def __init__(self, token=TOKEN):
        self.value = token

    def token(self):
        return self.value

    def set(self, token, remember=True):
        self.value = credentials.validated_token(token)
        return "fixture credential applied"


class Response:
    def __init__(self, data, headers=None):
        self.body = data if isinstance(data, bytes) else json.dumps(data).encode("utf-8")
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size):
        return self.body[:size]


class OpeningExplorerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "cache"
        self.peers = []

    def peer(self, opener=None, token=TOKEN, **kwargs):
        peer = explorer.OpeningExplorer(self.directory, MemoryCredentials(token),
                                        opener or (lambda request, timeout: Response(payload())),
                                        min_interval=0, **kwargs)
        self.peers.append(peer)
        self.addCleanup(peer.close)
        return peer

    def complete(self, peer, expected="ready"):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = peer.snapshot()
            if result["status"] != "loading":
                self.assertEqual(result["status"], expected, result)
                return result
            time.sleep(0.005)
        self.fail("fixture query did not complete")

    def test_no_credential_stays_offline_and_constructor_is_lazy(self):
        calls = []
        peer = self.peer(lambda *args, **kwargs: calls.append(1), token="")
        self.assertIsNone(peer._worker)
        self.assertEqual(peer.snapshot()["status"], "idle")
        result = peer.query(chess.STARTING_FEN)
        self.assertEqual(result["status"], "auth_required")
        self.assertFalse(result["configured"])
        self.assertEqual(calls, [])
        self.assertIsNone(peer._worker)

    def test_request_is_fixed_host_with_bearer_and_bucket_filters(self):
        calls = []
        def open_fixture(request, timeout):
            calls.append((request, timeout))
            return Response(payload())
        peer = self.peer(open_fixture)
        peer.query(chess.STARTING_FEN, "lichess", [2500, 1600, 1600], ["rapid", "classical"])
        result = self.complete(peer)
        request, timeout = calls[0]
        parts = urllib.parse.urlsplit(request.full_url)
        query = urllib.parse.parse_qs(parts.query)
        self.assertEqual((parts.scheme, parts.netloc, parts.path), ("https", "explorer.lichess.org", "/lichess"))
        self.assertEqual(request.get_header("Authorization"), "Bearer " + TOKEN)
        self.assertNotIn(TOKEN, request.full_url)
        self.assertEqual(query["ratings"], ["1600,2500"])
        self.assertEqual(query["speeds"], ["classical,rapid"])
        self.assertEqual(result["ratings"], [1600, 2500])
        self.assertLessEqual(timeout, 30)
        self.assertNotIn(TOKEN, json.dumps(result))

    def test_totals_and_probabilities_use_full_position_denominator(self):
        peer = self.peer()
        peer.query(chess.STARTING_FEN)
        result = self.complete(peer)
        self.assertEqual(result["total"], 100)
        row = result["moves"][0]
        self.assertEqual((row["count"], row["play_pct"]), (50, 50.0))
        self.assertEqual((row["white_pct"], row["draw_pct"], row["black_pct"]), (60, 20, 20))
        self.assertEqual(row["san"], "e4")
        board = chess.Board()
        board.push_uci("e2e4")
        self.assertEqual(row["fen_after"], board.fen(en_passant="fen"))
        result["moves"].clear()
        self.assertEqual(len(peer.snapshot()["moves"]), 1)

    def test_bad_move_rows_and_game_references_are_not_presented(self):
        data = payload()
        data["moves"] += [{"uci": "e2e5", "white": 1, "draws": 0, "black": 0},
                          {"uci": "e2e4", "white": 1, "draws": 0, "black": 0},
                          {"uci": "d2d4", "white": -1, "draws": 0, "black": 0},
                          {"uci": "c2c4", "white": True, "draws": 0, "black": 0},
                          {"uci": "g1f3", "white": 50, "draws": 20, "black": 30}]
        bad_game = copy.deepcopy(data["topGames"][0])
        bad_game["id"] = "../../bad"
        data["topGames"].append(bad_game)
        bad_game = copy.deepcopy(data["topGames"][0])
        bad_game["id"], bad_game["white"]["name"] = "zBcd1234", "control\nname"
        data["topGames"].append(bad_game)
        peer = self.peer(lambda *args, **kwargs: Response(data))
        peer.query(chess.STARTING_FEN)
        result = self.complete(peer)
        self.assertEqual([row["uci"] for row in result["moves"]], ["e2e4"])
        self.assertEqual(len(result["games"]), 1)
        self.assertEqual(result["games"][0]["url"], "https://lichess.org/import/master/aBcd1234/white")

    def test_lichess_game_reference_uses_different_trusted_route(self):
        peer = self.peer()
        peer.query(chess.STARTING_FEN, "lichess")
        self.assertEqual(self.complete(peer)["games"][0]["url"], "https://lichess.org/aBcd1234")

    def test_zero_games_is_a_ready_empty_result(self):
        peer = self.peer(lambda *args, **kwargs: Response({"white": 0, "draws": 0, "black": 0, "moves": []}))
        peer.query(chess.STARTING_FEN)
        result = self.complete(peer)
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["moves"], [])

    def test_invalid_query_cannot_start_network_work(self):
        calls = []
        peer = self.peer(lambda *args, **kwargs: calls.append(1))
        cases = [("not a fen", "masters", None, None),
                 ("8/8/8/8/8/8/8/8 w - - 0 1", "masters", None, None),
                 (chess.STARTING_FEN, "http://other.invalid", None, None),
                 (chess.STARTING_FEN, "lichess", [], None),
                 (chess.STARTING_FEN, "lichess", [True], None),
                 (chess.STARTING_FEN, "lichess", [1900], None),
                 (chess.STARTING_FEN, "lichess", [1600], ["hyperBullet"])]
        for arguments in cases:
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                peer.query(*arguments)
        self.assertEqual(calls, [])

    def test_equivalent_positions_share_cache_but_rebuild_child_counters(self):
        calls = []
        peer = self.peer(lambda *args, **kwargs: (calls.append(1), Response(payload()))[1])
        peer.query(chess.STARTING_FEN, "masters", [1600], ["rapid"])
        first = self.complete(peer)
        board = chess.Board()
        board.halfmove_clock, board.fullmove_number = 20, 19
        second = peer.query(board.fen())
        self.assertTrue(second["cached"])
        self.assertEqual(first["request_key"], second["request_key"])
        self.assertEqual(second["fen"], board.fen())
        board.push_uci("e2e4")
        self.assertEqual(second["moves"][0]["fen_after"], board.fen(en_passant="fen"))
        self.assertEqual(calls, [1])

    def test_filters_and_castling_rights_change_request_identity(self):
        normal = explorer._context(chess.STARTING_FEN, "lichess", [1600], ["rapid"])
        filtered = explorer._context(chess.STARTING_FEN, "lichess", [1800], ["rapid"])
        board = chess.Board()
        board.castling_rights = 0
        other_position = explorer._context(board.fen(), "lichess", [1600], ["rapid"])
        self.assertNotEqual(normal["request_key"], filtered["request_key"])
        self.assertNotEqual(normal["request_key"], other_position["request_key"])

    def test_legal_en_passant_identity_and_unusable_target(self):
        board = chess.Board()
        board.push_uci("e2e4")
        raw = explorer._context(board.fen(en_passant="fen"), "masters", None, None)
        legal = explorer._context(board.fen(en_passant="legal"), "masters", None, None)
        self.assertEqual(raw["request_key"], legal["request_key"])
        board = chess.Board("4k3/8/8/3pP3/8/8/8/4K3 w - d6 0 10")
        with_ep = explorer._context(board.fen(), "masters", None, None)
        board.ep_square = None
        without_ep = explorer._context(board.fen(), "masters", None, None)
        self.assertNotEqual(with_ep["request_key"], without_ep["request_key"])

    def test_disk_cache_is_public_only_and_survives_new_service(self):
        peer = self.peer()
        peer.query(chess.STARTING_FEN)
        first = self.complete(peer)
        deadline = time.monotonic() + 2
        while not list(self.directory.glob("*.json")) and time.monotonic() < deadline:
            time.sleep(0.005)
        files = list(self.directory.glob("*.json"))
        self.assertEqual(len(files), 1)
        stored = files[0].read_text(encoding="utf-8")
        self.assertNotIn(TOKEN, stored)
        self.assertNotIn("Authorization", stored)
        next_peer = self.peer(lambda *args, **kwargs: self.fail("disk cache missed"), token="")
        second = next_peer.query(chess.STARTING_FEN)
        self.assertEqual(second["status"], "ready")
        self.assertTrue(second["cached"])
        self.assertFalse(second["configured"])
        self.assertEqual(second["moves"], first["moves"])

    def test_expired_and_malformed_cache_are_ignored(self):
        context = explorer._context(chess.STARTING_FEN, "masters", None, None)
        self.directory.mkdir()
        target = self.directory / (context["request_key"] + ".json")
        for document in ["not json", "[]", "[" * 2000, json.dumps({"version": explorer.CACHE_VERSION, "request_key": context["request_key"],
                                                "timestamp": time.time() - explorer.CACHE_SECONDS - 5,
                                                "payload": payload()})]:
            target.write_text(document, encoding="utf-8")
            peer = self.peer(token="")
            self.assertEqual(peer.query(chess.STARTING_FEN)["status"], "auth_required")

    def test_single_worker_coalesces_pending_and_never_publishes_old_position(self):
        entered, release = threading.Event(), threading.Event()
        calls, active, maximum = [], 0, 0
        def open_fixture(request, timeout):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            fen = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)["fen"][0]
            calls.append(fen)
            if len(calls) == 1:
                entered.set()
                release.wait(2)
            active -= 1
            return Response(payload() if len(calls) == 1 else payload("b1c3"))
        peer = self.peer(open_fixture)
        self.addCleanup(release.set)
        peer.query(chess.STARTING_FEN)
        self.assertTrue(entered.wait(1))
        first_branch = chess.Board()
        first_branch.push_uci("e2e4")
        peer.query(first_branch.fen())
        final_branch = chess.Board()
        final_branch.push_uci("d2d4")
        final_branch.push_uci("d7d5")
        final = peer.query(final_branch.fen())
        release.set()
        result = self.complete(peer)
        self.assertEqual(result["request_key"], final["request_key"])
        self.assertEqual(result["fen"], final_branch.fen())
        self.assertEqual(result["moves"][0]["uci"], "b1c3")
        self.assertEqual(len(calls), 2)
        self.assertEqual(maximum, 1)

    def test_close_discards_inflight_result(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def open_fixture(*args, **kwargs):
            entered.set()
            release.wait(2)
            finished.set()
            return Response(payload())
        peer = self.peer(open_fixture)
        self.addCleanup(release.set)
        peer.query(chess.STARTING_FEN)
        self.assertTrue(entered.wait(1))
        peer.close()
        release.set()
        self.assertTrue(finished.wait(1))
        peer._worker.join(1)
        self.assertEqual(peer.snapshot()["status"], "idle")
        self.assertEqual(list(self.directory.glob("*.json")), [])
        with self.assertRaises(ValueError):
            peer.query(chess.STARTING_FEN)

    def test_http429_stops_all_sources_for_at_least_sixty_seconds(self):
        calls, now = [], [100.0]
        def open_fixture(request, timeout):
            calls.append(request.full_url)
            if len(calls) == 1:
                raise urllib.error.HTTPError(request.full_url, 429, "fixture", {"Retry-After": "2"}, io.BytesIO())
            return Response(payload())
        peer = self.peer(open_fixture, monotonic=lambda: now[0])
        peer.query(chess.STARTING_FEN)
        result = self.complete(peer, "rate_limited")
        self.assertEqual(result["retry_after"], 60)
        self.assertEqual(peer.query(chess.STARTING_FEN, "lichess")["status"], "rate_limited")
        self.assertEqual(len(calls), 1)
        now[0] += 61
        peer.query(chess.STARTING_FEN, "lichess")
        self.complete(peer)
        self.assertEqual(len(calls), 2)

    def test_auth_and_network_errors_never_reflect_diagnostics(self):
        for code, status in [(401, "auth_required"), (403, "auth_required"), (500, "error"), (302, "error")]:
            def rejected(request, timeout, code=code):
                raise urllib.error.HTTPError("https://other.invalid/" + TOKEN, code, TOKEN, {}, io.BytesIO(TOKEN.encode()))
            peer = self.peer(rejected)
            peer.query(chess.STARTING_FEN)
            self.assertNotIn(TOKEN, json.dumps(self.complete(peer, status)))
        peer = self.peer(lambda *args, **kwargs: (_ for _ in ()).throw(urllib.error.URLError(TOKEN)))
        peer.query(chess.STARTING_FEN)
        self.assertNotIn(TOKEN, json.dumps(self.complete(peer, "error")))

    def test_invalid_payload_size_counts_and_json_fail_closed(self):
        cases = [Response(b"not-json"), Response(b"[" * 2000), Response(b"x" * (explorer.MAX_BODY_BYTES + 1)),
                 Response(payload(), {"Content-Length": str(explorer.MAX_BODY_BYTES + 1)}),
                 Response({"white": True, "draws": 0, "black": 0, "moves": []}),
                 Response({"white": -1, "draws": 0, "black": 0, "moves": []}),
                 Response({"white": 1, "draws": 0, "black": 0, "moves": {}})]
        for response in cases:
            peer = self.peer(lambda *args, response=response, **kwargs: response)
            peer.query(chess.STARTING_FEN)
            result = self.complete(peer, "error")
            self.assertEqual(result["moves"], [])

    def test_redirect_handler_never_forwards_authorization(self):
        self.assertIsNone(explorer._NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.invalid/"))

    def test_memory_and_disk_cache_limits_are_bounded(self):
        peer = self.peer()
        context = explorer._context(chess.STARTING_FEN, "masters", None, None)
        data = explorer._normalise(payload(), context)
        entry = {"data": data, "timestamp": time.time(), "fetched_at": "fixture"}
        with patch.object(explorer, "MEMORY_ENTRIES", 2), patch.object(explorer, "DISK_ENTRIES", 2):
            for number in range(5):
                key = str(number).zfill(64)
                peer._remember_locked(key, entry)
                peer._save_cache({**context, "request_key": key}, entry)
            self.assertEqual(len(peer._cache), 2)
            self.assertEqual(len(list(self.directory.glob("*.json"))), 2)

    def test_token_change_invalidates_pending_and_requires_new_query(self):
        peer = self.peer(token="")
        peer.query(chess.STARTING_FEN)
        result = peer.set_token(TOKEN, remember=False)
        self.assertTrue(result["configured"])
        self.assertEqual(result["status"], "idle")
        self.assertNotIn(TOKEN, json.dumps(result))
        peer.query(chess.STARTING_FEN)
        self.complete(peer)


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "credentials" / "lichess.dpapi"
        environment = patch.dict(os.environ, {"REGICIDE_LICHESS_TOKEN": ""})
        environment.start()
        self.addCleanup(environment.stop)

    def test_environment_precedes_memory_and_memory_only_does_not_persist(self):
        store = credentials.LichessCredentials(self.path)
        store.set(TOKEN, remember=False)
        self.assertEqual(store.token(), TOKEN)
        self.assertFalse(self.path.exists())
        with patch.dict(os.environ, {"REGICIDE_LICHESS_TOKEN": "environment-fixture"}):
            self.assertEqual(store.token(), "environment-fixture")
            self.assertIn("환경변수", store.set("replacement-fixture", remember=False))
            self.assertEqual(store.token(), "environment-fixture")
        self.assertEqual(credentials.LichessCredentials(self.path).token(), "")

    def test_invalid_header_tokens_are_rejected(self):
        store = credentials.LichessCredentials(self.path)
        for value in ["bad\r\nAuthorization: injected", "token with space", "x" * 4097, None]:
            with self.assertRaises(ValueError):
                store.set(value)

    def test_dpapi_or_memory_fallback_never_writes_plaintext(self):
        store = credentials.LichessCredentials(self.path)
        store.set(TOKEN, remember=True)
        if os.name == "nt":
            self.assertTrue(self.path.exists())
            self.assertNotIn(TOKEN.encode(), self.path.read_bytes())
            self.assertEqual(credentials.LichessCredentials(self.path).token(), TOKEN)
            store.set("new-fixture", remember=False)
            self.assertFalse(self.path.exists())
            self.assertEqual(credentials.LichessCredentials(self.path).token(), "")
        else:
            self.assertFalse(self.path.exists())
            self.assertEqual(store.token(), TOKEN)

    def test_forget_clears_stored_token(self):
        store = credentials.LichessCredentials(self.path)
        store.set(TOKEN, remember=True)
        store.set("", remember=True)
        self.assertEqual(store.token(), "")
        self.assertFalse(self.path.exists())

    def test_protection_failure_keeps_memory_without_plain_file(self):
        store = credentials.LichessCredentials(self.path)
        with patch.object(credentials, "_dpapi", side_effect=OSError("fixture")):
            message = store.set(TOKEN, remember=True)
        self.assertIn("이번 실행", message)
        self.assertEqual(store.token(), TOKEN)
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
