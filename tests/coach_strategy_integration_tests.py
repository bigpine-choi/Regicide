"""Cross-layer strategic explanation and continuation checks with legal fixtures."""
import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import chess

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from coach_explanations import describe_move
from coach_strategy_tests import FIXTURES, fixture, mirror_move, source_row


class StrategyIntegrationTests(unittest.TestCase):
    def test_quiet_central_development_with_no_attacked_piece_keeps_review_running(self):
        initial = chess.Board()
        for san in "e4 c5 Nf3 d6 d4 cxd4 Nxd4 Nf6".split():
            initial.push_san(san)
        for mirrored in (False, True):
            board = initial.mirror() if mirrored else initial
            pv = ["b1c3", "a7a6"]
            if mirrored:
                pv = [mirror_move(move) for move in pv]
            row = source_row(board, pv, pv, "best")
            coach = describe_move(board, row)
            for packet in (coach, *coach["perspectives"].values()):
                self.assertEqual(packet["insight"]["theme"], "development")
                self.assertEqual(len(packet["insight"]["sections"]), 3)
                self.assertIn("influences", packet["insight"]["sections"][0]["text"])

    def test_all_motifs_feed_the_matching_continuation_without_mutating_evidence(self):
        for value in FIXTURES:
            for mirrored in (False, True):
                with self.subTest(theme=value[0], mirrored=mirrored):
                    board, row = fixture(value, mirrored)
                    saved = copy.deepcopy(row)
                    coach = describe_move(board, row)
                    self.assertEqual(row, saved)
                    for packet in (coach, *coach["perspectives"].values()):
                        insight, continuation = packet["insight"], packet["continuation"]
                        self.assertEqual(insight["theme"], value[0])
                        self.assertTrue(continuation["intro"].startswith(insight["summary"]))
                        for note in insight["step_notes"]:
                            index = note["ply_index"] - 1
                            self.assertEqual(continuation["steps"][index]["text"], note["text"])
                            self.assertEqual(continuation["steps"][index]["uci"], row["best"]["pv_uci"][index])
                            self.assertTrue(set(note["squares"]).issubset(continuation["steps"][index]["squares"]))

    def test_good_move_insights_keep_the_played_branch_and_do_not_invent_correction(self):
        board, source = fixture(FIXTURES[7])
        row = source_row(board, source["best"]["pv_uci"], source["played"]["pv_uci"], "good")
        coach = describe_move(board, row)
        for packet in (coach, *coach["perspectives"].values()):
            self.assertEqual(packet["insight"]["theme"], "development")
            self.assertTrue(all(section["line"] == "played" for section in packet["insight"]["sections"]))
            self.assertIsNone(packet["continuation"])

    def test_mate_on_first_move_preserves_mate_explanation_in_both_colors(self):
        initial = chess.Board("7k/1R6/8/8/8/8/7P/R5K1 w - - 0 1")
        for mirrored in (False, True):
            board = initial.mirror() if mirrored else initial
            actual, best = "h2h3", "a1a8"
            if mirrored:
                actual, best = mirror_move(actual), mirror_move(best)
            row = source_row(board, [actual], [best], "missed_mate")
            coach = describe_move(board, row)
            for packet in (coach, *coach["perspectives"].values()):
                self.assertIsNone(packet["insight"])
                continuation = packet["continuation"]
                self.assertTrue(continuation["terminal"])
                self.assertIn("checkmate", continuation["steps"][0]["text"])
                self.assertIn("checkmate", continuation["summary"])

    def test_terminal_result_has_priority_even_over_injected_strategy_note(self):
        board = chess.Board("7k/1R6/8/8/8/8/7P/R5K1 w - - 0 1")
        row = source_row(board, ["h2h3"], ["a1a8"], "missed_mate")
        stale = {"summary": "Try Ra8#.", "principle": "Check the result.",
                 "step_notes": [{"line": "best", "ply_index": 1, "text": "FORGED PLAN", "squares": ["a8"]}]}
        with patch("coach_explanations.analyze_strategy", return_value=stale):
            coach = describe_move(board, row)
        for packet in (coach, *coach["perspectives"].values()):
            self.assertIn("checkmate", packet["continuation"]["steps"][0]["text"])
            self.assertNotIn("FORGED", packet["continuation"]["steps"][0]["text"])

    def test_automatic_draw_ends_instruction_even_when_final_move_gives_check(self):
        initial = chess.Board("6k1/p6p/8/8/8/8/P6P/R5K1 w - - 147 80")
        for mirrored in (False, True):
            board = initial.mirror() if mirrored else initial
            actual, pv = "a2a3", ["a1e1", "g8f7", "e1e7"]
            if mirrored:
                actual, pv = mirror_move(actual), [mirror_move(move) for move in pv]
            row = source_row(board, [actual], pv)
            coach = describe_move(board, row)
            for packet in (coach, *coach["perspectives"].values()):
                self.assertEqual(packet["insight"]["theme"], "rook_file")
                continuation = packet["continuation"]
                self.assertTrue(continuation["terminal"])
                final = continuation["steps"][-1]["text"]
                self.assertIn("draw by the seventy-five-move rule", final)
                self.assertNotIn("must answer", final)


if __name__ == "__main__":
    unittest.main()
