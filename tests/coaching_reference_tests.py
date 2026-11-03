"""Position-reference contracts; no user game files or processes are used."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
import urllib.error
import urllib.request

import chess

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from coaching_reference import CoachingReference
from play_human import HumanServer


def payload(moves=(), root=chess.STARTING_FEN, request_id="a"):
    board = chess.Board(root)
    for move in moves:
        board.push_uci(move)
    return {"request_id": request_id, "root_fen": root, "history_uci": list(moves),
            "fen": board.fen(en_passant="fen")}


class Referee:
    def __init__(self):
        self.value = {}
        self.calls = []
        self.cancelled = 0
        self.closed = False

    def update_position(self, **context):
        self.calls.append(context)
        self.value = {"game_id": context["game_id"], "revision": context["revision"],
                      "fen": context["board"].fen(en_passant="fen"), "status": "loading"}

    def snapshot(self):
        return copy.deepcopy(self.value)

    def finish(self, cp=42, mate=None):
        self.value.update(status="ready", cp=cp, mate=mate, bound="exact", depth=16,
                          pv_uci=["e2e4"], uci="e2e4", available=True,
                          engine_name="Stockfish fake", binary_sha256="test-sha")

    def cancel(self):
        self.cancelled += 1

    def close(self):
        self.closed = True


class ReferenceTests(unittest.TestCase):
    def setUp(self):
        self.peer = Referee()
        self.service = CoachingReference(referee=self.peer)
        self.addCleanup(self.service.close)

    def test_white_and_black_keep_white_score_and_native_reports_unchanged(self):
        native = {"best": {"score_cp_white": -900}, "classification": "blunder"}
        before = copy.deepcopy(native)
        for moves, cp in [((), 123), (("e2e4",), -234)]:
            self.service.select(payload(moves))
            self.peer.finish(cp)
            value = self.service.snapshot()
            self.assertEqual(value["cp"], cp)
            self.assertEqual(value["turn"], "black" if moves else "white")
            self.assertEqual(value["source"], "stockfish_coaching_reference")
            self.assertNotIn("pv_uci", value)
            self.assertNotIn("uci", value)
        self.assertEqual(native, before)

    def test_mate_winners_and_terminal_rules(self):
        for moves, mate, winner in [((), 3, "white"), (("e2e4",), -2, "black")]:
            self.service.select(payload(moves))
            self.peer.finish(None, mate)
            self.assertEqual(self.service.snapshot()["winner"], winner)
        terminal = self.service.select(payload(["f2f3", "e7e5", "g2g4", "d8h4"]))
        self.assertEqual((terminal["status"], terminal["result"], terminal["winner"]),
                         ("terminal", "0-1", "black"))
        draw = self.service.select(payload(root="8/8/8/8/8/8/1k6/7K w - - 0 1"))
        self.assertEqual((draw["result"], draw["winner"]), ("1/2-1/2", None))

    def test_full_history_repetition_and_fen_are_validated(self):
        moves = ["g1f3", "g8f6", "f3g1", "f6g8"]
        self.service.select(payload(moves))
        self.assertEqual([m.uci() for m in self.peer.calls[-1]["board"].move_stack], moves)
        repeated = self.service.select(payload(moves * 2))
        self.assertEqual(repeated["status"], "loading")
        self.assertTrue(self.peer.calls[-1]["board"].can_claim_threefold_repetition())
        continued = self.service.select(payload(moves * 2 + ["e2e4"]))
        self.assertEqual(continued["status"], "loading")
        for invalid in [{**payload(), "history_uci": ["e2e5"]},
                        {**payload(["e2e4"]), "fen": chess.STARTING_FEN},
                        {**payload(), "cp": 999}, {**payload(), "request_id": ""}]:
            with self.assertRaises(ValueError):
                self.service.select(invalid)

    def test_claimable_fifty_move_draw_does_not_end_coaching_position(self):
        request = payload(root="r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 100 60")
        self.assertTrue(chess.Board(request["fen"]).can_claim_fifty_moves())
        self.assertEqual(self.service.select(request)["status"], "loading")
        self.peer.finish(0)
        self.assertEqual(self.service.snapshot()["status"], "ready")
        service = CoachingReference()
        self.addCleanup(service.close)
        self.assertFalse(service._referee._live_claim_draw)

    def test_stale_position_packet_cannot_replace_new_frame(self):
        self.service.select(payload())
        stale = self.peer.snapshot()
        self.service.select(payload(["e2e4"], request_id="b"))
        self.peer.value = {**stale, "status": "ready", "cp": 800}
        value = self.service.snapshot()
        self.assertEqual(value["status"], "loading")
        self.assertIsNone(value["cp"])
        self.assertEqual(value["request_id"], "b")

    def test_cache_is_full_path_specific_and_does_not_restart_same_frame(self):
        original = payload(["g1f3", "g8f6", "f3g1", "f6g8"])
        self.service.select(original)
        self.peer.finish(70)
        self.service.snapshot()
        self.service.select(payload(["e2e4"], request_id="b"))
        cached = self.service.select({**original, "request_id": "c"})
        self.assertEqual((cached["cp"], cached["request_id"], cached["cached"]), (70, "c", True))
        self.assertEqual(len(self.peer.calls), 2)
        self.service.select({**original, "request_id": "d"})
        self.assertEqual(len(self.peer.calls), 2)
        # Identical FEN without its repetition path is a different engine context.
        self.service.select(payload(root=original["fen"], request_id="e"))
        self.assertEqual(len(self.peer.calls), 3)

    def test_hidden_retry_erases_score_and_rejects_late_packets(self):
        self.service.select(payload())
        self.peer.finish(500)
        self.service.snapshot()
        hidden = self.service.select({"request_id": "a", "hidden": True})
        self.assertEqual(hidden["status"], "hidden")
        self.assertIsNone(hidden["cp"])
        self.assertNotIn("pv_uci", hidden)
        self.peer.finish(-900)
        self.assertIsNone(self.service.snapshot()["cp"])
        self.assertEqual(self.service.select(payload(request_id="revealed"))["cp"], 500)

    def test_old_hide_does_not_cancel_new_selected_frame(self):
        self.service.select(payload())
        self.service.select(payload(["e2e4"], request_id="b"))
        cancellations = self.peer.cancelled
        result = self.service.select({"request_id": "a", "hidden": True})
        self.assertEqual(result["request_id"], "b")
        self.assertEqual(cancellations, self.peer.cancelled)

    def test_missing_engine_is_explicit_fallback_without_fabricated_scores(self):
        service = CoachingReference()
        self.addCleanup(service.close)
        value = service.select(payload())
        self.assertEqual(value["status"], "unavailable")
        self.assertIsNone(value["cp"])
        self.assertIsNone(value["mate"])

    def test_explicit_retry_restarts_failed_same_position_but_polling_does_not(self):
        self.service.select(payload())
        self.peer.value.update(status="error", cp=None)
        self.assertEqual(self.service.snapshot()["status"], "error")
        self.service.select(payload())
        self.assertEqual(len(self.peer.calls), 1)
        self.service.select(payload(request_id="retry-1"))
        self.assertEqual(len(self.peer.calls), 2)
        self.peer.finish(21)
        self.assertEqual(self.service.snapshot()["cp"], 21)

    def test_close_clears_cache_and_stops_only_owned_referee(self):
        self.service.select(payload())
        self.service.close()
        self.assertTrue(self.peer.closed)
        self.assertEqual(self.service.snapshot()["status"], "unavailable")
        self.assertIsNone(self.service.snapshot()["request_id"])

    def test_http_auth_validation_and_read_only_isolation(self):
        with tempfile.TemporaryDirectory() as folder:
            game = SimpleNamespace(save_dir=Path(folder), coaching_reference=self.service)
            server = HumanServer(game)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            def request(data=None, token=True):
                headers = {"Content-Type": "application/json", "Origin": server.origin}
                if token:
                    headers["X-Regicide-Token"] = server.token
                req = urllib.request.Request(server.origin + "/api/coaching-reference",
                    data=json.dumps(data).encode() if data is not None else None, headers=headers)
                try:
                    result = urllib.request.urlopen(req, timeout=3)
                except urllib.error.HTTPError as error:
                    result = error
                with result:
                    return result.status, json.load(result)
            try:
                self.assertEqual(request(token=False)[0], 403)
                self.assertEqual(request(payload(), token=False)[0], 403)
                self.assertEqual(request(payload())[0], 200)
                code, invalid = request({"request_id": "bad"})
                self.assertEqual(code, 400)
                self.assertNotIn("state", invalid)
                self.assertIn("reference", invalid)
                self.assertEqual(list(Path(folder).iterdir()), [])
                # No state/coaching/native methods even exist on the fake game.
                self.assertEqual(request()[1]["request_id"], "a")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
