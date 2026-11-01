"""Standard-opening castling and actionable, read-only rule diagnostics.

Usage: python tests/analysis_castling_workflow_tests.py [path/to/regicide]
The real-engine case starts from the standard position and clears the actual
paths through legal opening moves rather than importing an open castle FEN.
"""

import argparse
from pathlib import Path
import tempfile
import unittest

import chess
import chess.engine

import human_play_tests as baseline
from hint_state_tests import HintFactory, wait_for
from analysis_castling_tests import open_fen, restriction_cases
from analysis_session import AnalysisSession

REAL_BINARY = None
OPENING = ("e2e4", "e7e5", "g1f3", "b8c6", "f1c4", "g8f6")


class CastlingWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="regicide-castling-workflow-")
        self.root = Path(self.folder.name)
        self.binary = self.root / "fake-uci.exe"
        self.binary.write_bytes(b"fake engine, not executed")
        self.analysis = AnalysisSession(self.binary, engine_factory=HintFactory())
        self.addCleanup(self.folder.cleanup)
        self.addCleanup(self.analysis.close)

    def start(self, fen=chess.STARTING_FEN):
        return self.analysis.start(fen, 100, self.analysis.revision)

    def move(self, move):
        return self.analysis.move(move, self.analysis.revision)

    def test_inactive_diagnostics_and_nested_snapshots_are_read_only(self):
        state = self.analysis.snapshot()
        self.assertEqual(state["initial_fen"], chess.STARTING_FEN)
        for side, move in (("king", "e1g1"), ("queen", "e1c1")):
            self.assertEqual(state["castling"][side]["move"], move)
            self.assertFalse(state["castling"][side]["available"])
            self.assertEqual(state["castling"][side]["reason_code"], "editing")
            self.assertTrue(state["castling"][side]["reason"])
        state["castling"]["king"]["available"] = True
        self.assertFalse(self.analysis.snapshot()["castling"]["king"]["available"])

    def test_standard_opening_keeps_rights_and_allows_both_sides_to_castle(self):
        state = self.start()
        for move in OPENING:
            state = self.move(move)
            self.assertEqual(state["fen"].split()[2], "KQkq")
            self.assertEqual(state["initial_fen"], chess.STARTING_FEN)
        self.assertTrue(state["castling"]["king"]["available"])
        self.assertEqual(state["castling"]["king"]["reason_code"], "available")
        self.assertEqual(state["castling"]["king"]["move"], "e1g1")
        before_white = state["fen"]
        state = self.move("e1g1")
        self.assertEqual(state["fen"].split()[2], "kq")
        state = self.analysis.undo(state["revision"])
        self.assertEqual(state["fen"], before_white)
        self.assertTrue(state["castling"]["king"]["available"])
        self.move("e1g1")
        self.move("f8c5")
        state = self.move("d2d3")
        self.assertEqual(state["fen"].split()[2], "kq")
        self.assertEqual(state["castling"]["king"]["move"], "e8g8")
        self.assertTrue(state["castling"]["king"]["available"])
        before_black = state["fen"]
        state = self.move("e8g8")
        self.assertEqual(state["fen"].split()[2], "-")
        state = self.analysis.undo(state["revision"])
        self.assertEqual(state["fen"], before_black)
        self.assertTrue(state["castling"]["king"]["available"])
        self.assertEqual(state["initial_fen"], chess.STARTING_FEN)

    def test_blocking_squares_are_named_at_standard_start(self):
        state = self.start()
        self.assertEqual(state["castling"]["king"]["reason_code"], "occupied_path")
        for square in ("f1", "g1"):
            self.assertIn(square, state["castling"]["king"]["reason"])
        for square in ("b1", "c1", "d1"):
            self.assertIn(square, state["castling"]["queen"]["reason"])

    def test_failure_diagnostics_match_oracle_for_both_colors(self):
        expected_codes = {"rights missing": "no_rights", "king currently checked": "in_check",
                          "king-side transit attacked": "attacked_path", "king-side landing attacked": "attacked_path",
                          "queen-side transit attacked": "attacked_path", "king-side blocked": "occupied_path",
                          "queen-side rook path blocked": "occupied_path"}
        for label, board, *moves in restriction_cases():
            with self.subTest(label=label):
                state = self.start(board.fen(en_passant="fen"))
                for side, (move, expected_available) in zip(("king", "queen"), moves):
                    diagnostic = state["castling"][side]
                    self.assertEqual(diagnostic["move"], move)
                    self.assertEqual(diagnostic["available"], expected_available)
                    self.assertEqual(diagnostic["available"], move in state["legal_moves"])
                    self.assertTrue(diagnostic["reason"])
                    if expected_available:
                        self.assertEqual(diagnostic["reason_code"], "available")
                    else:
                        prefix = label.rsplit(" ", 1)[0]
                        self.assertEqual(diagnostic["reason_code"], expected_codes[prefix])

    def test_attacked_b_file_is_not_a_queenside_restriction(self):
        for _, board, *moves in restriction_cases():
            if not board.is_attacked_by(not board.turn, chess.B1 if board.turn else chess.B8):
                continue
            # Only the otherwise-clear fixture is relevant; the occupied-path
            # fixture remains blocked even if another attacker also exists.
            if moves[1][1]:
                state = self.start(board.fen(en_passant="fen"))
                self.assertTrue(state["castling"]["queen"]["available"])
                self.assertEqual(state["castling"]["queen"]["reason_code"], "available")

    def test_missing_initial_rights_are_not_granted_by_legal_opening(self):
        no_rights = chess.STARTING_FEN.replace(" KQkq ", " - ")
        state = self.start(no_rights)
        for move in OPENING:
            state = self.move(move)
            self.assertEqual(state["fen"].split()[2], "-")
        self.assertNotIn("e1g1", state["legal_moves"])
        self.assertEqual(state["castling"]["king"]["reason_code"], "no_rights")
        self.assertIn("시작 배치", state["castling"]["king"]["reason"])
        self.assertEqual(state["initial_fen"], no_rights)

    def test_moved_king_or_rook_explains_lost_rights_and_undo_restores(self):
        state = self.start(open_fen())
        for move in ("h1h2", "e8e7", "h2h1", "e7e8"):
            state = self.move(move)
        self.assertEqual(state["castling"]["king"]["reason_code"], "rights_lost")
        self.assertIn("움직", state["castling"]["king"]["reason"])
        self.assertTrue(state["castling"]["queen"]["available"])
        for _ in range(4):
            state = self.analysis.undo(state["revision"])
        self.assertEqual(state["fen"], open_fen())
        self.assertTrue(state["castling"]["king"]["available"])
        self.assertTrue(state["castling"]["queen"]["available"])

    def test_terminal_and_stopped_positions_explain_unavailability(self):
        for fen in ("7k/6Q1/5K2/8/8/8/8/8 b - - 0 1", "4k3/8/8/8/8/8/8/4K3 w - - 0 1"):
            state = self.start(fen)
            for side in ("king", "queen"):
                self.assertEqual(state["castling"][side]["reason_code"], "terminal")
                self.assertFalse(state["castling"][side]["available"])
            state = self.analysis.stop(state["revision"])
            self.assertEqual(state["castling"]["king"]["reason_code"], "editing")

    def test_diagnostic_reads_preserve_live_game_and_archive(self):
        game = baseline.play_human.HumanGame(self.binary, self.root / "games",
                                             baseline.FakeFactory(), HintFactory())
        self.addCleanup(game.close)
        original = game.state()
        files = {path.name: path.read_bytes() for path in game.save_dir.iterdir()}
        game.analysis.start(chess.STARTING_FEN, 100, game.analysis.revision)
        for move in OPENING:
            game.analysis.move(move, game.analysis.revision)
            for _ in range(3):
                snapshot = game.analysis.snapshot()
                self.assertEqual(snapshot["initial_fen"], chess.STARTING_FEN)
        self.assertEqual(game.state(), original)
        self.assertEqual({path.name: path.read_bytes() for path in game.save_dir.iterdir()}, files)


class RealCastlingWorkflowTests(unittest.TestCase):
    def test_actual_engine_and_standard_opening_support_both_castles(self):
        if REAL_BINARY is None:
            self.skipTest("pass a Regicide binary for real standard-opening workflow")
        analysis = AnalysisSession(REAL_BINARY)
        self.addCleanup(analysis.close)
        state = analysis.start(chess.STARTING_FEN, 100, analysis.revision)
        with chess.engine.SimpleEngine.popen_uci([str(REAL_BINARY), "uci"], timeout=3) as engine:
            for move in (*OPENING, "e1g1", "f8c5", "d2d3", "e8g8"):
                with self.subTest(move=move):
                    self.assertIn(move, state["legal_moves"])
                    self.assertEqual(state["initial_fen"], chess.STARTING_FEN)
                    before = state["fen"]
                    if move in ("e1g1", "e8g8"):
                        self.assertTrue(state["castling"]["king"]["available"])
                        reply = engine.play(chess.Board(before), chess.engine.Limit(depth=2),
                                            root_moves=[chess.Move.from_uci(move)])
                        self.assertEqual(reply.move.uci(), move)
                    state = analysis.move(move, state["revision"])
                    if move in ("e1g1", "e8g8"):
                        state = analysis.undo(state["revision"])
                        self.assertEqual(state["fen"], before)
                        self.assertTrue(state["castling"]["king"]["available"])
                        state = analysis.move(move, state["revision"])
            wait_for(lambda: analysis.snapshot()["hint"]["status"] == "ready", timeout=8)
            self.assertEqual(state["initial_fen"], chess.STARTING_FEN)
            self.assertEqual(state["fen"].split()[2], "-")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path, nargs="?")
    args, rest = parser.parse_known_args()
    REAL_BINARY = args.binary.resolve(strict=True) if args.binary else None
    unittest.main(argv=[str(Path(__file__)), *rest], verbosity=2)
