"""Human-play model checks with a fake UCI peer and optional real-engine smoke.

Usage: python tests/human_play_tests.py [path/to/regicide]
python-chess is an independent legality/PGN oracle, never a move selector for
the real-engine smoke. Fake peers keep error and special-rule cases exact.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

import chess
import chess.engine
import chess.pgn


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("regicide_play_human", ROOT / "scripts" / "play_human.py")
play_human = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = play_human
SPEC.loader.exec_module(play_human)
REAL_BINARY = None


class FakeEngine:
    def __init__(self):
        self.id = {"name": "Regicide fake test peer", "author": "tests"}
        self.replies = []
        self.calls = []
        self.closed = False

    def play(self, board, limit, *, game=None, info=None):
        self.calls.append((board.copy(stack=True), limit, game, info))
        reply = self.replies.pop(0) if self.replies else next(iter(board.legal_moves))
        if isinstance(reply, BaseException):
            raise reply
        if isinstance(reply, chess.engine.PlayResult):
            return reply
        if isinstance(reply, str):
            reply = chess.Move.from_uci(reply)
        return chess.engine.PlayResult(reply, None, {"depth": 2, "nodes": 20})

    def quit(self):
        self.closed = True

    def close(self):
        self.closed = True


class FakeFactory:
    def __init__(self):
        self.instances = []

    def __call__(self, binary):
        engine = FakeEngine()
        self.instances.append(engine)
        return engine


def read_pgn(text):
    game = chess.pgn.read_game(io.StringIO(text))
    if game is None or game.errors:
        raise AssertionError(f"Invalid PGN: {text}")
    board = game.board()
    for move in game.mainline_moves():
        if move not in board.legal_moves:
            raise AssertionError(f"Illegal PGN move {move} in {board.fen()}")
        board.push(move)
    return game, board


class HumanPlayTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="regicide-human-tests-")
        self.root = Path(self.folder.name)
        self.binary = self.root / "regicide-test-peer.exe"
        self.binary.write_bytes(b"fake UCI peer; not executed")
        self.factory = FakeFactory()
        # These deterministic peers exercise live-game rules and failures;
        # opening selection has separate tests with scored candidate searches.
        self.game = play_human.HumanGame(self.binary, self.root / "games", self.factory,
                                        opening_variety_enabled=False)
        self.addCleanup(self.folder.cleanup)
        self.addCleanup(self.game.close)

    def new(self, color="white", ms=100):
        return self.game.new_game(color, ms, self.game.state()["revision"])

    def human(self, move):
        return self.game.human_move(move, self.game.state()["revision"])

    def engine(self, move=None):
        if move is not None:
            self.factory.instances[-1].replies.append(move)
        return self.game.engine_move(self.game.state()["revision"])

    def assert_unchanged_error(self, error, operation):
        before = self.game.state()
        with self.assertRaises(error):
            operation()
        self.assertEqual(self.game.state(), before)

    def test_initial_state_and_metadata(self):
        state = self.new()
        self.assertEqual(state["board"]["e1"], "K")
        self.assertEqual(state["board"]["e8"], "k")
        self.assertEqual(len(state["board"]), 32)
        self.assertEqual(set(state["legal_moves"]), {move.uci() for move in chess.Board().legal_moves})
        self.assertEqual(state["turn"], "white")
        self.assertEqual(state["human_color"], "white")
        self.assertTrue(state["human_to_move"])
        self.assertFalse(state["engine_to_move"])
        self.assertEqual(state["moves"], [])
        self.assertEqual(state["result"], "*")
        self.assertFalse(state["in_check"])
        self.assertEqual(state["engine"]["movetime_ms"], 100)
        self.assertEqual(state["engine"]["binary_sha256"], hashlib.sha256(self.binary.read_bytes()).hexdigest())

    def test_pgn_draw_policy_matches_current_saved_json_policy(self):
        self.new()
        record_path = self.game.save_dir / f"{self.game.game_id}.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        exported, _ = read_pgn(self.game.pgn())
        saved, _ = read_pgn(record_path.with_suffix(".pgn").read_text(encoding="utf-8"))
        self.assertEqual(record["draw_policy"], play_human.DRAW_POLICY)
        self.assertEqual(exported.headers["DrawPolicy"], record["draw_policy"])
        self.assertEqual(saved.headers["DrawPolicy"], record["draw_policy"])

    def test_human_engine_turn_and_san_history(self):
        self.new()
        self.assert_unchanged_error(ValueError, lambda: self.engine())
        state = self.human("e2e4")
        self.assertTrue(state["engine_to_move"])
        self.assertFalse(state["human_to_move"])
        self.assertEqual(state["moves"], [{"ply": 1, "san": "e4", "uci": "e2e4", "actor": "human"}])
        self.assert_unchanged_error(ValueError, lambda: self.human("d2d4"))
        state = self.engine("e7e5")
        self.assertTrue(state["human_to_move"])
        self.assertEqual(state["moves"][-1], {"ply": 2, "san": "e5", "uci": "e7e5", "actor": "engine"})
        self.assertEqual(state["last_move"], "e7e5")

    def test_invalid_uci_and_illegal_moves_do_not_mutate(self):
        self.new()
        for move in ("e2e5", "e7e5", "e1e2", "0000", "e2e4q", "garbage", "", None, 12):
            with self.subTest(move=move):
                self.assert_unchanged_error(ValueError, lambda move=move: self.human(move))

    def test_stale_revisions_reject_all_mutations(self):
        state = self.new()
        stale = state["revision"] - 1
        for operation in (
            lambda: self.game.human_move("e2e4", stale),
            lambda: self.game.engine_move(stale),
            lambda: self.game.new_game("black", 100, stale),
            lambda: self.game.undo(stale),
            lambda: self.game.resign(stale),
        ):
            self.assert_unchanged_error(play_human.RevisionError, operation)

    def test_invalid_configuration_preserves_existing_game(self):
        self.new()
        self.human("e2e4")
        for color, ms in (("blue", 100), (None, 100), ("white", 0), ("white", -1),
                          ("white", "fast"), ("white", True)):
            with self.subTest(color=color, movetime=ms):
                self.assert_unchanged_error(ValueError, lambda: self.new(color, ms))

    def test_engine_receives_full_history_and_exact_time(self):
        self.new(ms=250)
        self.human("g1f3")
        self.engine("g8f6")
        self.human("f3g1")
        self.engine("f6g8")
        self.human("g1f3")
        token = self.factory.instances[-1].calls[0][2]
        self.engine("g8f6")
        board, limit, game_token, _ = self.factory.instances[-1].calls[-1]
        self.assertEqual([move.uci() for move in board.move_stack],
                         ["g1f3", "g8f6", "f3g1", "f6g8", "g1f3"])
        self.assertEqual(board.root().fen(), chess.STARTING_FEN)
        self.assertEqual(limit.time, 0.25)
        self.assertIs(game_token, token)
        self.new()
        self.human("e2e4")
        self.engine("e7e5")
        self.assertIsNot(self.factory.instances[-1].calls[-1][2], token)

    def test_special_moves_match_independent_board(self):
        positions = (
            ("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1", "e1g1", "O-O"),
            ("4k3/8/8/3pP3/8/8/8/4K3 w - d6 0 1", "e5d6", "exd6"),
            ("7k/P7/8/8/8/8/8/4K3 w - - 0 1", "a7a8n", "a8=N"),
            ("7k/P7/8/8/8/8/8/4K3 w - - 0 1", "a7a8q", "a8=Q+"),
        )
        for fen, uci, san in positions:
            with self.subTest(uci=uci):
                self.new()
                self.game.board = chess.Board(fen)
                expected = self.game.board.copy(stack=True)
                expected.push_uci(uci)
                state = self.human(uci)
                self.assertEqual(self.game.board.fen(), expected.fen())
                self.assertEqual(state["moves"][-1]["san"], san)
                game, board = read_pgn(self.game.pgn())
                self.assertEqual(board.fen(), expected.fen())
                self.assertEqual(game.headers["FEN"], fen)

    def test_promotion_requires_explicit_piece(self):
        self.new()
        self.game.board = chess.Board("7k/P7/8/8/8/8/8/4K3 w - - 0 1")
        self.assert_unchanged_error(ValueError, lambda: self.human("a7a8"))
        state = self.game.state()
        self.assertTrue({"a7a8q", "a7a8r", "a7a8b", "a7a8n"}.issubset(state["legal_moves"]))

    def test_checked_king_state_and_mate(self):
        self.new("black")
        self.game.board = chess.Board("7k/5K2/6Q1/8/8/8/8/8 w - - 0 1")
        calls = len(self.factory.instances[-1].calls)
        state = self.engine("g6g7")
        self.assertEqual(len(self.factory.instances[-1].calls), calls + 1)
        self.assertEqual(state["result"], "1-0")
        self.assertEqual(state["termination"], "checkmate")
        self.assertTrue(state["in_check"])
        self.assertFalse(state["human_to_move"])
        self.assertFalse(state["engine_to_move"])
        self.assertEqual(state["legal_moves"], [])
        pgn, board = read_pgn(self.game.pgn())
        self.assertEqual(pgn.headers["Result"], "1-0")
        self.assertTrue(board.is_checkmate())

    def test_completed_terminal_positions_do_not_call_engine(self):
        positions = (
            (chess.Board("7k/5K2/6Q1/8/8/8/8/8 b - - 0 1"), "stalemate"),
            (chess.Board("7k/8/8/8/8/8/8/4K3 b - - 0 1"), "insufficient_material"),
            (chess.Board("7k/8/8/8/8/8/R7/4K3 b - - 100 1"), "fifty_moves"),
        )
        for board, termination in positions:
            with self.subTest(termination=termination, fen=board.fen()):
                self.new()
                self.game.board = board
                before = len(self.factory.instances[-1].calls)
                state = self.game.state()
                self.assert_unchanged_error(ValueError, lambda: self.engine())
                self.assertEqual(len(self.factory.instances[-1].calls), before)
                self.assertEqual(state["result"], "1/2-1/2")
                self.assertEqual(state["termination"], termination)

    def test_intended_repetition_waits_for_completed_move_with_full_history(self):
        self.new()
        board = chess.Board()
        for move in ("g1f3", "g8f6", "f3g1", "f6g8", "g1f3", "g8f6", "f3g1"):
            board.push_uci(move)
        self.assertTrue(board.can_claim_threefold_repetition())
        self.assertFalse(board.is_repetition(3))
        self.game.board = board
        before = len(self.factory.instances[-1].calls)
        state = self.game.state()
        self.assertEqual(state["result"], "*")
        self.assertTrue(state["engine_to_move"])
        self.assertEqual(len(self.game.board.move_stack), 7)
        state = self.engine("f6g8")
        self.assertEqual(len(self.factory.instances[-1].calls), before + 1)
        self.assertEqual(state["result"], "1/2-1/2")
        self.assertEqual(state["termination"], "threefold_repetition")
        self.assertEqual(len(self.game.board.move_stack), 8)
        self.assertTrue(self.game.board.is_repetition(3))
        pgn, replayed = read_pgn(self.game.pgn())
        self.assertEqual(pgn.headers["Result"], "1/2-1/2")
        self.assertEqual(len(replayed.move_stack), 8)
        self.assertTrue(replayed.is_repetition(3))
        self.assert_unchanged_error(ValueError, lambda: self.engine())

    def test_intended_repetition_can_be_avoided_by_different_move(self):
        self.new()
        board = chess.Board()
        for move in ("g1f3", "g8f6", "f3g1", "f6g8", "g1f3", "g8f6", "f3g1"):
            board.push_uci(move)
        self.game.board = board
        state = self.engine("e7e5")
        self.assertEqual(state["result"], "*")
        self.assertTrue(state["human_to_move"])
        self.assertFalse(self.game.board.can_claim_threefold_repetition())

    def test_fifty_move_draw_waits_for_actual_hundredth_halfmove(self):
        self.new()
        self.game.board = chess.Board("7k/8/8/8/8/8/R7/4K3 b - - 99 1")
        before = len(self.factory.instances[-1].calls)
        state = self.game.state()
        self.assertEqual(state["result"], "*")
        self.assertTrue(state["engine_to_move"])
        self.assertTrue(self.game.board.can_claim_fifty_moves())
        self.assertFalse(self.game.board.is_fifty_moves())
        state = self.engine("h8g8")
        self.assertEqual(len(self.factory.instances[-1].calls), before + 1)
        self.assertEqual(self.game.board.halfmove_clock, 100)
        self.assertEqual(state["result"], "1/2-1/2")
        self.assertEqual(state["termination"], "fifty_moves")

    def test_undo_white_pair_and_pending_human_move(self):
        self.new()
        self.human("e2e4")
        self.engine("e7e5")
        self.game.undo(self.game.state()["revision"])
        self.assertEqual(self.game.board.fen(), chess.STARTING_FEN)
        self.assertTrue(self.game.state()["human_to_move"])
        self.human("d2d4")
        self.game.undo(self.game.state()["revision"])
        self.assertEqual(self.game.board.fen(), chess.STARTING_FEN)

    def test_undo_black_preserves_engine_opening(self):
        self.new("black")
        self.engine("e2e4")
        opening = self.game.board.fen()
        try:
            self.game.undo(self.game.state()["revision"])
        except ValueError:
            pass
        self.assertEqual(self.game.board.fen(), opening)
        self.human("e7e5")
        self.engine("g1f3")
        self.game.undo(self.game.state()["revision"])
        self.assertEqual(self.game.board.fen(), opening)
        self.assertTrue(self.game.state()["human_to_move"])

    def test_human_resignation_and_restart(self):
        self.new("black")
        self.engine("e2e4")
        state = self.game.resign(self.game.state()["revision"])
        self.assertEqual(state["result"], "1-0")
        self.assertEqual(state["termination"], "resignation")
        self.assertFalse(state["human_to_move"])
        self.assertFalse(state["engine_to_move"])
        self.assertEqual(read_pgn(self.game.pgn())[0].headers["Result"], "1-0")
        state = self.new()
        self.assertEqual(state["result"], "*")
        self.assertTrue(state["human_to_move"])
        self.assertEqual(self.game.board.fen(), chess.STARTING_FEN)

    def test_engine_fault_preserves_unfinished_game_and_recovers(self):
        for reply in ("a1a8", chess.engine.PlayResult(None, None),
                      chess.engine.EngineTerminatedError("test peer exited")):
            with self.subTest(reply=str(reply)):
                self.new()
                self.human("e2e4")
                before = self.game.board.fen()
                peer = self.factory.instances[-1]
                peer.replies.append(reply)
                state = self.engine()
                self.assertEqual(self.game.board.fen(), before)
                self.assertEqual(state["result"], "*")
                self.assertTrue(state["error"])
                self.assertFalse(state["engine_to_move"])
                self.assertTrue(peer.closed)
                self.assertEqual(read_pgn(self.game.pgn())[0].headers["Result"], "*")
                self.new()
                self.assertIsNot(self.factory.instances[-1], peer)
                self.human("e2e4")
                self.engine("e7e5")
                self.assertTrue(self.game.state()["human_to_move"])

    def test_new_game_and_close_preserve_legal_unfinished_pgns(self):
        self.new()
        self.human("e2e4")
        self.engine("e7e5")
        save_dir = self.root / "games"
        previous = {path: path.read_bytes() for path in save_dir.glob("*.pgn")}
        self.assertTrue(previous, "every move must be persisted before the window closes")
        self.new("black")
        self.engine("d2d4")
        self.game.close()
        for path, content in previous.items():
            self.assertEqual(path.read_bytes(), content)
        saved = [read_pgn(path.read_text(encoding="utf-8")) for path in save_dir.glob("*.pgn")]
        histories = {tuple(move.uci() for move in game.mainline_moves()) for game, _ in saved}
        self.assertIn(("e2e4", "e7e5"), histories)
        self.assertIn(("d2d4",), histories)
        self.assertTrue(all(game.headers["Result"] == "*" for game, _ in saved))
        for path in save_dir.glob("*.json"):
            record = json.loads(path.read_text(encoding="utf-8"))
            game, board = read_pgn(path.with_suffix(".pgn").read_text(encoding="utf-8"))
            self.assertEqual(record["game_id"], path.stem)
            self.assertEqual(record["uci_moves"], [move.uci() for move in game.mainline_moves()])
            self.assertEqual(record["final_fen"], board.fen(en_passant="fen"))
            self.assertEqual(record["initial_fen"], game.board().fen(en_passant="fen"))
            self.assertEqual(record["state"]["result"], game.headers["Result"])
            self.assertEqual(record["state"]["engine"]["binary_sha256"],
                             hashlib.sha256(self.binary.read_bytes()).hexdigest())


class RealEngineSmokeTests(unittest.TestCase):
    def test_real_engine_white_and_black_100ms(self):
        if REAL_BINARY is None:
            self.skipTest("supply binary path to run the real Regicide smoke")
        with tempfile.TemporaryDirectory(prefix="regicide-human-smoke-") as folder:
            game = play_human.HumanGame(REAL_BINARY, Path(folder))
            try:
                state = game.new_game("white", 100, game.state()["revision"])
                state = game.human_move("e2e4", state["revision"])
                state = game.engine_move(state["revision"])
                self.assertIsNone(state["error"])
                self.assertEqual(len(state["moves"]), 2)
                self.assertTrue(state["human_to_move"])
                self.assertTrue(state["engine"]["name"].startswith("Regicide"))
                print(f"\nReal human-play peer: {state['engine']['name']}; 100ms per move", flush=True)
                _, board = read_pgn(game.pgn())
                self.assertEqual(board.fen(), game.board.fen())
                state = game.new_game("black", 100, state["revision"])
                state = game.engine_move(state["revision"])
                self.assertIsNone(state["error"])
                self.assertEqual(len(state["moves"]), 1)
                self.assertTrue(state["human_to_move"])
                _, board = read_pgn(game.pgn())
                self.assertEqual(board.fen(), game.board.fen())
            finally:
                game.close()


class HumanHTTPTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="regicide-human-http-")
        root = Path(self.folder.name)
        binary = root / "regicide-test-peer.exe"
        binary.write_bytes(b"fake UCI peer; not executed")
        self.factory = FakeFactory()
        self.game = play_human.HumanGame(binary, root / "games", self.factory,
                                        opening_variety_enabled=False)
        self.server = play_human.HumanServer(self.game, port=0)
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

    def request(self, path, data=None, *, token=True, headers=None, body=None):
        actual_headers = {}
        if token:
            actual_headers["X-Regicide-Token"] = self.server.token
        if data is not None:
            body = json.dumps(data).encode("utf-8")
        if body is not None:
            actual_headers["Content-Type"] = "application/json"
        actual_headers.update(headers or {})
        request = urllib.request.Request(self.server.origin + path, data=body, headers=actual_headers)
        try:
            response = urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            payload = response.read()
            return response.status, response.headers, payload

    def test_loopback_and_static_assets(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        for path, mime in (("/", "text/html"), ("/app.js", "text/javascript"), ("/style.css", "text/css")):
            code, headers, body = self.request(path, token=False)
            self.assertEqual(code, 200)
            self.assertIn(mime, headers["Content-Type"])
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
            if path == "/":
                self.assertNotIn(b"__REGICIDE_TOKEN__", body)
                self.assertIn(self.server.token.encode("ascii"), body)

    def test_move_audio_assets_are_binary_same_origin_and_allowlisted(self):
        for filename in ("move.wav", "capture.wav"):
            path = "/audio/" + filename
            code, headers, body = self.request(path, token=False)
            self.assertEqual(code, 200)
            self.assertEqual(headers["Content-Type"], "audio/wav")
            self.assertEqual(body, (ROOT / "godot/assets/audio" / filename).read_bytes())
            self.assertTrue(body.startswith(b"RIFF") and body[8:12] == b"WAVE")
            self.assertEqual(self.request(path, headers={"Origin": "https://external.example"})[0], 403)
        self.assertEqual(self.request("/move_sound.js", token=False)[0], 200)
        for path in ("/audio/unknown.wav", "/audio/../app.js", "/audio/../../scripts/play_human.py"):
            self.assertEqual(self.request(path, token=False)[0], 404)

    def test_api_requires_valid_token_and_same_origin(self):
        before = self.game.state()
        for path in ("/api/state", "/api/pgn"):
            self.assertEqual(self.request(path, token=False)[0], 403)
            self.assertEqual(self.request(path, headers={"X-Regicide-Token": "incorrect"})[0], 403)
            self.assertEqual(self.request(path, headers={"Origin": "https://external.example"})[0], 403)
            self.assertEqual(self.request(path, headers={"Host": "attacker.example"})[0], 403)
            self.assertEqual(self.request(path, headers={"Origin": self.server.origin})[0], 200)
        self.assertEqual(self.game.state(), before)

    def test_unauthorized_mutation_does_not_change_game(self):
        before = self.game.state()
        move = {"revision": before["revision"], "move": "e2e4"}
        self.assertEqual(self.request("/api/move", move, token=False)[0], 403)
        self.assertEqual(self.request("/api/move", move, headers={"Origin": "null"})[0], 403)
        self.assertEqual(self.request("/api/move", move, headers={"Host": "attacker.example"})[0], 403)
        self.assertEqual(self.game.state(), before)

    def test_revision_conflict_and_bad_moves_preserve_board(self):
        before = self.game.state()
        wire_before = json.loads(json.dumps(before))
        code, _, body = self.request("/api/move", {"revision": before["revision"] - 1, "move": "e2e4"})
        self.assertEqual(code, 409)
        self.assertEqual(json.loads(body)["state"], wire_before)
        self.assertEqual(self.game.state(), before)
        code, _, body = self.request("/api/move", {"revision": before["revision"], "move": "e2e5"})
        self.assertEqual(code, 400)
        self.assertEqual(json.loads(body)["state"], wire_before)
        self.assertEqual(self.game.state(), before)

    def test_malformed_or_oversized_body(self):
        before = self.game.state()
        for body in (b"{", b"[]", b"null", b"", b"x" * 4097, b"\xff"):
            with self.subTest(body=body[:20]):
                self.assertEqual(self.request("/api/move", body=body)[0], 400)
        self.assertEqual(self.request("/api/move", body=b"{}", headers={"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.game.state(), before)

    def test_concurrent_same_revision_accepts_only_one_move(self):
        revision = self.game.state()["revision"]
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(
                lambda move: self.request("/api/move", {"revision": revision, "move": move})[0],
                ("e2e4", "d2d4")))
        self.assertEqual(sorted(results), [200, 409])
        state = self.game.state()
        self.assertEqual(state["revision"], revision + 1)
        self.assertEqual(len(state["moves"]), 1)
        self.assertTrue(state["engine_to_move"])

    def test_legally_play_two_plies_and_export(self):
        current = self.game.state()
        code, _, body = self.request("/api/move", {"revision": current["revision"], "move": "e2e4"})
        self.assertEqual(code, 200)
        current = json.loads(body)
        self.assertTrue(current["engine_to_move"])
        self.factory.instances[-1].replies.append("e7e5")
        code, _, body = self.request("/api/engine", {"revision": current["revision"]})
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)["human_to_move"])
        code, headers, body = self.request("/api/pgn")
        self.assertEqual(code, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        game, board = read_pgn(body.decode("utf-8"))
        self.assertEqual([move.uci() for move in game.mainline_moves()], ["e2e4", "e7e5"])
        self.assertEqual(board.fen(), self.game.board.fen())

    def test_unknown_or_traversal_paths_do_not_serve_files(self):
        for path in ("/../scripts/play_human.py", "/%2e%2e/scripts/play_human.py", "/.git/config", "/api/missing"):
            with self.subTest(path=path):
                code, _, body = self.request(path)
                self.assertEqual(code, 404)
                self.assertNotIn(b"HumanGame", body)
        self.assertEqual(self.request("/api/missing", {"revision": self.game.state()["revision"]})[0], 404)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path, nargs="?")
    args, rest = parser.parse_known_args()
    REAL_BINARY = args.binary.resolve() if args.binary else None
    subprocess.run([sys.executable, str(ROOT / "tests/evaluation_state_tests.py")],
                   check=True, timeout=30)
    unittest.main(argv=[str(Path(__file__)), *rest], verbosity=2)
