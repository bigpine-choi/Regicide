"""Free-analysis position/history checks, independent of the live UCI game.

Run with ``python tests/analysis_session_tests.py``. These tests exercise chess
rules supplied by the existing Python host, not the engine's move selector.
"""

import io
from pathlib import Path
import sys
import unittest

import chess
import chess.pgn


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from analysis_session import AnalysisRevisionError, AnalysisSession


class AnalysisSessionTests(unittest.TestCase):
    def setUp(self):
        self.session = AnalysisSession()

    def play(self, move):
        return self.session.move(move, self.session.state()["revision"])

    def undo(self):
        return self.session.undo(self.session.state()["revision"])

    def redo(self):
        return self.session.redo(self.session.state()["revision"])

    def assert_preserved_error(self, error, operation):
        before = self.session.state()
        with self.assertRaises(error):
            operation()
        self.assertEqual(self.session.state(), before)

    def test_initial_position_allows_user_to_move_either_colour(self):
        state = self.session.state()
        self.assertTrue(state["analysis_mode"])
        self.assertTrue(state["game_id"].startswith("analysis-"))
        self.assertEqual(state["fen"], chess.STARTING_FEN)
        self.assertEqual(state["turn"], "white")
        self.assertTrue(state["human_to_move"])
        self.assertFalse(state["engine_to_move"])
        self.assertFalse(state["can_undo"])
        self.assertFalse(state["can_redo"])
        self.assertFalse(state["handicap_mode"])
        self.assertFalse(state["can_handicap"])
        self.assertEqual(state["board"]["e1"], "K")
        self.assertEqual(state["board"]["e8"], "k")
        self.assertEqual(set(state["legal_moves"]),
                         {move.uci() for move in chess.Board().legal_moves})

        after_white = self.play("e2e4")
        self.assertEqual(after_white["turn"], "black")
        self.assertTrue(after_white["human_to_move"])
        self.assertFalse(after_white["engine_to_move"])
        self.assertIn("e7e5", after_white["legal_moves"])
        after_black = self.play("e7e5")
        self.assertEqual(after_black["turn"], "white")
        self.assertEqual([entry["san"] for entry in after_black["moves"]], ["e4", "e5"])
        self.assertEqual([entry["actor"] for entry in after_black["moves"]],
                         ["analysis", "analysis"])
        self.assertEqual(after_black["last_move"], "e7e5")

    def test_undo_redo_restores_exact_position_in_stack_order(self):
        positions = [self.session.state()["fen"]]
        for move in ("e2e4", "e7e5", "g1f3"):
            positions.append(self.play(move)["fen"])
        original_moves = self.session.state()["moves"]
        revisions = [self.session.state()["revision"]]
        for position in reversed(positions[:-1]):
            state = self.undo()
            self.assertEqual(state["fen"], position)
            self.assertTrue(state["can_redo"])
            self.assertGreater(state["revision"], revisions[-1])
            revisions.append(state["revision"])
        self.assertFalse(self.session.state()["can_undo"])
        for position in positions[1:]:
            state = self.redo()
            self.assertEqual(state["fen"], position)
        self.assertFalse(self.session.state()["can_redo"])
        self.assertEqual(self.session.state()["moves"], original_moves)

    def test_new_branch_discards_only_old_redo_line(self):
        self.play("e2e4")
        self.play("e7e5")
        self.play("g1f3")
        self.undo()
        self.undo()
        state = self.play("c7c5")
        self.assertEqual([move["uci"] for move in state["moves"]], ["e2e4", "c7c5"])
        self.assertFalse(state["can_redo"])
        self.assert_preserved_error(ValueError, self.redo)

    def test_invalid_move_does_not_discard_redo(self):
        self.play("e2e4")
        expected = self.play("e7e5")["fen"]
        self.undo()
        self.assert_preserved_error(ValueError, lambda: self.play("e7e4"))
        self.assertTrue(self.session.state()["can_redo"])
        self.assertEqual(self.redo()["fen"], expected)

    def test_empty_history_undo_and_redo_preserve_position(self):
        self.assert_preserved_error(ValueError, self.undo)
        self.assert_preserved_error(ValueError, self.redo)

    def test_stale_revisions_reject_every_mutation(self):
        stale = self.session.state()["revision"]
        self.play("e2e4")
        for operation in (
                lambda: self.session.move("e7e5", stale),
                lambda: self.session.undo(stale),
                lambda: self.session.redo(stale),
                lambda: self.session.reset(stale)):
            with self.subTest(operation=operation):
                self.assert_preserved_error(AnalysisRevisionError, operation)

    def test_invalid_move_and_revision_types_preserve_state(self):
        for value in (None, True, 12, [], {}, "", "e2e4xx", "e2e9", "0000", "e2e5", "e7e5"):
            with self.subTest(move=value):
                self.assert_preserved_error(ValueError, lambda: self.play(value))
        for value in (None, True, False, "1", 1.0, []):
            with self.subTest(revision=value):
                self.assert_preserved_error(ValueError,
                                            lambda: self.session.move("e2e4", value))

    def test_invalid_fen_does_not_replace_existing_analysis(self):
        self.play("e2e4")
        for value in (True, [], {}, 15, "", "not a fen", "8/8/8/8/8/8/8/8 w - - 0 1", "x" * 500):
            with self.subTest(fen=value):
                self.assert_preserved_error(ValueError, lambda: self.session.start(value))

    def test_start_replaces_session_and_clears_old_redo(self):
        self.play("e2e4")
        self.undo()
        old = self.session.state()
        fen = "7k/8/8/8/8/8/7p/5K2 b - - 4 28"
        state = self.session.start(fen)
        self.assertNotEqual(state["game_id"], old["game_id"])
        self.assertGreater(state["revision"], old["revision"])
        self.assertEqual(state["fen"], fen)
        self.assertEqual(state["turn"], "black")
        self.assertTrue(state["human_to_move"])
        self.assertEqual(state["moves"], [])
        self.assertFalse(state["can_undo"])
        self.assertFalse(state["can_redo"])

    def test_reset_returns_to_standard_start(self):
        self.session.start("7k/8/8/8/8/8/7p/5K2 b - - 4 28")
        self.play("h2h1q")
        old = self.session.state()
        state = self.session.reset(old["revision"])
        self.assertEqual(state["fen"], chess.STARTING_FEN)
        self.assertGreater(state["revision"], old["revision"])
        self.assertEqual(state["moves"], [])
        self.assertFalse(state["can_undo"])
        self.assertFalse(state["can_redo"])

    def test_castling_for_both_sides_and_reversal(self):
        self.session.start("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1")
        before = self.session.state()["fen"]
        white = self.play("e1g1")
        self.assertEqual(white["board"]["g1"], "K")
        self.assertEqual(white["board"]["f1"], "R")
        self.assertNotIn("h1", white["board"])
        black = self.play("e8c8")
        self.assertEqual(black["board"]["c8"], "k")
        self.assertEqual(black["board"]["d8"], "r")
        self.assertEqual([move["san"] for move in black["moves"]], ["O-O", "O-O-O"])
        self.undo()
        self.undo()
        self.assertEqual(self.session.state()["fen"], before)
        self.redo()
        self.assertEqual(self.redo()["fen"], black["fen"])

    def test_en_passant_capture_undo_and_redo_preserve_rights(self):
        for move in ("e2e4", "a7a6", "e4e5", "d7d5"):
            self.play(move)
        before = self.session.state()
        self.assertEqual(before["fen"].split()[3], "d6")
        self.assertIn("e5d6", before["legal_moves"])
        after = self.play("e5d6")
        self.assertEqual(after["board"]["d6"], "P")
        self.assertNotIn("d5", after["board"])
        self.assertEqual(self.undo()["fen"], before["fen"])
        self.assertEqual(self.redo()["fen"], after["fen"])

    def test_all_four_promotion_choices_and_missing_choice_rejection(self):
        fen = "7k/P7/8/8/8/8/8/7K w - - 0 1"
        for kind in "qrbn":
            with self.subTest(promotion=kind):
                self.session.start(fen)
                self.assert_preserved_error(ValueError, lambda: self.play("a7a8"))
                state = self.play("a7a8" + kind)
                self.assertEqual(state["board"]["a8"], kind.upper())
                self.assertNotIn("a7", state["board"])
                self.assertEqual(self.undo()["fen"], fen)
                self.assertEqual(self.redo()["fen"], state["fen"])

    def test_checkmate_and_stalemate_have_no_available_move(self):
        for fen in ("7k/6Q1/6K1/8/8/8/8/8 b - - 0 1",
                    "7k/5Q2/6K1/8/8/8/8/8 b - - 0 1"):
            with self.subTest(fen=fen):
                state = self.session.start(fen)
                self.assertEqual(state["legal_moves"], [])
                self.assertFalse(state["human_to_move"])
                self.assertFalse(state["engine_to_move"])
                self.assert_preserved_error(ValueError, lambda: self.play("h8h7"))

    def test_automatic_draw_does_not_block_free_exploration(self):
        state = self.session.start("7k/8/8/8/8/8/8/7K w - - 0 1")
        self.assertTrue(chess.Board(state["fen"]).is_insufficient_material())
        self.assertTrue(state["human_to_move"])
        self.assertEqual(self.play("h1g1")["board"]["g1"], "K")

    def test_returned_state_is_detached_from_internal_history(self):
        state = self.play("e2e4")
        before = self.session.state()
        state["board"].clear()
        state["legal_moves"].clear()
        state["moves"][0]["uci"] = "e2e5"
        state["moves"].append({"uci": "bad"})
        self.assertEqual(self.session.state(), before)

    def test_custom_black_start_pgn_roundtrip_preserves_setup_and_moves(self):
        fen = "7k/8/8/8/8/8/7p/5K2 b - - 4 28"
        self.session.start(fen)
        final = self.play("h2h1q")
        text = self.session.pgn()
        game = chess.pgn.read_game(io.StringIO(text))
        self.assertIsNotNone(game)
        self.assertEqual(game.errors, [])
        self.assertEqual(game.headers["FEN"], fen)
        self.assertEqual(game.headers["SetUp"], "1")
        self.assertIn("28...", text)
        board = game.board()
        for move in game.mainline_moves():
            self.assertIn(move, board.legal_moves)
            board.push(move)
        self.assertEqual(board.fen(en_passant="fen"), final["fen"])


if __name__ == "__main__":
    unittest.main()
