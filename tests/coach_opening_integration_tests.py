"""Opening coaching integrates with history, cache rebuilding and normal review.

The fake engine only provides deterministic transport data; it is not a chess
strength oracle and these scores do not establish an opening advantage.
"""
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

import chess
import chess.pgn

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from coach_explanations import COACH_VERSION, describe_move
from coaching_session import CoachingSession
from coaching_tests import Factory, FakeEngine, packet, wait_for

MOVES = "1. Nf3 d5 2. d4 Nf6 3. c4 e6 4. Nc3 Be7 5. Bg5 O-O 6. e3 h6 *"
PGN = '[Opening "FORGED OPENING"]\n[Result "*"]\n\n' + MOVES + '\n'


class ReplayEngine(FakeEngine):
    def __init__(self, game):
        super().__init__()
        self.game_moves = list(game.mainline_moves())

    def analyse(self, board, limit, **kwargs):
        self.calls.append((board.copy(stack=True), limit, kwargs))
        move = kwargs.get("root_moves", [self.game_moves[len(board.move_stack)]])[0]
        return packet(0, color=chess.WHITE, pv=[move.uci()], depth=10)


class OpeningIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="regicide-opening-unit-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        binary = self.root / "fake.exe"
        binary.write_bytes(b"Not executed")
        self.factory = Factory()
        self.peer = ReplayEngine(chess.pgn.read_game(io.StringIO(PGN)))
        self.factory.peers.append(self.peer)
        self.session = CoachingSession(binary, save_dir=self.root, engine_factory=self.factory)
        self.addCleanup(self.session.close)

    def review(self):
        self.session.start(PGN, "black", 500, self.session.revision)
        return wait_for(lambda: self.session.snapshot() if self.session.snapshot()["status"] != "running" else None)

    def test_actual_history_names_transposition_and_keeps_existing_feedback(self):
        report = self.review()
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["coach"]["version"], COACH_VERSION)
        board = chess.Board()
        for row in report["moves"]:
            saved = copy.deepcopy(row)
            rebuilt = describe_move(board, row)
            self.assertEqual(row, saved)
            self.assertEqual(row["coach"], rebuilt)
            board.push_uci(row["uci"])
            opening = rebuilt["opening"]
            self.assertIsNotNone(opening)
            self.assertEqual(opening["fen"], board.fen(en_passant="fen"))
            self.assertEqual(opening["ply_index"], row["ply"])
            self.assertEqual([section["id"] for section in opening["sections"]], ["white", "black", "balance"])
            self.assertTrue(rebuilt["dialogue"])
            self.assertNotIn("FORGED", json.dumps(opening))
            if row["ply"] < 12:
                self.assertEqual(opening["assessment"]["status"], "too_early")
        self.assertIn("Réti", report["moves"][0]["coach"]["opening"]["white"]["name"])
        self.assertIn("Queen's Gambit", report["moves"][-1]["coach"]["opening"]["recognition"]["name"])
        self.assertEqual(report["moves"][-1]["coach"]["opening"]["assessment"]["status"], "balanced")
        self.assertTrue(report["moves"][-1]["coach"]["opening"]["assessment"]["provisional"])
        self.assertLessEqual(len(self.peer.calls), 24)

    def test_cached_opening_text_rebuilds_without_an_engine_or_original_game_write(self):
        original = self.root / "original.pgn"
        original.write_text(PGN, encoding="utf-8")
        before = original.read_bytes()
        first = self.review()
        cache_path, = (self.root / "coaching").glob("*.json")
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        for row in cached["report"]["moves"]:
            row["coach"]["opening"] = {"summary": "FORGED winner"}
        cache_path.write_text(json.dumps(cached), encoding="utf-8")
        launches, calls = self.factory.calls, len(self.peer.calls)
        second = self.session.start(PGN, "black", 500, self.session.revision)
        self.assertTrue(second["cached"])
        self.assertEqual(self.factory.calls, launches)
        self.assertEqual(len(self.peer.calls), calls)
        self.assertEqual([row["coach"] for row in first["moves"]], [row["coach"] for row in second["moves"]])
        self.assertEqual(original.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
