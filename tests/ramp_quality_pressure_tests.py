"""Generic minor-family/contact discovery for unsafe ramp incumbents.

The recorded Bxf3 is fixed test input. No reference-engine root move is used
to construct the candidate groups, and every candidate still needs the normal
native and external evidence before it can replace that input.
"""
import json
from pathlib import Path
import unittest
from unittest import mock

import chess

import sparring_quality_discovery_tests as support
from sparring_quality_policy_tests import assessment, native_reply

quality = support.quality
ROOT = Path(__file__).resolve().parents[1]


def recorded_scene():
    record = json.loads((ROOT / "docs/matches/2026-11-04-pressure-clock-v2/black/game.json").read_text(encoding="utf-8"))
    board = chess.Board(record["initial_fen"])
    for uci in record["uci_moves"][:43]:
        board.push_uci(uci)
    return board


class MinorContactTests(unittest.TestCase):
    def test_ramp_uses_both_unrepresented_minor_families_in_existing_two_slots(self):
        board = recorded_scene()
        excluded = set(map(chess.Move.from_uci, ["g4f3", "a6a5", "b7b6"]))
        origins = quality._minor_origins(board, excluded)
        self.assertEqual(len(origins), 2)
        self.assertEqual([board.piece_type_at(square) for square in origins], [chess.BISHOP, chess.KNIGHT])
        self.assertNotIn(chess.G4, origins)
        mirrored = support.mirrored_history(board)
        reverse = quality._minor_origins(mirrored, {support.mirrored_move(move) for move in excluded})
        self.assertEqual(reverse, [chess.square_mirror(square) for square in origins])

    def test_pre16_keeps_original_knight_origin_order(self):
        board = support.move33_board()
        while len(board.move_stack) > 28:
            board.pop()
        origins = quality._minor_origins(board, set())
        self.assertEqual([board.piece_type_at(square) for square in origins], [chess.KNIGHT, chess.KNIGHT])

    def test_contact_groups_are_legal_board_geometry_on_a_different_position(self):
        board = chess.Board("6k1/8/5r2/8/8/8/8/R1B3K1 w - - 0 21")
        moves = list(board.legal_moves) + [chess.Move.from_uci("c1c5")]
        contacts = quality._contact_moves(board, moves)
        self.assertEqual(set(move.uci() for move in contacts), {"c1b2", "c1g5"})
        mirrored = board.mirror()
        reverse = quality._contact_moves(mirrored, list(mirrored.legal_moves))
        self.assertEqual(set(reverse), {support.mirrored_move(move) for move in contacts})

    def test_pawn_targets_and_major_movers_do_not_fabricate_minor_contact(self):
        board = chess.Board("6k1/8/5p2/8/8/8/8/R1B3K1 w - - 0 21")
        self.assertEqual(quality._contact_moves(board, list(board.legal_moves)), [])

    def test_recorded_scene_contact_group_is_found_without_reference_pv(self):
        board = recorded_scene()
        for position in (board, support.mirrored_history(board)):
            excluded = {next(move for move in position.legal_moves
                             if position.is_capture(move) and position.piece_type_at(move.from_square) == chess.BISHOP)}
            for origin in quality._minor_origins(position, excluded):
                allowed = [move for move in position.legal_moves if move.from_square == origin]
                contacts = quality._contact_moves(position, allowed)
                self.assertTrue(all(move in allowed for move in contacts))
                for move in contacts:
                    after = position.copy(stack=False)
                    after.push(move)
                    self.assertTrue(any((piece := after.piece_at(square)) is not None
                        and piece.color != position.turn and piece.piece_type in [chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN]
                        for square in after.attacks(move.to_square)))

    def test_contact_discovery_cannot_bypass_reference_collapse_check(self):
        board = recorded_scene()
        original = chess.Move.from_uci("g4f3")
        calls = []
        def search(amount, *, root_moves, **options):
            calls.append((amount, list(root_moves)))
            return native_reply(board, root_moves[0])
        def assess(move, amount, cancelled):
            return assessment(board, move, 50, -200)
        result, receipt, discarded = quality.verify(board, original, search, lambda: None, assess)
        self.assertIsNone(result)
        self.assertEqual(receipt["status"], "unverified")
        self.assertEqual(receipt["selected_uci"], original.uci())
        self.assertFalse(discarded)
        self.assertLessEqual(sum(attempt["kind"] in ["minor_contact", "minor_origin"] for attempt in receipt["attempts"]), 2)
        self.assertTrue(all(amount <= 500 for amount, _ in calls))

    def test_short_reference_floor_miss_stays_mitigated_despite_better_deep_score(self):
        board = recorded_scene()
        original, better = map(chess.Move.from_uci, ["g4f3", "e7b4"])
        def search(amount, *, root_moves, **options):
            self.assertIn(better, root_moves)
            return native_reply(board, better)
        def assess(move, amount, cancelled):
            return assessment(board, move, 78 if move == original else 102,
                              -138 if move == original else 40)
        with mock.patch.object(quality, "NATIVE_BUDGETS_MS", (400,)):
            replacement, receipt, discarded = quality.verify(board, original, search, lambda: None, assess)
        self.assertEqual(replacement[0].move, better)
        self.assertFalse(discarded)
        self.assertEqual(receipt["status"], "mitigated")
        self.assertEqual(receipt["reason"], "verified_native_damage_limitation")
        self.assertFalse(receipt["final_verdict"]["acceptable"])
        self.assertFalse(receipt["mitigation"]["floor_met"])
        self.assertEqual(receipt["mitigation"]["improvement_cp"], 178)
        # The later independent +74cp audit cannot overwrite the recorded
        # +40cp short-reference result or promote it to strict verification.
        self.assertEqual(receipt["final_verdict"]["child_cp_own"], 40)


if __name__ == "__main__":
    unittest.main()
