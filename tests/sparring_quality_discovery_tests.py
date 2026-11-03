"""Bounded native piece diversity independent of the referee's move/PV."""
import unittest
from unittest import mock

import chess

from sparring_quality_policy_tests import assessment, native_reply
from sparring_quality_mitigation_tests import recorded_move21
import sparring_quality as quality


def move33_board():
    later = recorded_move21()
    board = later.root()
    for move in later.move_stack[:32]:
        board.push(move)
    return board


def mirrored_move(move):
    return chess.Move(chess.square_mirror(move.from_square), chess.square_mirror(move.to_square),
                      promotion=move.promotion)


def mirrored_history(board):
    result = board.root().mirror()
    for move in board.move_stack:
        result.push(mirrored_move(move))
    return result


@mock.patch.object(quality, "MAX_ROOK_PROBES", 0)
class QualityDiscoveryTests(unittest.TestCase):
    def run_diversity(self, *, black=False, succeed=True, foreign=False):
        board = move33_board()
        original, bishop, king, good = map(chess.Move.from_uci, ("f2f4", "g2d5", "b1a1", "c3d5"))
        if black:
            board = mirrored_history(board)
            original, bishop, king, good = map(mirrored_move, (original, bishop, king, good))
        calls, assessed = [], []
        def search(amount, *, root_moves, **options):
            index = len(calls)
            calls.append((amount, list(root_moves)))
            if index == 0:
                chosen = bishop
            elif index == 1:
                chosen = king
            elif index == 2:
                chosen = original if foreign else good
            else:
                chosen = root_moves[0]
            return native_reply(board, chosen)
        def assess(move, amount, cancelled):
            assessed.append(move)
            value = assessment(board, move, 0, 0 if move == good and succeed else -400)
            # Deliberately unrelated reference PV cannot steer native groups.
            value["root"]["uci"] = "a7a6" if black else "a2a3"
            return value
        replacement, receipt, discarded = quality.verify(board, original, search, lambda: None, assess)
        return board, original, good, calls, assessed, replacement, receipt, discarded

    def test_generic_unrepresented_knight_group_finds_recorded_nd5(self):
        board, _original, good, calls, _assessed, replacement, receipt, discarded = self.run_diversity()
        self.assertFalse(discarded)
        self.assertEqual(replacement[0].move, good)
        self.assertEqual(receipt["status"], "replaced")
        self.assertEqual([attempt["kind"] for attempt in receipt["attempts"]],
                         ["global", "global", "minor_origin"])
        attempt = receipt["attempts"][2]
        self.assertEqual(attempt["origin"], "c3")
        expected = [move for move in board.legal_moves if move.from_square == chess.C3]
        self.assertEqual(calls[2][1], expected)
        self.assertEqual(attempt["allowed_uci"], [move.uci() for move in expected])
        self.assertEqual(calls[2][0], 400)
        self.assertEqual(attempt["native_evidence"]["root"], good.uci())

    def test_black_mirroring_preserves_history_and_origin_order(self):
        board, _original, good, calls, _assessed, replacement, receipt, _discarded = self.run_diversity(black=True)
        self.assertEqual(len(board.move_stack), 32)
        self.assertEqual(board.turn, chess.BLACK)
        self.assertEqual(replacement[0].move, good)
        self.assertEqual(receipt["attempts"][2]["origin"], "c6")
        self.assertTrue(all(move.from_square == chess.C6 for move in calls[2][1]))

    def test_original_rejected_minor_origin_is_already_represented(self):
        board = chess.Board("2r5/B1p1k3/2Bp4/PR2b3/2P3p1/2P2pP1/5P1r/1K6 b - - 0 43")
        original = chess.Move.from_uci("e5c3")
        calls = []
        def search(amount, *, root_moves, **options):
            calls.append(amount)
            return native_reply(board, root_moves[0])
        def assess(move, amount, cancelled):
            return assessment(board, move, 0, -400)
        replacement, receipt, _discarded = quality.verify(board, original, search, lambda: None, assess)
        self.assertIsNone(replacement)
        self.assertEqual([attempt["kind"] for attempt in receipt["attempts"]], ["global"] * 4)
        self.assertEqual(calls, [400, 400, 2000, 500])

    def test_two_group_cap_and_all_excluded_moves_are_preserved(self):
        board, original, _good, calls, _assessed, replacement, receipt, _discarded = self.run_diversity(succeed=False)
        self.assertIsNone(replacement)
        self.assertEqual([attempt["origin"] for attempt in receipt["attempts"] if attempt["kind"] == "minor_origin"],
                         ["c3", "g5"])
        self.assertEqual(sum(attempt["kind"] == "global" for attempt in receipt["attempts"]), 4)
        self.assertEqual(len(calls), 6)
        for (amount, allowed), attempt in zip(calls, receipt["attempts"]):
            excluded = set(attempt["excluded_uci"])
            self.assertIn(original.uci(), excluded)
            self.assertEqual(attempt["allowed_uci"], [move.uci() for move in allowed])
            self.assertTrue(all(move in board.legal_moves and move.uci() not in excluded for move in allowed))
            self.assertLessEqual(amount, 500)

    def test_group_cannot_return_foreign_native_root(self):
        _board, _original, _good, _calls, assessed, replacement, receipt, _discarded = self.run_diversity(foreign=True)
        self.assertIsNone(replacement)
        self.assertEqual(receipt["reason"], "invalid_native_candidate")
        self.assertEqual(len(assessed), 3)

    def test_insufficient_remaining_budget_skips_extra_groups_without_claiming_success(self):
        board = move33_board()
        original, bishop, king = map(chess.Move.from_uci, ("f2f4", "g2d5", "b1a1"))
        clock, calls, assessments = [100.0], [], []
        def search(amount, *, root_moves, **options):
            calls.append(list(root_moves))
            return native_reply(board, bishop if len(calls) == 1 else king)
        def assess(move, amount, cancelled):
            assessments.append(move)
            if len(assessments) == 3:
                clock[0] = 107.25
            return assessment(board, move, 0, -400)
        with mock.patch.object(quality.time, "monotonic", side_effect=lambda: clock[0]):
            replacement, receipt, _discarded = quality.verify(board, original, search, lambda: None, assess)
        self.assertIsNone(replacement)
        self.assertEqual(receipt["status"], "unverified")
        self.assertEqual(len(calls), 2)
        self.assertLess(receipt["elapsed_ms"], 8000)


if __name__ == "__main__":
    unittest.main()
