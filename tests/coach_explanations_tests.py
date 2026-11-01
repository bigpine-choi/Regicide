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
        self.assertEqual(set(packet["perspectives"]), {"white", "black"})
        for view in packet["perspectives"].values():
            self.assertLessEqual(len(view["dialogue"].split()), 40)
            self.assertLessEqual(view["dialogue"].count("."), 3 if view.get("teaching") else 2)
            self.assertLessEqual(len(view["hint"].split()), 16)
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
                for view in packet["perspectives"].values():
                    self.assertNotIn(uci, view["hint"])
                    self.assertNotIn(uci[2:4], view["hint"])
                    if move in board.legal_moves:
                        self.assertNotIn(board.san(move), view["hint"])
        return packet

    def test_positive_moves_ignore_remote_capture_and_castle_in_dialogue_and_evidence(self):
        for pv in (["e2e4", "d7d5", "e4d5", "d8d5"],
                   ["e2e4", "e7e5", "g1f3", "b8c6", "f1c4", "f8c5", "d2d3", "g8f6", "e1g1"]):
            result = self.check_packet(chess.Board(), row("e2e4", pv))
            self.assertIn("White takes space in the center", result["dialogue"])
            self.assertNotIn("capture", result["dialogue"])
            self.assertNotIn("O-O", result["dialogue"])
            self.assertTrue(all(fact["ply_index"] <= 2 for fact in result["evidence"]))
            self.assertFalse(any(fact["kind"] in ("capture", "castle") for fact in result["evidence"]))

    def test_black_development_names_only_immediate_reply(self):
        board = chess.Board()
        board.push_uci("e2e4")
        result = self.check_packet(board, row("g8f6", ["g8f6", "e4e5", "f6d5"], kind="best"))
        self.assertIn("Black's knight develops", result["dialogue"])
        self.assertIn("White can attack it with e5", result["dialogue"])
        self.assertIn("retreat ready", result["dialogue"])
        self.assertNotIn("Nd5", result["dialogue"])

    def test_short_tactical_reply_prioritizes_queen_over_pawn_and_keeps_scope(self):
        board = chess.Board("3q2k1/8/3b4/8/8/5N2/7P/3Q2K1 w - - 0 1")
        result = self.check_packet(board, row("d1d3", ["d1d3", "d6h2", "f3h2", "d8d3"], ["d1d4"], "blunder"))
        self.assertIn("After Qd3, Bxh2+ exposes White's queen on the d-file", result["dialogue"])
        self.assertIn("Qd4 gives White's queen protection from the knight", result["dialogue"])
        self.assertNotIn("away from that attack", result["dialogue"])
        self.assertIn("White's queen", result["evidence"][0]["text"])
        self.assertEqual(result["evidence"][0]["ply_index"], 4)
        self.assertIn("d3", result["focus_squares"])
        self.assertNotRegex(result["dialogue"], r"forced|inevitable|pin|fork|material balance")

    def test_same_square_alternative_explains_piece_choice_without_inventing_cause(self):
        board = chess.Board("4k3/8/8/8/8/8/3P4/4KB2 w - - 0 1")
        result = self.check_packet(board, row("d2d3", ["d2d3"], ["f1d3"], "mistake"))
        self.assertIn("bishop could use d3, but the pawn takes that square", result["dialogue"])
        self.assertIn("Bd3 brings White's bishop into play", result["dialogue"])

    def test_black_tactical_loss_keeps_actor_and_victim_correct(self):
        board = chess.Board("3q2k1/8/3b4/8/8/5N2/7P/3Q2K1 w - - 0 1").mirror()
        result = self.check_packet(board, row("d8d6", ["d8d6", "d3h7", "f6h7", "d1d6"], ["d8d5"], "blunder"))
        self.assertIn("After Qd6", result["dialogue"])
        self.assertIn("exposes Black's queen", result["dialogue"])
        self.assertIn("Qd5 gives Black's queen protection from the knight", result["dialogue"])
        self.assertIn("exposes your queen", result["perspectives"]["black"]["dialogue"])
        self.assertIn("exposes Black's queen", result["perspectives"]["white"]["dialogue"])
        self.assertIn("White's Qxd6 captures Black's queen", result["evidence"][0]["text"])

    def test_later_mate_is_a_shown_continuation_not_an_immediate_forced_finish(self):
        result = self.check_packet(chess.Board(), row("f2f3", ["f2f3", "e7e5", "g2g4", "d8h4"], ["e2e4"], "allows_mate"))
        self.assertIn("shown line after f3 ends in checkmate against White's king", result["dialogue"])
        self.assertNotRegex(result["dialogue"], r"can finish|forced|inevitable|unavoidable")

    def test_capture_and_recapture_are_not_free_material(self):
        board = chess.Board("4k3/8/2p5/3p4/4P3/8/8/4K3 w - - 0 1")
        result = self.check_packet(board, row("e4d5", ["e4d5", "c6d5"]))
        self.assertIn("Black can recapture the pawn with cxd5", result["dialogue"])
        self.assertIn("Count everything that comes off", result["dialogue"])
        self.assertFalse(any(item["kind"] == "material" for item in result["evidence"]))
        short = self.check_packet(board, row("e4d5", ["e4d5"]))
        self.assertNotRegex(short["dialogue"], r"free|wins|gains")
        self.assertIn("Check for a recapture", short["dialogue"])

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
        self.assertIn("Black can finish immediately with Qh4#", allowed["dialogue"])
        self.assertIn("White's king", allowed["hint"])
        board.push_uci("g2g4")
        actual = self.check_packet(board, row("d8h4", ["d8h4"], kind="best"))
        self.assertIn("That's checkmate: there isn't a legal reply", actual["dialogue"])
        self.assertEqual(len(actual["evidence"]), 1)
        board = chess.Board("7k/5K2/6Q1/8/8/8/8/8 w - - 0 1")
        missed = self.check_packet(board, row("g6g5", ["g6g5"], ["g6g7"], "missed_mate"))
        self.assertIn("Try Qg7#: that's checkmate right now", missed["dialogue"])
        self.assertIn("Black's king", missed["hint"])

    def test_invalid_or_different_played_line_cannot_explain_actual_move(self):
        for bad in (["d2d4"], ["e2e4", "e7e6", "e4e6"], [None], "e2e4", []):
            result = self.check_packet(chess.Board(), row("e2e4", bad, ["d2d4"], "best"))
            self.assertEqual(result["mood"], "think")
            self.assertIn("continuation doesn't check out", result["dialogue"])
            self.assertFalse(any(item["line"] == "played" for item in result["evidence"]))

    def test_special_moves_are_literal_without_safety_or_profit_claims(self):
        fixtures = [
            ("r3k2r/8/8/8/8/8/8/R3K2R b KQkq - 0 1", "e8c8", "Black has castled queenside"),
            ("4k3/8/8/3pP3/8/8/8/4K3 w - d6 0 1", "e5d6", "Black's pawn on d5"),
            ("1r2k3/P7/8/8/8/8/8/4K3 w - - 0 1", "a7b8n", "turns the pawn into a knight and takes Black's rook"),
        ]
        for fen, uci, observation in fixtures:
            result = self.check_packet(chess.Board(fen), row(uci, [uci], kind="best"))
            self.assertIn(observation, result["dialogue"])
            self.assertNotRegex(result["dialogue"], r"safe|wins|gains|forced")

    def test_report_is_short_english_factual_and_excludes_unready_lessons(self):
        rows = [{"ply": 1, "color": "white", "classification": "blunder"},
                {"ply": 2, "color": "black", "classification": "best"},
                {"ply": 3, "color": "white", "classification": "pending"}]
        report = describe_report(rows, [1])
        self.assertEqual(report["version"], COACH_VERSION)
        self.assertEqual(COACH_VERSION, 4)
        self.assertEqual(report["lesson_plies"], [1, 2])
        self.assertIn("White has 1", report["summary"])
        self.assertIn("Black has 0", report["summary"])
        self.assertIn("So far", report["summary"])
        self.assertLessEqual(len(report["summary"].split()), 35)
        self.assertNotRegex(report["summary"], r"[가-힣]|opening skill|endgame skill")

    def test_perspectives_address_the_user_without_switching_actor_or_ownership(self):
        board = chess.Board()
        for uci in ("e2e4", "c7c5", "d1h5"):
            board.push_uci(uci)
        source = row("g8f6", ["g8f6", "h5h4"], kind="best")
        original = json.dumps(source, sort_keys=True)
        result = self.check_packet(board, source)
        white, black = (result["perspectives"][side]["dialogue"] for side in ("white", "black"))
        self.assertIn("Black develops a knight with an attack on your queen", white)
        self.assertIn("You develop a knight with an attack on White's queen", black)
        self.assertNotIn("your queen", black)
        self.assertIn("h5", result["focus_squares"])
        self.assertEqual(json.dumps(source, sort_keys=True), original)
        # The UI can switch perspective without reanalysis or a mutated report.
        self.assertEqual(describe_move(board, source), result)

    def test_retreat_is_only_called_an_escape_if_that_attacker_no_longer_attacks(self):
        board = chess.Board("2r4k/8/8/8/2R5/8/P7/7K w - - 0 1")
        still_attacked = self.check_packet(board, row("a2a3", ["a2a3", "c8c4"], ["c4c5"], "blunder"))
        escaped = self.check_packet(board, row("a2a3", ["a2a3", "c8c4"], ["c4b4"], "blunder"))
        self.assertNotIn("away from that attack", still_attacked["dialogue"])
        self.assertIn("Rc5 attacks Black's rook on c8", still_attacked["dialogue"])
        self.assertNotIn("out of that rook's attack", still_attacked["dialogue"])
        self.assertIn("Rb4 moves White's rook out of that rook's attack", escaped["dialogue"])

    def test_pinned_piece_does_not_supply_false_attack_or_protection(self):
        board = chess.Board("7k/8/8/8/1r1b4/8/1B6/K7 w - - 0 1")
        # Bc3 remains pinned on the a1-d4 diagonal; its attack on Rb4 is not usable.
        self.assertTrue(board.is_pinned(chess.WHITE, chess.B2))
        result = self.check_packet(board, row("b2c3", ["b2c3"], kind="best"))
        self.assertFalse(any(fact["kind"] == "attack" and "b4" in fact["squares"] for fact in result["evidence"]))
        self.assertNotIn("rook", result["dialogue"])
        # A pinned defender cannot be called protection for an alternate queen square.
        board = chess.Board("4r2k/8/8/8/8/8/4N3/3QK3 w - - 0 1")
        result = self.check_packet(board, row("d1d3", ["d1d3"], ["d1c3"], "mistake"))
        self.assertNotIn("protection from the knight", result["dialogue"])

    def test_direct_advice_varies_with_verified_chess_events(self):
        fixtures = [(chess.Board(), row("e2e4", ["e2e4", "g8f6"]))]
        board = chess.Board()
        for uci in ("e2e4", "c7c5", "d1h5"):
            board.push_uci(uci)
        fixtures.append((board, row("g8f6", ["g8f6", "h5h4"], kind="best")))
        fixtures.append((chess.Board("3q2k1/8/3b4/8/8/2P2N2/7P/3Q2K1 w - - 0 1"),
                         row("d1d3", ["d1d3", "d6h2", "f3h2", "d8d3"], ["d1d4"], "blunder")))
        texts = [self.check_packet(board, source)["perspectives"]["white"]["dialogue"] for board, source in fixtures]
        self.assertEqual(len({text.split(".")[0] for text in texts}), 3)
        self.assertTrue(all(16 <= len(text.split()) <= 40 for text in texts))
        self.assertTrue(any("c-pawn" in text for text in texts))
        for text in texts:
            self.assertNotRegex(text, r"Good move|Watch for|evaluation|centipawn|forced|fork|pin|free")

    def test_discovered_check_does_not_name_the_moving_piece_as_checker(self):
        for board, uci, checker in ((chess.Board("k7/8/8/8/8/8/N7/R6K w - - 0 1"), "a2c3", "a1"),
                                    (chess.Board("k7/8/8/8/8/8/N7/R6K w - - 0 1").mirror(), "a7c6", "a8")):
            source = row(uci, [uci], kind="good")
            result = self.check_packet(board, source)
            board.push_uci(uci)
            self.assertEqual(list(board.checkers()), [chess.parse_square(checker)])
            for text in [result["dialogue"], *(view["dialogue"] for view in result["perspectives"].values())]:
                self.assertIn("That move gives check", text)
                self.assertNotIn("knight gives check", text)

    def test_attacked_queen_capturing_the_attacker_is_an_exchange_not_an_escape(self):
        initial = chess.Board("3q2k1/8/8/8/8/4P3/8/3Q2K1 w - - 0 1")
        fixtures = [(initial, ["d1d4", "d8d4", "e3d4"], "white", "Black", "Qxd4", "exd4"),
                    (initial.mirror(), ["d8d5", "d1d5", "e6d5"], "black", "White", "Qxd5", "exd5")]
        for board, pv, perspective, opponent, capture, recapture in fixtures:
            result = self.check_packet(board, row(pv[0], pv))
            dialogue = result["perspectives"][perspective]["dialogue"]
            self.assertIn(f"{opponent} can take your queen with {capture}", dialogue)
            self.assertIn(f"{recapture} recaptures the queen", dialogue)
            for view in (result, *result["perspectives"].values()):
                self.assertNotIn("out of the queen's reach", view["dialogue"])
                self.assertNotRegex(view["dialogue"], r"wins|free|forced")
            short = self.check_packet(board, row(pv[0], pv[:2]))
            self.assertIn("Check for a recapture", short["dialogue"])
            self.assertNotIn(recapture, short["dialogue"])

    def test_protection_after_promotion_names_the_resulting_piece(self):
        initial = chess.Board("k2r4/4PN2/8/8/8/8/8/7K w - - 0 1")
        for board, actual, best, perspective in ((initial, "e7e8r", "e7d8q", "white"),
                                                (initial.mirror(), "e2e1r", "e2d1q", "black")):
            result = self.check_packet(board, row(actual, [actual], [best], "mistake"))
            self.assertIn("your queen protection from the knight", result["perspectives"][perspective]["dialogue"])
            for view in (result, *result["perspectives"].values()):
                self.assertNotIn("pawn protection", view["dialogue"])


if __name__ == "__main__":
    unittest.main()
