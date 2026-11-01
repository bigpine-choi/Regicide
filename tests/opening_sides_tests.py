"""Side roles, provisional names and display-only official supplements."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import chess
import opening_guide as guide
from opening_presentation import OpeningGuideCache
import opening_variety


def board(line):
    value = chess.Board()
    for san in line.split():
        value.push_san(san)
    return value


def sides(line):
    return guide.get_opening_guide(board(line))["sides"]


class OpeningSidesTests(unittest.TestCase):
    def test_first_move_and_response_wait_are_independent(self):
        before = sides("")
        self.assertEqual(before["white"]["status"], "pending")
        self.assertEqual(before["black"]["status"], "pending")
        after = sides("e4")
        self.assertEqual(after["white"]["name"], "King's Pawn Opening")
        self.assertIsNone(after["black"]["name"])
        self.assertIn("기다리고", after["black"]["detail"])

    def test_common_opening_roles_are_not_two_unrelated_names(self):
        cases = [
            ("e4 c5", "King's Pawn Opening", "Sicilian Defense"),
            ("e4 e6", "King's Pawn Opening", "French Defense"),
            ("e4 c6", "King's Pawn Opening", "Caro-Kann Defense"),
            ("e4 d6", "King's Pawn Opening", "Pirc Defense"),
            ("d4 Nf6", "Queen's Pawn Opening", "Indian Game"),
            ("d4 Nf6 c4 g6", "Queen's Pawn Opening", "King's Indian Defense"),
            ("e4 e5 Nf3 Nc6 Bb5 a6", "Ruy Lopez Opening", "Morphy Defense"),
            ("e4 e5 Nf3 Nc6 Bb5 Nf6", "Ruy Lopez Opening", "Berlin Defense"),
            ("e4 e5 Nf3 Nc6 Bc4 Bc5", "Italian Game", "Giuoco Piano"),
            ("e4 e5 Nf3 Nc6 Bc4 Nf6", "Italian Game", "Two Knights Defense"),
            ("d4 d5 c4 e6", "Queen's Gambit", "Queen's Gambit Declined"),
            ("d4 d5 c4 dxc4", "Queen's Gambit", "Queen's Gambit Accepted"),
            ("d4 d5 c4 c6", "Queen's Gambit", "Slav Defense"),
        ]
        for line, white, black in cases:
            with self.subTest(line=line):
                value = sides(line)
                self.assertEqual((value["white"]["name"], value["black"]["name"]), (white, black))
                self.assertEqual(value["white"]["status"], "confirmed")
                self.assertEqual(value["black"]["status"], "confirmed")

    def test_white_confirmed_variant_replaces_generic_first_pawn(self):
        cases = [
            ("e4 c5 c3", "Alapin Variation (Sicilian)", "Sicilian Defense"),
            ("e4 c5 Nc3 Nc6 g3", "Closed Sicilian", "Sicilian Defense"),
            ("e4 c5 Nf3 d6 d4 cxd4 Nxd4", "Open Sicilian", "Sicilian Defense"),
            ("e4 e6 d4 d5 e5", "Advance Variation (French)", "French Defense"),
            ("e4 e6 d4 d5 exd5", "Exchange Variation (French)", "French Defense"),
            ("e4 c6 d4 d5 e5", "Advance Variation (Caro-Kann)", "Caro-Kann Defense"),
            ("d4 Nf6 Bf4 g6", "London System", "Indian Game"),
        ]
        for line, white, black in cases:
            with self.subTest(line=line):
                value = sides(line)
                self.assertEqual((value["white"]["name"], value["black"]["name"]), (white, black))

    def test_shared_move_does_not_invent_a_different_defense(self):
        self.assertEqual(sides("e4 c5 Nf3 e6")["black"]["name"], "Sicilian Defense")
        self.assertEqual(sides("Nf3 g6")["black"]["name"], "Kingside Fianchetto Variation")
        self.assertEqual(sides("Nf3 c5")["black"]["name"], "Sicilian Invitation")
        self.assertEqual(sides("c4 c5")["black"]["name"], "Symmetrical English")
        self.assertNotEqual(sides("c4 e5")["black"]["name"], "Sicilian Defense")
        for line in ("e4 e5", "e4 e5 Nf3 Nc6 Bb5", "d4 d5 c4"):
            value = sides(line)
            self.assertIsNone(value["black"]["name"])
            self.assertEqual(value["black"]["status"], "pending")
            self.assertIn("확인한 대응", value["black"]["detail"])

    def test_portuguese_is_whites_opening(self):
        value = sides("e4 e5 Bb5")
        self.assertEqual(value["white"]["name"], "Portuguese Opening")
        self.assertIsNone(value["black"]["name"])

    def test_transposition_updates_roles_from_current_family(self):
        value = sides("Nf3 c5 e4")
        self.assertEqual(value["black"]["name"], "Sicilian Defense")
        self.assertIn("다른 수순", value["black"]["detail"])
        value = sides("Nf3 Nf6 c4")
        self.assertEqual(value["white"]["name"], "English Opening")

    def test_last_known_does_not_claim_current_confirmation(self):
        value = sides("Nf3 h5")
        self.assertEqual(value["white"]["name"], "Réti Opening")
        self.assertEqual(value["white"]["status"], "last_known")
        self.assertEqual(value["white"]["matched_ply"], 1)
        self.assertIn("…h5", value["black"]["detail"])

    def test_fen_only_uses_position_not_historical_choice(self):
        value = guide.get_opening_guide(chess.Board(board("e4 c5").fen()))["sides"]
        for side in ("white", "black"):
            self.assertEqual(value[side]["status"], "position")
            self.assertIn("알 수 없습니다", value[side]["detail"])
        unknown = guide.get_opening_guide(chess.Board("4k3/8/8/8/8/8/8/4K3 w - - 0 1"))["sides"]
        self.assertIsNone(unknown["white"]["name"])
        self.assertEqual(unknown["black"]["status"], "unknown")

    def test_unavailable_is_safe_and_has_both_sides(self):
        with patch("opening_presentation.get_opening_guide", side_effect=OSError("private path")):
            value = OpeningGuideCache().get(board("e4"))["sides"]
        self.assertEqual(value["white"]["status"], "unavailable")
        self.assertEqual(value["black"]["status"], "unavailable")
        self.assertNotIn("private", str(value))

    def test_undo_cache_is_current_and_returns_independent_copies(self):
        value = board("e4 c5")
        cache = OpeningGuideCache()
        first = cache.get(value)
        before = value.fen(), tuple(value.move_stack)
        value.push_san("c3")
        self.assertEqual(cache.get(value)["sides"]["white"]["name"], "Alapin Variation (Sicilian)")
        value.pop()
        self.assertEqual(cache.get(value), first)
        self.assertEqual((value.fen(), tuple(value.move_stack)), before)
        first["sides"]["black"]["name"] = "wrong"
        self.assertEqual(cache.get(value)["sides"]["black"]["name"], "Sicilian Defense")

    def test_modern_supplement_is_current_and_has_no_invented_eco(self):
        value = guide.get_opening_guide(board("d4 d6 c4 g6"))
        self.assertEqual(value["matched_opening"]["name"], "Modern Defense")
        self.assertEqual(value["matched_opening"]["eco"], "")
        self.assertEqual(value["matched_opening"]["name_origin"], "supplemental")
        self.assertEqual(value["sides"]["black"]["name"], "Modern Defense")
        self.assertEqual(value["sides"]["black"]["status"], "confirmed")
        previous = guide.get_opening_guide(board("d4 d6 c4"))
        branch = next(item for item in previous["continuations"] if item["uci"] == "g7g6")
        self.assertIn("Modern Defense", [item["name"] for item in branch["immediate_openings"]])
        self.assertIn("Modern Defense", [item["name"] for item in branch["reachable_openings"]])
        value = guide.get_opening_guide(board("d4 g6 c4 d6"))
        self.assertEqual(value["matched_opening"]["name"], "Modern Defense")
        self.assertEqual(value["matched_opening"]["match_type"], "transposition")

    def test_display_supplement_never_mutates_engine_candidate_catalog(self):
        base = guide._catalog()
        keys, named = dict(base.positions), dict(base.named)
        root = board("d4 d6 c4")
        plan = opening_variety.plan(root, 1000, "ab" * 16)
        guide.get_opening_guide(root)
        guide.get_opening_guide(board("d4 d6 c4 g6"))
        self.assertEqual(opening_variety.plan(root, 1000, "ab" * 16), plan)
        self.assertEqual((len(base.openings), len(base.positions)), (3865, 7983))
        self.assertEqual(base.positions, keys)
        self.assertEqual(base.named, named)
        self.assertNotIn(guide.position_key(board("d4 d6 c4 g6")), base.named)


if __name__ == "__main__":
    unittest.main()
