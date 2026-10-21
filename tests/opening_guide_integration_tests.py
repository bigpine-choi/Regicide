"""Read-only opening study through live state, undo, handicaps and archive HTTP."""

import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.request

import chess

import human_play_tests as baseline
from game_archive import GameArchive
from opening_guide import get_opening_guide
from opening_presentation import OpeningGuideCache

backend = baseline.play_human


class OpeningPresentationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="regicide-opening-ui-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.binary = self.root / "fake-engine.exe"
        self.binary.write_bytes(b"fake peer, never executed")
        self.factory = baseline.FakeFactory()
        self.game = backend.HumanGame(self.binary, self.root / "games", self.factory)
        self.addCleanup(self.game.close)

    def play(self, moves):
        for uci in moves.split():
            revision = self.game.state()["revision"]
            if self.game.board.turn == self.game.human_color:
                self.game.human_move(uci, revision)
            else:
                self.factory.instances[-1].replies.append(uci)
                self.game.engine_move(revision)

    def files(self):
        return {p.name: p.read_bytes() for p in self.game.save_dir.iterdir() if p.is_file()}

    def ruy(self):
        self.play("e2e4 e7e5 g1f3 b8c6 f1b5")

    def test_initial_state_has_legal_catalog_branches_without_engine_search(self):
        state = self.game.state()
        guide = state["opening_guide"]
        self.assertEqual(guide["status"], "in_catalog")
        self.assertIsNone(guide["matched_opening"])
        self.assertIn("e2e4", [row["uci"] for row in guide["continuations"]])
        self.assertFalse(self.factory.instances[-1].calls)
        self.assertEqual(self.game.board.move_stack, [])

    def test_named_opening_and_concrete_future_line_preview(self):
        self.ruy()
        guide = self.game.state()["opening_guide"]
        self.assertTrue(guide["current_matches"][0]["name"].startswith("Ruy Lopez"))
        branch = next(row for row in guide["continuations"] if row["uci"] == "a7a6")
        self.assertTrue(any("Morphy" in row["name"] for row in branch["immediate_openings"]))
        for line in branch["reachable_openings"]:
            board = self.game.board.copy(stack=True)
            moves = [chess.Move.from_uci(uci) for uci in line["remaining_uci"]]
            self.assertEqual(board.variation_san(moves), line["remaining_san"])
            self.assertEqual(moves[0].uci(), branch["uci"])
            for move in moves:
                self.assertIn(move, board.legal_moves)
                board.push(move)
            self.assertIn(line["name"], [r["name"] for r in get_opening_guide(board)["current_matches"]])

    def test_reads_and_response_mutation_do_not_change_game_or_save(self):
        self.ruy()
        before = self.game.state()
        saved = self.files()
        calls = len(self.factory.instances[-1].calls)
        for _ in range(3):
            result = self.game.state()
            result["opening_guide"]["current_matches"][0]["name"] = "changed client copy"
            result["opening_guide"]["continuations"].clear()
        self.assertEqual(self.game.state(), before)
        self.assertEqual(self.files(), saved)
        self.assertEqual(len(self.factory.instances[-1].calls), calls)

    def test_undo_recomputes_guide_for_actual_remaining_history(self):
        self.play("e2e4 c7c5 g1f3 d7d6 d2d4 c5d4 f3d4 g8f6 b1c3 a7a6")
        before = self.game.state()
        self.assertIn("Najdorf", before["opening_guide"]["current_matches"][0]["name"])
        after = self.game.undo(before["revision"])
        self.assertNotEqual(after["opening_guide"]["position_key"], before["opening_guide"]["position_key"])
        self.assertEqual(after["opening_guide"], get_opening_guide(self.game.board))

    def test_handicap_preview_cancel_and_replacement_have_own_guides(self):
        self.play("e2e4 e7e5 g1f3 b8c6 f1b5 a7a6")
        original = self.game.state()
        saved = self.files()
        preview = self.game.begin_handicap(2, original["revision"])
        self.assertEqual(preview["opening_guide"], get_opening_guide(self.game.board))
        self.assertNotEqual(preview["opening_guide"]["position_key"], original["opening_guide"]["position_key"])
        self.assertEqual(self.files(), saved)
        restored = self.game.cancel_handicap(preview["revision"])
        self.assertEqual(restored["opening_guide"], original["opening_guide"])
        preview = self.game.begin_handicap(2, restored["revision"])
        changed = self.game.forced_move("c7c5", preview["revision"])
        self.assertTrue(changed["opening_guide"]["current_matches"][0]["name"].startswith("Sicilian"))

    def test_custom_fen_does_not_claim_unknown_earlier_history(self):
        self.ruy()
        self.game.board = chess.Board(self.game.board.fen())
        guide = self.game.state()["opening_guide"]
        self.assertEqual(guide["history_scope"], "custom")
        self.assertEqual(guide["current_matches"][0]["match_type"], "position")

    def test_catalog_departure_keeps_last_named_opening(self):
        self.ruy()
        before = self.game.state()["opening_guide"]
        child = None
        for move in sorted(self.game.board.legal_moves, key=lambda m: m.uci()):
            board = self.game.board.copy(stack=True)
            board.push(move)
            if get_opening_guide(board)["status"] == "beyond_catalog":
                child = move
                break
        self.assertIsNotNone(child)
        self.factory.instances[-1].replies.append(child.uci())
        after = self.game.engine_move(self.game.state()["revision"])["opening_guide"]
        self.assertEqual(after["status"], "beyond_catalog")
        self.assertEqual(after["matched_opening"]["name"], before["current_matches"][0]["name"])
        self.assertEqual(after["continuations"], [])

    def test_unavailable_optional_data_does_not_block_play_and_recovers(self):
        with patch("opening_presentation.get_opening_guide", side_effect=OSError("private/local/path")):
            self.game._opening_guide = OpeningGuideCache()
            state = self.game.human_move("e2e4", self.game.state()["revision"])
            self.assertEqual(state["opening_guide"]["status"], "unavailable")
            self.assertNotIn("private/local/path", json.dumps(state["opening_guide"]))
            self.assertEqual(state["last_move"], "e2e4")
        self.assertEqual(self.game.state()["opening_guide"]["status"], "in_catalog")

    def test_archive_frames_reconstruct_guides_without_trusting_saved_snapshot(self):
        self.ruy()
        path = self.game.save_dir / (self.game.game_id + ".json")
        record = json.loads(path.read_text(encoding="utf-8"))
        record["state"]["opening_guide"] = {"status": "made-up", "name": "do not trust"}
        path.write_text(json.dumps(record), encoding="utf-8")
        saved = self.files()
        live = self.game.state()
        details = GameArchive(self.game.save_dir).get_game(self.game.game_id)
        board = self.game.board.root()
        for ply, frame in enumerate(details["frames"]):
            self.assertEqual(frame["opening_guide"], get_opening_guide(board))
            if ply < len(details["moves"]):
                board.push_uci(details["moves"][ply]["uci"])
        self.assertEqual(self.files(), saved)
        self.assertEqual(self.game.state(), live)

    def test_authenticated_http_includes_read_only_live_and_replay_guides(self):
        self.ruy()
        server = backend.HumanServer(self.game, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        saved = self.files()
        try:
            for path in ("/api/state", "/api/games/" + self.game.game_id):
                req = urllib.request.Request(server.origin + path, headers={"X-Regicide-Token": server.token})
                with urllib.request.urlopen(req, timeout=5) as response:
                    value = json.load(response)
                guide = value.get("opening_guide") or value["frames"][-1]["opening_guide"]
                self.assertTrue(guide["current_matches"][0]["name"].startswith("Ruy Lopez"))
            self.assertEqual(self.files(), saved)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_cache_is_bounded_and_keeps_distinct_history_and_client_copies(self):
        cache = OpeningGuideCache(capacity=1)
        first = chess.Board()
        second = chess.Board()
        second.push_uci("e2e4")
        with patch("opening_presentation.get_opening_guide", return_value={"status": "in_catalog", "names": []}) as query:
            guide = cache.get(first)
            guide["names"].append("client edit")
            self.assertEqual(cache.get(first)["names"], [])
            cache.get(second)
            cache.get(first)
            self.assertEqual(query.call_count, 3)
        self.assertEqual(len(cache._entries), 1)


if __name__ == "__main__":
    unittest.main()
