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
    def test_major_loss_recovery_must_start_before_each_sides_sixteenth_move(self):
        self.assertEqual(pacing.CLOSE_PLIES, 30)
        for color, capture_ply, included in ((chess.WHITE, 27, True), (chess.BLACK, 28, True),
                                             (chess.WHITE, 29, False), (chess.BLACK, 30, False),
                                             (chess.WHITE, 31, False), (chess.BLACK, 32, False)):
            fen = "1n5k/q7/8/8/8/8/8/RN5K w - - 0 1" if color else "rn5k/8/8/8/8/8/Q7/1N5K w - - 0 1"
            board = chess.Board(fen)
            cycle = ("b1c3", "b8c6", "c3b1", "c6b8")
            for ply in range(capture_ply - 1):
                board.push_uci(cycle[ply % 4])
            self.assertTrue(board.is_valid())
            self.assertEqual(board.turn, color)
            board.push_uci("a1a7" if color else "a8a2")
            board.push_uci("h8g8" if color else "h1g1")
            self.assertTrue(board.is_valid())
            plan = pacing.derive(board, [], 900)
            self.assertEqual(plan["active"], included, (color, capture_ply, plan))
            if included:
                self.assertEqual(plan["event_ply"], capture_ply)
                self.assertEqual(plan["stage"], 1)
            else:
                self.assertEqual(plan["reason"], "close_phase_finished")
                self.assertFalse(plan["allow_material_return"])

    def test_resource_guard_only_latches_through_completed_move_fifteen(self):
        for capture_ply in (30, 32):
            original = chess.Board("1n5k/q7/8/8/8/8/R7/1N5K w - - 0 1")
            for ply in range(capture_ply - 1):
                original.push_uci(("b1c3", "b8c6", "c3b1", "c6b8")[ply % 4])
            original.push_uci("a7a2")
            mirrored = original.root().mirror()
            for move in original.move_stack:
                mirrored.push(chess.Move(chess.square_mirror(move.from_square), chess.square_mirror(move.to_square)))
            for board in (original, mirrored):
                self.assertTrue(board.is_valid())
                guard = pacing.resource_guard(board)
                self.assertEqual(guard["active"], capture_ply == 30, (capture_ply, board.turn, guard))
                self.assertEqual(guard["trigger_ply"], 30 if capture_ply == 30 else None)
                self.assertTrue(guard["material_concessions_blocked"])

    def test_recorded_rook_loss_after_move15_no_longer_starts_or_carries_recovery(self):
        for ply in (36, 38, 40, 42, 44):
            board, _ = recorded(1, ply)
            plan = pacing.derive(board, [], 500)
            self.assertFalse(plan["active"])
            self.assertEqual(plan["reason"], "close_phase_finished")
            self.assertFalse(plan["allow_material_return"])
            self.assertEqual(plan["return_cap_cp"], 0)

    @patch.object(pacing, "CLOSE_PLIES", 80)
    def test_explicit_extended_window_keeps_recorded_four_stage_settlement(self):
        # Exercise the original recorded exchange mechanics under an explicit
        # longer test window; this is not the current 15-move runtime policy.
        targets = []
        for stage, ply in enumerate((36, 38, 40, 42), 1):
            board, _ = recorded(1, ply)
            plan = pacing.derive(board, [], 500, persist_recovery=False)
            self.assertTrue(plan["active"])
            self.assertEqual((plan["event_ply"], plan["stage"]), (35, stage))
            self.assertEqual(plan["allow_material_return"], stage >= 3)
            targets.append(plan["target_cp"])
        self.assertEqual(targets[-1], 30)
        self.assertEqual(targets, sorted(targets, reverse=True))
        board, _ = recorded(1, 44)
        self.assertFalse(pacing.derive(board, [], 500, persist_recovery=False)["active"])

    @patch.object(pacing, "CLOSE_PLIES", 80)
    def test_equal_rook_and_long_queen_rook_exchange_are_not_blunders(self):
        # g3 p36..41 trades pawns, two queens and two rooks: net zero.
        # Looking only at p39..41 would incorrectly report a +500cp gift.
        for game, ply in ((3, 42), (7, 66)):
            board, _ = recorded(game, ply)
            plan = pacing.derive(board, [], 500)
            self.assertFalse(plan["active"])
            self.assertEqual(plan["reason"], "no_recent_major_loss")

    def test_earlier_capture_cannot_carry_return_across_the_root_deadline(self):
        board, _ = recorded(1, 36)
        with patch.object(pacing, "CLOSE_PLIES", 37):
            self.assertTrue(pacing.derive(board, [], 500)["active"])
        with patch.object(pacing, "CLOSE_PLIES", 36):
            self.assertFalse(pacing.derive(board, [], 500)["active"])

    @patch.object(pacing, "CLOSE_PLIES", 80)
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

    @patch.object(pacing, "CLOSE_PLIES", 80)
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

    @patch.object(pacing, "CLOSE_PLIES", 80)
    def test_explicit_fade_scales_temporary_allowance_without_changing_nominal_band(self):
        board, _ = recorded(1, 38)
        normal = pacing.derive(board, [], 500)
        fading = pacing.derive(board, [], 500, ramp_fraction=.5)
        self.assertLess(fading["upper_cp"], normal["upper_cp"])
        self.assertGreaterEqual(fading["upper_cp"], 100)

    @patch.object(pacing, "CLOSE_PLIES", 80)
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
