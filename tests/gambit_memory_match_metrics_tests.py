"""Engine-free contracts for played-move memory and gambit character metrics."""
import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import chess

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import boss_play
import gambit_memory_match_metrics as metrics
import gambit_practice_memory as memory
import sound_sacrifice


def replay(words):
    board = chess.Board()
    for word in words.split():
        board.push_uci(word)
    return board


def identity(board):
    return {"before_fen": board.fen(en_passant="fen"),
            "history_uci": [move.uci() for move in board.move_stack]}


def receipt(board, word, **extra):
    return {**identity(board), "policy": boss_play.POLICY,
            "ply": len(board.move_stack) + 1, "selected_uci": word,
            "baseline_uci": word, **extra}


def proposal(board, word):
    return {**identity(board), "source": memory.SOURCE, "uci": word}


class MoveMetricsTests(unittest.TestCase):
    def test_played_offer_is_separate_from_proposal_and_immediate_capture(self):
        board = replay("e2e4 e7e5")
        decision = receipt(board, "f2f4", mode="opening", proposed_line_id="kings",
            line_id="kings", candidate_uci="f2f4", line_complete=True)
        row = metrics.move_metrics(board, "f2f4", decision)
        self.assertTrue(row["authored_signature_proposed"])
        self.assertTrue(row["authored_signature_selected"])
        self.assertTrue(row["catalog_signature_played_this_move"])
        self.assertTrue(row["signature_immediate_capture_available"])
        self.assertEqual(row["signature_capture_options_uci"], ["e5f4"])
        self.assertFalse(row["catalog_signature_accepted_this_move"])
        self.assertFalse(row["actual_capture"])

    def test_proposed_but_rejected_offer_is_not_played(self):
        board = replay("e2e4 e7e5")
        decision = receipt(board, "g1f3", mode="opening", proposed_line_id="kings",
            line_id=None, candidate_uci="f2f4", line_complete=False)
        row = metrics.move_metrics(board, "g1f3", decision)
        self.assertTrue(row["authored_signature_proposed"])
        self.assertFalse(row["authored_signature_selected"])
        self.assertFalse(row["catalog_signature_played_this_move"])

    def test_actual_signature_without_receipt_is_still_a_board_fact(self):
        row = metrics.move_metrics(replay("e2e4 e7e5"), "f2f4")
        self.assertFalse(row["receipt_valid"])
        self.assertFalse(row["authored_signature_selected"])
        self.assertTrue(row["catalog_signature_played_this_move"])

    def test_acceptance_is_only_actual_capture_now_with_owner_separate(self):
        board = replay("e2e4 e7e5 f2f4")
        row = metrics.move_metrics(board, "e5f4")
        self.assertTrue(row["catalog_signature_accepted_this_move"])
        self.assertEqual(row["accepted_signature_capture_ply"], 4)
        self.assertEqual(row["accepted_signature_owner"], "white")
        self.assertEqual(row["accepted_signature_family_id"], "kings")
        self.assertEqual(row["captured_piece"], "P")
        self.assertEqual(row["material"]["delta_own"], 100)
        self.assertFalse(row["catalog_signature_played_this_move"])
        board.push_uci("e5f4")
        later = metrics.move_metrics(board, "g1f3")
        self.assertFalse(later["catalog_signature_accepted_this_move"])
        self.assertFalse(later["catalog_signature_played_this_move"])
        self.assertTrue(later["operations_before"]["accepted"])

    def test_stale_receipt_cannot_supply_authored_or_memory_credit(self):
        board = replay("e2e4 e7e5")
        good = receipt(board, "f2f4", mode="opening", proposed_line_id="kings",
            line_id="kings", candidate_uci="f2f4", line_complete=True)
        for field, value in [("before_fen", "old"), ("history_uci", []),
                             ("selected_uci", "g1f3"), ("policy", "unknown"), ("ply", 2)]:
            with self.subTest(field=field):
                row = metrics.move_metrics(board, "f2f4", {**good, field: value})
                self.assertFalse(row["receipt_valid"])
                self.assertFalse(row["authored_signature_proposed"])
                self.assertFalse(row["authored_signature_selected"])
                self.assertTrue(row["catalog_signature_played_this_move"])

    def test_receipt_sacrifice_candidate_is_not_an_actual_capture_or_site_grade(self):
        board = replay("e2e4 e7e5")
        geometry = {**identity(board), "policy": sound_sacrifice.POLICY,
            "uci": "f2f4", "eligible": True, "complete": True, "investment_cp": 100}
        detail = {"qualified": True, "applied": True, "selected_uci": "f2f4",
            "kinds": ["sound_sacrifice_candidate"], "evidence": {"sacrifice": geometry}}
        decision = receipt(board, "f2f4", baseline_uci="g1f3", tactics=detail)
        with patch.object(sound_sacrifice, "evidence", side_effect=AssertionError("No extra probes")):
            row = metrics.move_metrics(board, "f2f4", decision)
        self.assertTrue(row["selected_sacrifice_candidate_from_receipt"])
        self.assertTrue(row["sacrifice_candidate_changed_move"])
        self.assertFalse(row["actual_capture"])
        self.assertEqual(row["local_sacrifice_investment_cp"], 100)
        geometry["complete"] = False
        self.assertFalse(metrics.move_metrics(board, "f2f4", decision)["selected_sacrifice_candidate_from_receipt"])

    def test_memory_attempt_packet_admission_and_change_are_separate(self):
        board = replay("e2e4 e7e5 f2f4 e5f4 d1g4 d8f6")
        source = proposal(board, "g1h3")
        attempt = {"uci": "g1h3", "source": source, "admitted": True,
            "candidate_compared": {"depth": 4, "cp": -70, "pv_uci": ["g1h3", "d7d6"]}}
        decision = receipt(board, "g1h3", baseline_uci="f1c4", knowledge={"applied": True,
            "selected_uci": "g1h3", "attempts": [attempt]})
        row = metrics.move_metrics(board, "g1h3", decision, proposals=[source, source])
        for key in ["memory_proposed_uci", "memory_attempted_uci", "memory_probed_uci", "memory_admitted_uci"]:
            self.assertEqual(row[key], ["g1h3"])
        self.assertTrue(row["memory_changed_move"])
        self.assertTrue(row["selected_matches_memory_proposal"])
        decision["knowledge"]["applied"] = False
        self.assertFalse(metrics.move_metrics(board, "g1h3", decision, proposals=[source])["memory_changed_move"])
        self.assertTrue(metrics.move_metrics(board, "g1h3", decision, proposals=[source])["selected_matches_memory_proposal"])

    def test_stale_source_and_illegal_packet_do_not_count_completed_probe(self):
        board = replay("e2e4 e7e5")
        source = proposal(board, "g1f3")
        attempt = {"uci": "g1f3", "source": source, "admitted": True,
            "candidate_compared": {"depth": 4, "cp": 10, "pv_uci": ["g1f3", "e8e1"]}}
        decision = receipt(board, "g1f3", baseline_uci="b1c3", knowledge={"applied": True,
            "selected_uci": "g1f3", "attempts": [attempt]})
        row = metrics.move_metrics(board, "g1f3", decision, proposals=[source])
        self.assertEqual(row["memory_attempted_uci"], ["g1f3"])
        self.assertEqual(row["memory_probed_uci"], [])
        self.assertFalse(row["memory_changed_move"])
        source["history_uci"] = []
        row = metrics.move_metrics(board, "g1f3", decision, proposals=[source])
        self.assertEqual(row["memory_proposed_uci"], [])
        self.assertEqual(row["memory_attempted_uci"], [])

    def test_en_passant_and_black_material_sign(self):
        board = replay("e2e4 a7a6 e4e5 d7d5")
        row = metrics.move_metrics(board, "e5d6")
        self.assertTrue(row["actual_capture"])
        self.assertEqual(row["captured_piece"], "p")
        self.assertEqual(row["captured_material_units"], 100)
        self.assertEqual(row["material"]["delta_own"], 100)
        black = metrics.move_metrics(replay("e2e4 e7e5 f2f4"), "e5f4")
        self.assertEqual(black["actor_color"], "black")
        self.assertEqual(black["material"]["after_own"], 100)

    def test_inputs_are_unchanged_and_malformed_optional_receipt_is_closed(self):
        board = replay("e2e4 e7e5")
        decision = receipt(board, "g1f3", knowledge={"attempts": None},
                           tactics={"evidence": "invalid", "kinds": None})
        proposals = [proposal(board, "g1f3")]
        snapshot = (board.fen(en_passant="fen"), list(board.move_stack), copy.deepcopy(decision), copy.deepcopy(proposals))
        row = metrics.move_metrics(board, "g1f3", decision, proposals=proposals)
        self.assertEqual(snapshot, (board.fen(en_passant="fen"), list(board.move_stack), decision, proposals))
        self.assertTrue(row["position_unchanged"])
        self.assertEqual(row["activity"]["delta"]["developed_minors"], 1)
        self.assertEqual(row["memory_probed_uci"], [])

    def test_illegal_move_is_rejected(self):
        with self.assertRaises(ValueError):
            metrics.move_metrics(chess.Board(), "e2e5")


if __name__ == "__main__":
    unittest.main()
