"""Completed-position auto draws, claim boundaries and archive compatibility."""
from pathlib import Path
import copy
import io
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import chess
import chess.pgn
from game_archive import GameArchive
from game_outcome import automatic_outcome, DRAW_POLICY, LEGACY_DRAW_POLICY

CYCLE = ["g1f3", "g8f6", "f3g1", "f6g8"]


def replay(moves, fen=chess.STARTING_FEN):
    board = chess.Board(fen)
    for uci in moves:
        board.push_uci(uci)
    return board


class AutomaticOutcomeTests(unittest.TestCase):
    def test_threefold_waits_until_position_is_actually_reached(self):
        board = replay((CYCLE * 2)[:7])
        self.assertTrue(board.can_claim_threefold_repetition())
        self.assertFalse(board.is_repetition(3))
        self.assertIsNone(automatic_outcome(board))
        board.push_uci("f6g8")
        self.assertEqual(automatic_outcome(board), chess.Outcome(chess.Termination.THREEFOLD_REPETITION, None))

    def test_claim_can_be_declined_by_playing_an_alternative(self):
        board = replay((CYCLE * 2)[:7])
        board.push_uci("e7e5")
        self.assertFalse(board.can_claim_threefold_repetition())
        self.assertIsNone(automatic_outcome(board))

    def test_compulsory_fivefold_has_priority_after_actual_fifth_occurrence(self):
        board = replay((CYCLE * 4)[:15])
        self.assertIsNone(board.outcome(claim_draw=False))
        board.push_uci("f6g8")
        self.assertEqual(automatic_outcome(board).termination, chess.Termination.FIVEFOLD_REPETITION)

    def test_castling_rights_distinguish_same_piece_placement(self):
        fen = "4k1n1/8/8/8/8/8/8/4K1NR w K - 0 1"
        cycle = ["h1h2", "g8f6", "h2h1", "f6g8"]
        board = replay(cycle * 2, fen)
        self.assertEqual(board.board_fen(), chess.Board(fen).board_fen())
        self.assertFalse(board.has_kingside_castling_rights(chess.WHITE))
        self.assertTrue(board.is_repetition(2))
        self.assertIsNone(automatic_outcome(board))
        for uci in cycle:
            board.push_uci(uci)
        self.assertEqual(automatic_outcome(board).termination, chess.Termination.THREEFOLD_REPETITION)

    def test_only_legal_en_passant_opportunity_distinguishes_positions(self):
        examples = [
            ("4k1n1/8/8/3pP3/8/8/8/4K1NR w K d6 0 1", True, False),
            ("4k1n1/8/8/3p4/8/8/8/4K1NR w K d6 0 1", False, True),
            ("k3r1n1/8/8/3pP3/8/8/8/4K1NR w K d6 0 1", False, True),
        ]
        for fen, legal_ep, threefold in examples:
            with self.subTest(fen=fen):
                self.assertEqual(chess.Board(fen).has_legal_en_passant(), legal_ep)
                board = replay(CYCLE * 2, fen)
                self.assertEqual(board.is_repetition(3), threefold)
                self.assertEqual(automatic_outcome(board) is not None, threefold)

    def test_opposite_turn_does_not_count_same_piece_placement(self):
        fen = "7k/8/8/8/3R4/8/8/K7 w - - 0 1"
        board = replay(["a1b1", "h8g8", "b1b2", "g8h8", "b2a1"], fen)
        self.assertEqual(board.board_fen(), chess.Board(fen).board_fen())
        self.assertNotEqual(board.turn, chess.Board(fen).turn)
        self.assertFalse(board.is_repetition(2))
        self.assertIsNone(automatic_outcome(board))

    def test_fifty_moves_does_not_end_before_hundredth_halfmove(self):
        board = chess.Board("8/8/8/8/8/2k5/8/R5K1 w - - 99 51")
        self.assertTrue(board.can_claim_fifty_moves())
        self.assertIsNone(automatic_outcome(board))
        board.push_uci("a1a2")
        self.assertEqual(automatic_outcome(board).termination, chess.Termination.FIFTY_MOVES)

    def test_pawn_and_capture_can_reset_anticipated_fifty_move_condition(self):
        for fen, move in [
            ("8/8/8/8/8/2k5/4P3/R5K1 w - - 99 51", "e2e4"),
            ("8/8/8/8/8/2k5/r7/R5K1 w - - 99 51", "a1a2"),
        ]:
            with self.subTest(move=move):
                board = chess.Board(fen)
                self.assertTrue(board.can_claim_fifty_moves())
                self.assertIsNone(automatic_outcome(board))
                board.push_uci(move)
                self.assertEqual(board.halfmove_clock, 0)
                self.assertIsNone(automatic_outcome(board))

    def test_seventyfive_moves_is_compulsory_only_at_150_halfmoves(self):
        board = chess.Board("8/8/8/8/8/2k5/8/R5K1 w - - 149 76")
        self.assertIsNone(board.outcome(claim_draw=False))
        self.assertEqual(automatic_outcome(board).termination, chess.Termination.FIFTY_MOVES)
        board.push_uci("a1a2")
        self.assertEqual(automatic_outcome(board).termination, chess.Termination.SEVENTYFIVE_MOVES)

    def test_checkmate_precedes_draw_threshold(self):
        board = replay(["g6g7"], "7k/8/5KQ1/8/8/8/8/8 w - - 149 75")
        self.assertEqual(board.halfmove_clock, 150)
        self.assertEqual(automatic_outcome(board), chess.Outcome(chess.Termination.CHECKMATE, chess.WHITE))

    def test_full_history_is_preserved_and_fen_alone_is_not_repetition(self):
        board = replay(CYCLE * 2)
        before = (board.fen(en_passant="fen"), list(board.move_stack))
        self.assertEqual(automatic_outcome(board).termination, chess.Termination.THREEFOLD_REPETITION)
        self.assertEqual((board.fen(en_passant="fen"), list(board.move_stack)), before)
        self.assertIsNone(automatic_outcome(chess.Board(board.fen())))


class ArchiveDrawPolicyTests(unittest.TestCase):
    def record(self, plies=7, policy=DRAW_POLICY, result="*", termination=None):
        record = {"game_id": "0123456789abcdef0123456789abcdef",
                  "uci_moves": (CYCLE * 2)[:plies], "draw_policy": policy,
                  "state": {"result": result, "termination": termination, "human_color": "white"}}
        return record

    def prepare(self, record):
        return GameArchive._prepare(record, record["game_id"])

    def test_unfinished_records_are_not_upgraded_by_intended_repetition(self):
        for policy in (DRAW_POLICY, LEGACY_DRAW_POLICY, None):
            with self.subTest(policy=policy):
                record = self.record(policy=policy)
                if policy is None:
                    record.pop("draw_policy")
                before = copy.deepcopy(record)
                prepared = self.prepare(record)
                self.assertEqual(prepared["result"], "*")
                self.assertIsNone(prepared["termination"])
                self.assertEqual(record, before)

    def test_actual_third_occurrence_is_terminal_for_all_archive_versions(self):
        for policy in (DRAW_POLICY, LEGACY_DRAW_POLICY):
            with self.subTest(policy=policy):
                prepared = self.prepare(self.record(plies=8, policy=policy))
                self.assertEqual(prepared["result"], "1/2-1/2")
                self.assertEqual(prepared["termination"], "threefold_repetition")

    def test_explicit_legacy_claim_results_remain_viewable(self):
        for policy in (LEGACY_DRAW_POLICY, None):
            with self.subTest(policy=policy):
                record = self.record(policy=policy, result="1/2-1/2", termination="threefold_repetition")
                if policy is None:
                    record.pop("draw_policy")
                prepared = self.prepare(record)
                self.assertEqual(prepared["result"], "1/2-1/2")
                self.assertEqual(prepared["termination"], "threefold_repetition")

    def test_new_policy_rejects_unsupported_early_draw_result(self):
        with self.assertRaises(ValueError):
            self.prepare(self.record(result="1/2-1/2", termination="threefold_repetition"))

    def test_legacy_claim_requires_verifiable_claim_and_matching_reason(self):
        for record in (
            self.record(plies=6, policy=LEGACY_DRAW_POLICY, result="1/2-1/2", termination="threefold_repetition"),
            self.record(policy=LEGACY_DRAW_POLICY, result="1/2-1/2", termination="fifty_moves"),
        ):
            with self.subTest(record=record), self.assertRaises(ValueError):
                self.prepare(record)

    def test_legacy_fifty_move_result_preserved_but_unfinished_remains_open(self):
        record = self.record(plies=0, policy=LEGACY_DRAW_POLICY)
        record["initial_fen"] = "8/8/8/8/8/2k5/8/R5K1 w - - 99 51"
        self.assertEqual(self.prepare(record)["result"], "*")
        record["state"].update(result="1/2-1/2", termination="fifty_moves")
        self.assertEqual(self.prepare(record)["result"], "1/2-1/2")

    def test_pgn_result_and_policy_match_actual_repetition_history(self):
        for plies, result in ((7, "*"), (8, "1/2-1/2")):
            with self.subTest(plies=plies):
                text = GameArchive._pgn(self.prepare(self.record(plies=plies)))
                game = chess.pgn.read_game(io.StringIO(text))
                self.assertFalse(game.errors)
                self.assertEqual(game.headers["Result"], result)
                self.assertEqual(game.headers["DrawPolicy"], DRAW_POLICY)
                self.assertEqual(len(game.end().board().move_stack), plies)

    def test_legacy_pgn_retains_explicit_historical_policy(self):
        record = self.record(policy=LEGACY_DRAW_POLICY, result="1/2-1/2", termination="threefold_repetition")
        game = chess.pgn.read_game(io.StringIO(GameArchive._pgn(self.prepare(record))))
        self.assertEqual(game.headers["Result"], "1/2-1/2")
        self.assertEqual(game.headers["DrawPolicy"], LEGACY_DRAW_POLICY)
        self.assertEqual(len(game.end().board().move_stack), 7)


if __name__ == "__main__":
    unittest.main()
