"""Analysis castling rights, legal restrictions and exact undo.

Usage: python tests/analysis_castling_tests.py [path/to/regicide]
The optional actual binary is queried with searchmoves to require each castle;
this verifies UCI/rule support, not that castling is the engine's preferred move.
"""

import argparse
from contextlib import closing
from pathlib import Path
import subprocess
import tempfile
import unittest

import chess
import chess.engine

import human_play_tests as baseline
from hint_state_tests import HintFactory, wait_for
from analysis_session import AnalysisSession

REAL_BINARY = None
OPEN_BOARD = "r3k2r/8/8/8/8/8/8/R3K2R"


def open_fen(turn="w", rights="KQkq"):
    return f"{OPEN_BOARD} {turn} {rights} - 7 9"


def restriction_cases():
    # Expected king-side / queen-side eligibility for White; mirror() supplies
    # the equivalent Black cases, including the often-misread b-file attack.
    for label, fen, king_side, queen_side in [
        ("rights missing", open_fen(rights="-"), False, False),
        ("king currently checked", "r3k2r/8/8/8/8/8/4r3/R3K2R w KQkq - 0 1", False, False),
        ("king-side transit attacked", "r3k2r/8/8/8/8/5r2/8/R3K2R w KQkq - 0 1", False, True),
        ("king-side landing attacked", "r3k2r/8/8/8/8/6r1/8/R3K2R w KQkq - 0 1", False, True),
        ("queen-side transit attacked", "r3k2r/8/8/8/8/3r4/8/R3K2R w KQkq - 0 1", True, False),
        ("b-file attacked only", "r3k2r/8/8/8/8/1r6/8/R3K2R w KQkq - 0 1", True, True),
        ("king-side blocked", "r3k2r/8/8/8/8/8/8/R3KB1R w KQkq - 0 1", False, True),
        ("queen-side rook path blocked", "r3k2r/8/8/8/8/8/8/RN2K2R w KQkq - 0 1", True, False),
    ]:
        board = chess.Board(fen)
        yield label + " white", board, ("e1g1", king_side), ("e1c1", queen_side)
        yield label + " black", board.mirror(), ("e8g8", king_side), ("e8c8", queen_side)


class AnalysisCastlingTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="regicide-analysis-castling-")
        self.root = Path(self.folder.name)
        self.binary = self.root / "fake-uci.exe"
        self.binary.write_bytes(b"fake peer; never executed")
        self.factory = HintFactory()
        self.analysis = AnalysisSession(self.binary, engine_factory=self.factory)
        self.addCleanup(self.folder.cleanup)
        self.addCleanup(self.analysis.close)

    def test_all_four_castles_move_both_pieces_and_undo_exact_fen(self):
        for turn, move, king_square, rook_square, old_rook in [
            ("w", "e1g1", "g1", "f1", "h1"),
            ("w", "e1c1", "c1", "d1", "a1"),
            ("b", "e8g8", "g8", "f8", "h8"),
            ("b", "e8c8", "c8", "d8", "a8"),
        ]:
            with self.subTest(move=move):
                original = open_fen(turn)
                state = self.analysis.start(original, 100, self.analysis.revision)
                self.assertIn(move, state["legal_moves"])
                expected = chess.Board(original)
                expected.push_uci(move)
                state = self.analysis.move(move, state["revision"])
                self.assertEqual(state["fen"], expected.fen(en_passant="fen"))
                self.assertEqual(state["board"][king_square], "K" if turn == "w" else "k")
                self.assertEqual(state["board"][rook_square], "R" if turn == "w" else "r")
                self.assertNotIn(move[:2], state["board"])
                self.assertNotIn(old_rook, state["board"])
                self.assertEqual(state["moves"][0]["san"], "O-O" if move[2] == "g" else "O-O-O")
                undone = self.analysis.undo(state["revision"])
                self.assertEqual(undone["fen"], original)
                self.assertEqual(undone["moves"], [])
                self.assertFalse(undone["can_undo"])
                self.assertIn(move, undone["legal_moves"])

    def test_castling_restrictions_and_b_file_attack_for_both_colors(self):
        for label, board, *moves in restriction_cases():
            with self.subTest(label=label):
                self.assertTrue(board.is_valid(), board.fen())
                state = self.analysis.start(board.fen(en_passant="fen"), 100, self.analysis.revision)
                for move, allowed in moves:
                    self.assertEqual(move in state["legal_moves"], allowed, (label, move))
                    if not allowed:
                        before = self.analysis.snapshot()
                        with self.assertRaises(ValueError):
                            self.analysis.move(move, before["revision"])
                        after = self.analysis.snapshot()
                        for field in ("revision", "game_id", "fen", "board", "moves"):
                            self.assertEqual(after[field], before[field], (label, field))

    def test_rook_or_king_movement_does_not_restore_lost_rights(self):
        state = self.analysis.start(open_fen(), 100, self.analysis.revision)
        for move in ("h1h2", "e8e7", "h2h1", "e7e8"):
            state = self.analysis.move(move, state["revision"])
        self.assertNotIn("e1g1", state["legal_moves"])
        self.assertIn("e1c1", state["legal_moves"])
        self.assertEqual(state["fen"].split()[2], "Q")

    def test_invalid_home_piece_rights_are_rejected_atomically(self):
        self.analysis.start(open_fen(), 100, self.analysis.revision)
        for fen in ("r3k2r/8/8/8/8/8/8/R3K3 w KQkq - 0 1",
                    "r3k2r/8/8/8/8/8/8/R4K1R w KQkq - 0 1",
                    "4k2r/8/8/8/8/8/8/R3K2R b KQkq - 0 1"):
            with self.subTest(fen=fen):
                before = self.analysis.snapshot()
                with self.assertRaisesRegex(ValueError, "캐슬링"):
                    self.analysis.start(fen, 100, self.analysis.revision)
                after = self.analysis.snapshot()
                for field in ("revision", "game_id", "fen", "board", "moves"):
                    self.assertEqual(after[field], before[field])

    def test_analysis_castles_leave_live_game_and_archive_unchanged(self):
        game = baseline.play_human.HumanGame(self.binary, self.root / "games",
                                             baseline.FakeFactory(), HintFactory())
        self.addCleanup(game.close)
        original = game.state()
        evaluation = game.evaluation()
        pgn = game.pgn()
        records = {path.name: path.read_bytes() for path in game.save_dir.iterdir()}
        for turn, move in (("w", "e1g1"), ("w", "e1c1"), ("b", "e8g8"), ("b", "e8c8")):
            state = game.analysis.start(open_fen(turn), 100, game.analysis.revision)
            state = game.analysis.move(move, state["revision"])
            state = game.analysis.undo(state["revision"])
            self.assertEqual(state["fen"], open_fen(turn))
        self.assertEqual(game.state(), original)
        self.assertEqual(game.evaluation(), evaluation)
        self.assertEqual(game.pgn(), pgn)
        self.assertEqual({path.name: path.read_bytes() for path in game.save_dir.iterdir()}, records)


class RealAnalysisCastlingTests(unittest.TestCase):
    def setUp(self):
        if REAL_BINARY is None:
            self.skipTest("pass a Regicide binary for actual UCI castling checks")

    def test_actual_uci_all_four_castles_then_analysis_exact_undo(self):
        with tempfile.TemporaryDirectory(prefix="regicide-real-castling-") as folder:
            with closing(baseline.play_human.HumanGame(REAL_BINARY, Path(folder) / "games")) as game:
                original = game.state()
                records = {path.name: path.read_bytes() for path in game.save_dir.iterdir()}
                with chess.engine.SimpleEngine.popen_uci([str(REAL_BINARY), "uci"], timeout=3) as engine:
                    for turn, move in (("w", "e1g1"), ("w", "e1c1"), ("b", "e8g8"), ("b", "e8c8")):
                        with self.subTest(move=move):
                            board = chess.Board(open_fen(turn))
                            reply = engine.play(board, chess.engine.Limit(depth=2),
                                                root_moves=[chess.Move.from_uci(move)])
                            self.assertEqual(reply.move.uci(), move)
                            state = game.analysis.start(board.fen(en_passant="fen"), 100, game.analysis.revision)
                            wait_for(lambda: game.analysis.snapshot()["hint"]["status"] == "ready", timeout=8)
                            state = game.analysis.move(move, state["revision"])
                            board.push_uci(move)
                            self.assertEqual(state["fen"], board.fen(en_passant="fen"))
                            wait_for(lambda: game.analysis.snapshot()["hint"]["status"] == "ready", timeout=8)
                            self.assertIn(game.analysis.snapshot()["hint"]["move"], state["legal_moves"])
                            inspected = subprocess.run([str(REAL_BINARY), "inspect", "--fen", open_fen(turn),
                                                        "--play", move], capture_output=True, text=True,
                                                       timeout=5, check=True)
                            self.assertIn("fen: " + state["fen"], inspected.stdout)
                            self.assertIn("make_undo: OK", inspected.stdout)
                            state = game.analysis.undo(state["revision"])
                            self.assertEqual(state["fen"], open_fen(turn))
                            self.assertIn(move, state["legal_moves"])
                            wait_for(lambda: game.analysis.snapshot()["hint"]["status"] == "ready", timeout=8)
                self.assertEqual(game.state(), original)
                self.assertEqual({path.name: path.read_bytes() for path in game.save_dir.iterdir()}, records)

    def test_actual_core_move_lists_match_castling_restrictions(self):
        for label, board, *moves in restriction_cases():
            with self.subTest(label=label):
                result = subprocess.run([str(REAL_BINARY), "moves", "--fen", board.fen(en_passant="fen")],
                                        capture_output=True, text=True, timeout=5, check=True)
                legal = result.stdout.splitlines()[2].split()
                for move, allowed in moves:
                    self.assertEqual(move in legal, allowed, (label, move, result.stdout))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path, nargs="?")
    args, rest = parser.parse_known_args()
    REAL_BINARY = args.binary.resolve(strict=True) if args.binary else None
    unittest.main(argv=[str(Path(__file__)), *rest], verbosity=2)
