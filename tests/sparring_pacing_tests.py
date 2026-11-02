"""Actual recorded exchanges and explicit small-ending protection fixtures."""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import zipfile

import chess

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import sparring_pacing as pacing


def recorded(game, ply):
    shard = "ratings-1320-1700" if game <= 4 else "ratings-2100-2500"
    with zipfile.ZipFile(ROOT / "docs/matches/2026-11-02-sparring/raw-records.zip") as archive:
        source = json.loads(archive.read(f"{shard}/game-{game:03d}.json"))
    board = chess.Board()
    for move in source["uci_moves"][:ply]:
        board.push_uci(move)
    return board, source


class PacingTests(unittest.TestCase):
    def test_actual_rook_loss_recovers_over_four_bot_turns(self):
        targets = []
        for stage, ply in enumerate((36, 38, 40, 42), 1):
            board, _ = recorded(1, ply)
            plan = pacing.derive(board, [], 500)
            self.assertTrue(plan["active"])
            self.assertEqual((plan["event_ply"], plan["stage"]), (35, stage))
            self.assertEqual(plan["allow_material_return"], stage >= 3)
            targets.append(plan["target_cp"])
        self.assertEqual(targets[-1], 30)
        self.assertEqual(targets, sorted(targets, reverse=True))
        board, _ = recorded(1, 44)
        self.assertFalse(pacing.derive(board, [], 500)["active"])

    def test_equal_rook_and_long_queen_rook_exchange_are_not_blunders(self):
        # g3 p36..41 trades pawns, two queens and two rooks: net zero.
        # Looking only at p39..41 would incorrectly report a +500cp gift.
        for game, ply in ((3, 42), (7, 66)):
            board, _ = recorded(game, ply)
            self.assertFalse(pacing.derive(board, [], 500)["active"])

    def test_capture_on_last_practice_ply_is_included_not_the_next_ply(self):
        board, _ = recorded(1, 36)
        with patch.object(pacing, "CLOSE_PLIES", 35):
            self.assertTrue(pacing.derive(board, [], 500)["active"])
        with patch.object(pacing, "CLOSE_PLIES", 34):
            self.assertFalse(pacing.derive(board, [], 500)["active"])

    def test_black_mirror_has_same_material_and_delay(self):
        original, _ = recorded(1, 40)
        mirror = original.root().mirror()
        for move in original.move_stack:
            mirror.push(chess.Move(chess.square_mirror(move.from_square),
                                   chess.square_mirror(move.to_square), promotion=move.promotion))
        self.assertTrue(mirror.is_valid())
        white = pacing.derive(original, [], 500)
        black = pacing.derive(mirror, [], 500)
        for key in ("active", "event_ply", "stage", "debt_cp", "target_cp", "allow_material_return"):
            self.assertEqual(white[key], black[key])

    def test_undo_and_stale_anchor_do_not_keep_future_loss(self):
        board, source = recorded(1, 36)
        plan = pacing.derive(board, [], 500)
        row = {"ply": 37, "history_uci": source["uci_moves"][:36],
               "before_fen": board.fen(en_passant="fen"), "selected_uci": source["uci_moves"][36],
               "pacing": plan}
        later, _ = recorded(1, 38)
        valid = pacing.derive(later, [row], 900)
        self.assertEqual(valid["anchor_cp"], 500)
        bad = copy.deepcopy(row)
        bad["history_uci"][0] = "a2a3"
        self.assertEqual(pacing.derive(later, [bad], 900)["anchor_cp"], 900)
        board.pop()
        board.pop()
        self.assertFalse(pacing.derive(board, [row], 500)["active"])

    def test_ramp_fades_temporary_allowance_without_changing_nominal_band(self):
        board, _ = recorded(1, 38)
        normal = pacing.derive(board, [], 500)
        fading = pacing.derive(board, [], 500, ramp_fraction=.5)
        self.assertLess(fading["upper_cp"], normal["upper_cp"])
        self.assertGreaterEqual(fading["upper_cp"], 100)

    def test_partial_material_return_keeps_remaining_recovery_window(self):
        board, _ = recorded(1, 36)
        original = pacing.derive(board, [], 500)
        initial_balance = pacing._balance(board, board.turn) - original["debt_cp"]
        candidate = None
        for move in list(board.legal_moves):
            child = board.copy(stack=True)
            child.push(move)
            for reply in list(child.legal_moves):
                if not child.is_capture(reply):
                    continue
                after = child.copy(stack=True)
                after.push(reply)
                remaining = pacing._balance(after, board.turn) - initial_balance
                if 0 < remaining < 400 and after.outcome(claim_draw=True) is None:
                    candidate = after
                    break
            if candidate is not None:
                break
        self.assertIsNotNone(candidate)
        plan = pacing.derive(candidate, [], 300)
        self.assertTrue(plan["active"])
        self.assertEqual(plan["stage"], 2)
        self.assertLess(plan["debt_cp"], 400)

    def test_caution_keeps_practice_active_and_counts_all_pawns(self):
        # Queenless rook+knight each, with eight pawns: no full shutdown.
        board = chess.Board("r5nk/pppp4/8/8/8/8/PPPP4/R5NK w - - 0 1")
        guard = pacing.resource_guard(board)
        self.assertFalse(guard["active"])
        self.assertTrue(guard["material_concessions_blocked"])
        self.assertEqual((guard["nonpawn_cp_total"], guard["pawns_total"]), (1600, 8))
        self.assertFalse(pacing.resource_guard(chess.Board())["material_concessions_blocked"])

    def test_critical_endings_stop_concessions_not_adjudicate_draws(self):
        for fen, reason in (("7k/7p/8/8/8/8/P7/K7 w - - 0 1", "pawn_endgame"),
                            ("r6k/7p/8/8/8/8/P7/R6K w - - 0 1", "simple_low_pawn_endgame")):
            board = chess.Board(fen)
            self.assertIsNone(board.outcome())
            guard = pacing.resource_guard(board)
            self.assertTrue(guard["active"])
            self.assertEqual(guard["reason"], reason)
            self.assertNotIn("result", guard)

    def test_promotion_does_not_restart_already_stopped_practice(self):
        board = chess.Board("7k/P7/8/8/8/8/8/7K w - - 0 1")
        board.push_uci("a7a8q")
        guard = pacing.resource_guard(board)
        self.assertTrue(guard["active"])
        self.assertEqual(guard["trigger_ply"], 0)

    def test_own_promotion_after_equal_exchange_is_not_opponent_major_loss(self):
        board = chess.Board("q6k/1P6/8/8/r7/8/8/R6K w - - 0 1")
        for uci in ("a1a4", "a8a4", "b7b8q", "h8h7"):
            board.push_uci(uci)
        self.assertFalse(pacing.derive(board, [], 500)["active"])

    def test_opponent_promotion_compensation_prevents_false_major_loss(self):
        board = chess.Board("7k/3q4/8/8/8/8/p7/3R3K w - - 0 1")
        for uci in ("d1d7", "a2a1q", "h1h2", "h8g8"):
            board.push_uci(uci)
        self.assertFalse(pacing.derive(board, [], 500)["active"])


if __name__ == "__main__":
    unittest.main()
