"""Strict observation evidence with controlled packets, no native searches."""
import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import chess
import chess.engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import boss_play
import rating_evidence as evidence


def board_after(*moves):
    board = chess.Board()
    for move in moves:
        board.push_uci(move)
    return board


def packet(board, moves, cp=20, *, mate=None, depth=6, **extra):
    score = chess.engine.Cp(cp) if mate is None else chess.engine.Mate(mate)
    return {"depth": depth, "score": chess.engine.PovScore(score, board.turn),
            "pv": [chess.Move.from_uci(move) for move in moves.split()], **extra}


def iterations(board, moves, cp=20, *, mate=None, depths=(4, 5, 6)):
    return {depth: packet(board, moves, cp, mate=mate, depth=depth) for depth in depths}


def compare(board, best, played, *, best_info=None, played_info=None,
            best_packets=None, played_packets=None, returned=None):
    best_packets = iterations(board, best, 100) if best_packets is None else best_packets
    played_packets = iterations(board, played, -100) if played_packets is None else played_packets
    best_info = best_packets[max(best_packets)] if best_info is None else best_info
    played_info = played_packets[max(played_packets)] if played_info is None else played_info
    actual_move = chess.Move.from_uci(played.split()[0])
    return evidence.assess(board, chess.Move.from_uci(best.split()[0]), best_info, best_packets,
                           actual_move, played_info=played_info, played_packets=played_packets,
                           returned_move=actual_move if returned is None else returned)


class RatingEvidenceTests(unittest.TestCase):
    def test_same_position_cp_and_black_pov(self):
        for board, best, played in ((chess.Board(), "e2e4", "d2d4"),
                                    (board_after("e2e4"), "c7c5", "e7e5")):
            with self.subTest(turn=board.turn):
                result = compare(board, best, played)
                self.assertTrue(result["valid"])
                self.assertEqual(result["kind"], "cp_loss")
                self.assertEqual((result["best_cp"], result["played_cp"]), (100, -100))
                self.assertEqual(result["depth"], 6)
                self.assertEqual(result["reliability"], "stable")
                self.assertEqual((result["root_changes"], result["cp_span"]), (0, 0))

    def test_single_shallow_completed_best_match_is_only_limited(self):
        board = chess.Board()
        info = packet(board, "e2e4", depth=4)
        result = evidence.assess(board, info["pv"][0], info, {4: info}, info["pv"][0])
        self.assertTrue(result["valid"])
        self.assertEqual(result["best_cp"], result["played_cp"])
        self.assertEqual(result["reliability"], "limited")

    def test_two_consecutive_exact_best_matches_can_be_stable(self):
        board = chess.Board()
        values = iterations(board, "e2e4", depths=(4, 5))
        result = evidence.assess(board, chess.Move.from_uci("e2e4"), values[5], values,
                                 chess.Move.from_uci("e2e4"))
        self.assertTrue(result["valid"])
        self.assertEqual(result["reliability"], "stable")

    def test_bound_wrong_pov_multipv_illegal_or_mismatched_packet_rejected(self):
        board = chess.Board()
        base = packet(board, "e2e4", 100)
        bad_values = [{**base, "lowerbound": True}, {**base, "upperbound": True},
                      {**base, "multipv": 2}, {**base, "depth": 3},
                      {**base, "score": chess.engine.PovScore(chess.engine.Cp(100), chess.BLACK)},
                      {**base, "pv": [chess.Move.from_uci("e2e5")]},
                      {**base, "pv": []}, {**base, "depth": True}]
        for info in bad_values:
            with self.subTest(info=info):
                result = compare(board, "e2e4", "d2d4", best_info=info)
                self.assertFalse(result["valid"])
        missing_raw = compare(board, "e2e4", "d2d4", best_info=base,
                              best_packets={6: packet(board, "e2e4", 150)})
        self.assertFalse(missing_raw["valid"])

    def test_invalid_actual_packet_returned_move_and_depth_are_rejected(self):
        board = chess.Board()
        for values in (iterations(board, "d2d4", depths=(3,)),
                       {6: packet(board, "d2d4", upperbound=True)},
                       {6: packet(board, "d2d4", multipv=2)},
                       {6: packet(board, "e2e4")},
                       {7: packet(board, "d2d4", depth=7)}):
            self.assertFalse(compare(board, "e2e4", "d2d4", played_packets=values)["valid"])
        self.assertFalse(compare(board, "e2e4", "d2d4", returned=chess.Move.from_uci("e2e4"))["valid"])

    def test_newer_bound_cannot_be_hidden_behind_older_exact_packet(self):
        board = chess.Board()
        best = iterations(board, "e2e4", 100)
        best[7] = packet(board, "e2e4", 100, depth=7, lowerbound=True)
        result = compare(board, "e2e4", "d2d4", best_packets=best, best_info=best[6])
        self.assertFalse(result["valid"])

    def test_changed_root_score_swing_or_depth_gap_limits_reliability(self):
        board = chess.Board()
        for change in ("root", "swing", "gap"):
            values = iterations(board, "e2e4", 100)
            if change == "root":
                values[5] = packet(board, "g1f3", 100, depth=5)
            elif change == "swing":
                values[4] = packet(board, "e2e4", 400, depth=4)
            else:
                del values[5]
            result = compare(board, "e2e4", "d2d4", best_packets=values)
            self.assertTrue(result["valid"])
            self.assertEqual(result["reliability"], "limited")
            if change == "root":
                self.assertEqual(result["root_changes"], 2)
            elif change == "swing":
                self.assertEqual(result["cp_span"], 300)

    def test_actual_root_needs_its_own_repeat_evidence(self):
        board = chess.Board()
        result = compare(board, "e2e4", "d2d4", played_packets={6: packet(board, "d2d4", -100)})
        self.assertTrue(result["valid"])
        self.assertEqual(result["reliability"], "limited")

    def test_restricted_candidate_beating_reference_is_a_conflict(self):
        board = chess.Board()
        result = compare(board, "e2e4", "d2d4", played_packets=iterations(board, "d2d4", 160))
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "reference_conflict")

    def test_allowed_mate_is_separate_from_centipawn_loss(self):
        board = board_after("f2f3", "e7e5")
        actual = iterations(board, "g2g4 d8h4", mate=-1)
        result = compare(board, "g2g3", "g2g4 d8h4", played_packets=actual)
        self.assertTrue(result["valid"])
        self.assertEqual(result["kind"], "allowed_mate")
        self.assertEqual(result["played_mate"], -1)
        self.assertIsNone(result["played_cp"])
        self.assertEqual(result["reliability"], "stable")

    def test_missed_mate_black_pov_and_preserved_mate(self):
        board = board_after("f2f3", "e7e5", "g2g4")
        best = iterations(board, "d8h4", mate=1)
        result = compare(board, "d8h4", "b8c6", best_packets=best)
        self.assertTrue(result["valid"])
        self.assertEqual(result["kind"], "missed_mate")
        self.assertEqual(result["best_mate"], 1)
        self.assertIsNone(result["best_cp"])
        preserved = evidence.assess(board, chess.Move.from_uci("d8h4"), best[6], best,
                                    chess.Move.from_uci("d8h4"))
        self.assertEqual(preserved["kind"], "mate_preserved")
        self.assertEqual(preserved["played_mate"], 1)

    def test_already_lost_mate_is_not_new_allowed_mate(self):
        board = board_after("f2f3", "e7e5", "g2g4", "b8c6")
        best = iterations(board, "a2a3 d8h4", mate=-1)
        actual = iterations(board, "b2b3 d8h4", mate=-1)
        result = compare(board, "a2a3 d8h4", "b2b3 d8h4", best_packets=best, played_packets=actual)
        self.assertTrue(result["valid"])
        self.assertEqual(result["kind"], "already_lost")
        self.assertIsNone(result["best_cp"])
        self.assertIsNone(result["played_cp"])

    def test_mate_distance_sign_and_legal_completion_are_strict(self):
        board = board_after("f2f3", "e7e5", "g2g4")
        for info in (packet(board, "d8h4", mate=2), packet(board, "d8h4", mate=-1),
                     packet(board, "b8c6", mate=1), packet(board, "d8h4", cp=500)):
            result = evidence.assess(board, info["pv"][0], info, {6: info}, info["pv"][0])
            self.assertFalse(result["valid"])

    def test_best_and_candidate_cannot_use_different_completed_depths(self):
        board = chess.Board()
        result = compare(board, "e2e4", "d2d4", played_packets=iterations(board, "d2d4", depths=(4, 5)))
        self.assertFalse(result["valid"])
        result = compare(board, "e2e4", "d2d4", played_packets=iterations(board, "d2d4", depths=(7, 8)))
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "no_common_depth")

    def test_input_board_and_packets_are_not_modified(self):
        board = chess.Board()
        first, second = iterations(board, "e2e4", 100), iterations(board, "d2d4", -100)
        snapshot = copy.deepcopy((first, second)), board.fen(), list(board.move_stack)
        compare(board, "e2e4", "d2d4", best_packets=first, played_packets=second)
        self.assertEqual((first, second), snapshot[0])
        self.assertEqual((board.fen(), list(board.move_stack)), snapshot[1:])


class ObservationIntegrationTests(unittest.TestCase):
    def observe(self, board, best_moves, best_cp=100, best_mate=None, actual_moves=None,
                actual_cp=-100, actual_mate=None, depth=6, **kwargs):
        calls = []
        def search(budget, *, root_move=None, position=None, **options):
            calls.append({"budget": budget, "root_move": root_move, **options})
            moves = actual_moves if root_move else best_moves
            cp, mate = (actual_cp, actual_mate) if root_move else (best_cp, best_mate)
            values = iterations(position, moves, cp, mate=mate, depths=tuple(range(4, depth + 1)))
            info = values[depth]
            return chess.engine.BestMove(info["pv"][0], None), info, values
        result, discarded = boss_play.observe_human_move(board, search, lambda: None, budget_ms=500, **kwargs)
        return result, discarded, calls

    def test_legacy_cp_loss_and_search_protocol_unchanged(self):
        result, discarded, calls = self.observe(board_after("d2d4"), "e2e4", actual_moves="d2d4")
        self.assertFalse(discarded)
        self.assertTrue(result["valid"])
        self.assertEqual(result["loss_cp"], 200)
        self.assertEqual(result["reason"], "same_position_completed_depth")
        self.assertTrue(result["rating_evidence"]["valid"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["budget"], 225)
        self.assertTrue(all(call["fresh"] and not call["publish"] and call["budget"] <= 500 for call in calls))
        self.assertEqual(calls[1]["depth"], 6)

    def test_legacy_reference_match_still_needs_one_search(self):
        result, discarded, calls = self.observe(board_after("e2e4"), "e2e4", depth=4)
        self.assertFalse(discarded)
        self.assertEqual(len(calls), 1)
        self.assertTrue(result["valid"])
        self.assertEqual(result["loss_cp"], 0)
        self.assertEqual(result["reason"], "actual_move_is_reference")
        self.assertEqual(result["rating_evidence"]["reliability"], "limited")

    def test_actual_allowed_mate_informs_rating_without_changing_legacy_controller(self):
        board = board_after("f2f3", "e7e5", "g2g4")
        result, discarded, calls = self.observe(board, "g2g3", actual_moves="g2g4 d8h4", actual_mate=-1)
        self.assertFalse(discarded)
        self.assertFalse(result["valid"])
        self.assertIsNone(result["loss_cp"])
        self.assertEqual(result["reason"], "mate_candidate_not_comparable")
        self.assertEqual(result["rating_evidence"]["kind"], "allowed_mate")
        self.assertEqual(len(calls), 2)

    def test_mate_reference_uses_remaining_budget_for_rating_only(self):
        board = board_after("f2f3", "e7e5", "g2g4", "b8c6")
        result, discarded, calls = self.observe(board, "d8h4", best_mate=1, actual_moves="b8c6")
        self.assertFalse(discarded)
        self.assertFalse(result["valid"])
        self.assertIsNone(result["loss_cp"])
        self.assertEqual(result["reason"], "reference_unreliable")
        self.assertEqual(result["rating_evidence"]["kind"], "missed_mate")
        self.assertEqual(len(calls), 2)
        self.assertLessEqual(calls[1]["budget"], 500)

    def test_timeout_invalidates_both_legacy_and_rating_evidence(self):
        with patch.object(boss_play.time, "monotonic", side_effect=[0, .6]):
            result, _, _ = self.observe(board_after("e2e4"), "e2e4")
        self.assertFalse(result["valid"])
        self.assertIsNone(result["loss_cp"])
        self.assertFalse(result["rating_evidence"]["valid"])
        self.assertEqual(result["rating_evidence"]["reason"], "quality_probe_timeout")

    def test_forced_missing_history_and_short_budget_do_not_create_evidence(self):
        for board, kwargs in ((chess.Board(), {}), (board_after("e2e4"), {"forced": True}),
                              (board_after("e2e4"), {"budget_ms": 99})):
            calls = []
            result, _ = boss_play.observe_human_move(board, lambda *args, **kw: calls.append(1), lambda: None, **kwargs)
            self.assertEqual(calls, [])
            self.assertFalse(result["rating_evidence"]["valid"])

    def test_probe_exception_does_not_publish_partial_rating(self):
        board = board_after("e2e4")
        def fail(*args, **kwargs):
            raise RuntimeError("controlled")
        result, discarded = boss_play.observe_human_move(board, fail, lambda: None)
        self.assertTrue(discarded)
        self.assertFalse(result["rating_evidence"]["valid"])
        self.assertEqual(result["rating_evidence"]["reason"], "quality_probe_failed")


if __name__ == "__main__":
    unittest.main()
