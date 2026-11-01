"""Short, source-specific coach dialogue; no engine or personal game inputs."""
import json
from pathlib import Path
import re
import sys
import unittest

import chess

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from coach_explanations import COACH_VERSION, describe_move, describe_report


def row(actual, played, best=None, kind="good"):
    return {"ply": 1, "uci": actual, "san": "FORGED_SAN", "color": "wrong",
            "classification": kind, "played": {"pv_uci": played},
            "best": {"pv_uci": best if best is not None else played}}


class CoachDialogueTests(unittest.TestCase):
    def check_packet(self, board, source):
        packet = describe_move(board, source)
        self.assertLessEqual(len(packet["dialogue"].split()), 40)
        self.assertLessEqual(len(packet["hint"].split()), 16)
        self.assertEqual(packet["hint"].count("."), 1)
        self.assertNotRegex(json.dumps(packet, ensure_ascii=False), r"[가-힣]")
        self.assertNotIn("FORGED_SAN", packet["dialogue"])
        for evidence in packet["evidence"]:
            self.assertIn(evidence["line"], ("played", "best"))
            self.assertTrue(1 <= evidence["ply_index"] <= len(source[evidence["line"]]["pv_uci"]))
            self.assertTrue(all(chess.parse_square(square) >= 0 for square in evidence["squares"]))
            self.assertLessEqual(len(evidence["text"].split()), 24)
        for uci in source["best"]["pv_uci"][:1]:
            if isinstance(uci, str) and re.fullmatch(r"[a-h][1-8][a-h][1-8][qrbn]?", uci):
                self.assertNotIn(uci, packet["hint"])
                self.assertNotIn(uci[2:4], packet["hint"])
                move = chess.Move.from_uci(uci)
                if move in board.legal_moves:
                    self.assertNotIn(board.san(move), packet["hint"])
        return packet

    def test_positive_moves_ignore_remote_capture_and_castle_in_dialogue_and_evidence(self):
        for pv in (["e2e4", "d7d5", "e4d5", "d8d5"],
                   ["e2e4", "e7e5", "g1f3", "b8c6", "f1c4", "f8c5", "d2d3", "g8f6", "e1g1"]):
            result = self.check_packet(chess.Board(), row("e2e4", pv))
            self.assertIn("White puts a pawn in the center", result["dialogue"])
            self.assertNotIn("capture", result["dialogue"])
            self.assertNotIn("O-O", result["dialogue"])
            self.assertEqual(result["evidence"], [])

    def test_black_development_names_only_immediate_reply(self):
        board = chess.Board()
        board.push_uci("e2e4")
        result = self.check_packet(board, row("g8f6", ["g8f6", "e4e5", "f6d5"], kind="best"))
        self.assertEqual(result["dialogue"], "Black develops the knight with Nf6. Watch for White's e5 in response.")
        self.assertNotIn("Nd5", result["dialogue"])

    def test_short_tactical_reply_prioritizes_queen_over_pawn_and_keeps_scope(self):
        board = chess.Board("3q2k1/8/3b4/8/8/5N2/7P/3Q2K1 w - - 0 1")
        result = self.check_packet(board, row("d1d3", ["d1d3", "d6h2", "f3h2", "d8d3"], ["d1d4"], "blunder"))
        self.assertIn("After White's Qd3, the line Bxh2+ Nxh2 Qxd3 captures White's queen", result["dialogue"])
        self.assertIn("Qd4 instead", result["dialogue"])
        self.assertIn("White's queen", result["evidence"][0]["text"])
        self.assertEqual(result["evidence"][0]["ply_index"], 4)
        self.assertIn("d3", result["focus_squares"])
        self.assertNotRegex(result["dialogue"], r"forced|inevitable|pin|fork|material balance")

    def test_same_square_alternative_explains_piece_choice_without_inventing_cause(self):
        board = chess.Board("4k3/8/8/8/8/8/3P4/4KB2 w - - 0 1")
        result = self.check_packet(board, row("d2d3", ["d2d3"], ["f1d3"], "mistake"))
        self.assertIn("puts a pawn on d3", result["dialogue"])
        self.assertIn("use that square for the bishop", result["dialogue"])

    def test_black_tactical_loss_keeps_actor_and_victim_correct(self):
        board = chess.Board("3q2k1/8/3b4/8/8/5N2/7P/3Q2K1 w - - 0 1").mirror()
        result = self.check_packet(board, row("d8d6", ["d8d6", "d3h7", "f6h7", "d1d6"], ["d8d5"], "blunder"))
        self.assertIn("After Black's Qd6", result["dialogue"])
        self.assertIn("captures Black's queen", result["dialogue"])
        self.assertIn("Black should consider Qd5", result["dialogue"])
        self.assertIn("White's Qxd6 captures Black's queen", result["evidence"][0]["text"])

    def test_later_mate_is_a_shown_continuation_not_an_immediate_forced_finish(self):
        result = self.check_packet(chess.Board(), row("f2f3", ["f2f3", "e7e5", "g2g4", "d8h4"], ["e2e4"], "allows_mate"))
        self.assertIn("in the shown line, checkmating White", result["dialogue"])
        self.assertNotRegex(result["dialogue"], r"can finish|forced|inevitable|unavoidable")

    def test_capture_and_recapture_are_not_free_material(self):
        board = chess.Board("4k3/8/2p5/3p4/4P3/8/8/4K3 w - - 0 1")
        result = self.check_packet(board, row("e4d5", ["e4d5", "c6d5"]))
        self.assertIn("Black can recapture a pawn with cxd5", result["dialogue"])
        self.assertIn("both sides", result["dialogue"])
        self.assertFalse(any(item["kind"] == "material" for item in result["evidence"]))
        short = self.check_packet(board, row("e4d5", ["e4d5"]))
        self.assertNotRegex(short["dialogue"], r"free|wins|gain")
        self.assertIn("reply still needs checking", short["dialogue"])

    def test_capture_elsewhere_is_not_called_recapture(self):
        board = chess.Board("r3k3/8/8/3p4/4P3/8/P7/4K3 w - - 0 1")
        result = self.check_packet(board, row("e4d5", ["e4d5", "a8a2"]))
        self.assertNotIn("recapture", result["dialogue"])
        self.assertIn("Rxa2", result["dialogue"])

    def test_mate_distinguishes_actual_finish_allowed_threat_and_missed_attack(self):
        board = chess.Board()
        for uci in ("f2f3", "e7e5"):
            board.push_uci(uci)
        allowed = self.check_packet(board, row("g2g4", ["g2g4", "d8h4"], ["b1c3"], "allows_mate"))
        self.assertIn("Black can finish with Qh4#", allowed["dialogue"])
        self.assertIn("White's king", allowed["hint"])
        board.push_uci("g2g4")
        actual = self.check_packet(board, row("d8h4", ["d8h4"], kind="best"))
        self.assertEqual(actual["dialogue"], "Black's Qh4# is checkmate. White has no legal reply.")
        self.assertEqual(len(actual["evidence"]), 1)
        board = chess.Board("7k/5K2/6Q1/8/8/8/8/8 w - - 0 1")
        missed = self.check_packet(board, row("g6g5", ["g6g5"], ["g6g7"], "missed_mate"))
        self.assertIn("White had Qg7#, checkmate immediately", missed["dialogue"])
        self.assertIn("Black's king", missed["hint"])

    def test_invalid_or_different_played_line_cannot_explain_actual_move(self):
        for bad in (["d2d4"], ["e2e4", "e7e6", "e4e6"], [None], "e2e4", []):
            result = self.check_packet(chess.Board(), row("e2e4", bad, ["d2d4"], "best"))
            self.assertEqual(result["mood"], "think")
            self.assertIn("continuation is unverified", result["dialogue"])
            self.assertFalse(any(item["line"] == "played" for item in result["evidence"]))

    def test_special_moves_are_literal_without_safety_or_profit_claims(self):
        fixtures = [
            ("r3k2r/8/8/8/8/8/8/R3K2R b KQkq - 0 1", "e8c8", "king to c8 and rook to d8"),
            ("4k3/8/8/3pP3/8/8/8/4K3 w - d6 0 1", "e5d6", "Black's pawn on d5"),
            ("1r2k3/P7/8/8/8/8/8/4K3 w - - 0 1", "a7b8n", "Black's rook and promotes to a knight"),
        ]
        for fen, uci, observation in fixtures:
            result = self.check_packet(chess.Board(fen), row(uci, [uci], kind="best"))
            self.assertIn(observation, result["dialogue"])
            self.assertNotRegex(result["dialogue"], r"safe|wins|gain|forced")

    def test_report_is_short_english_factual_and_excludes_unready_lessons(self):
        rows = [{"ply": 1, "color": "white", "classification": "blunder"},
                {"ply": 2, "color": "black", "classification": "best"},
                {"ply": 3, "color": "white", "classification": "pending"}]
        report = describe_report(rows, [1])
        self.assertEqual(report["version"], COACH_VERSION)
        self.assertEqual(COACH_VERSION, 2)
        self.assertEqual(report["lesson_plies"], [1, 2])
        self.assertIn("White has 1", report["summary"])
        self.assertIn("Black has 0", report["summary"])
        self.assertIn("So far", report["summary"])
        self.assertLessEqual(len(report["summary"].split()), 35)
        self.assertNotRegex(report["summary"], r"[가-힣]|opening skill|endgame skill")


if __name__ == "__main__":
    unittest.main()
