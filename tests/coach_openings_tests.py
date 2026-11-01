"""Actual-history opening context and provisional score policy, without engines."""
import copy
import hashlib
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import chess

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import coach_openings as coach
import opening_guide as guide


ITALIAN = "e4 e5 Nf3 Nc6 Bc4 Nf6 d3 Bc5 O-O d6 c3 O-O"
FAMILIES = (
    ("e4 c5", "Sicilian Defense", "sicilian"),
    ("e4 e6 d4 d5", "French Defense", "french"),
    ("e4 c6 d4 d5", "Caro-Kann Defense", "caro"),
    ("e4 e5", "King's Pawn Game", "open_game"),
    ("e4 e5 Nf3 Nc6 Bc4", "Italian Game", "italian"),
    ("e4 e5 Nf3 Nc6 Bb5", "Ruy Lopez", "ruy"),
    ("e4 e5 Nf3 Nc6 d4", "Scotch Game", "scotch"),
    ("e4 e5 Nf3 Nf6", "Petrov's Defense", "petrov"),
    ("d4 d5 c4", "Queen's Gambit", "qg"),
    ("d4 d5 c4 e6", "Queen's Gambit Declined", "qgd"),
    ("d4 d5 c4 dxc4", "Queen's Gambit Accepted", "qga"),
    ("d4 d5 c4 c6", "Slav Defense", "slav"),
    ("d4 d5 Bf4", "London System", "london"),
    ("d4 Nf6 c4 g6 Nc3 Bg7 e4 d6", "King's Indian Defense", "kid"),
    ("d4 Nf6 c4 e6 Nc3 Bb4", "Nimzo-Indian Defense", "nimzo"),
    ("d4 Nf6 c4 g6 Nc3 d5", "Grünfeld Defense", "grunfeld"),
    ("Nf3", "Réti Opening", "reti"),
    ("c4", "English Opening", "english"),
)


def source(sans, cp=20, depth=6, classification="good"):
    after = chess.Board()
    for san in sans.split():
        after.push_san(san)
    before = after.copy(stack=True)
    move = before.pop()
    frames = [{"fen": before.fen(en_passant="fen")}, {"fen": after.fen(en_passant="fen")}]
    row = {"ply": len(after.move_stack), "uci": move.uci(), "san": "FORGED_SAN", "classification": classification,
           "fen_before": before.fen(en_passant="fen"), "fen_after": after.fen(en_passant="fen"),
           "played": {"uci": move.uci(), "pv_uci": [move.uci()], "san": "FORGED_SAN", "pv_san": ["FORGED_SAN"],
                      "pv_frames": frames, "bound": "exact", "score_cp_white": cp, "depth": depth, "mate_white": None},
           "best": {"uci": "a1a8", "pv_uci": ["a1a8"], "score_cp_white": -9999, "depth": 99, "bound": "exact"}}
    return before, row


class CoachOpeningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = guide._catalog()

    def explain(self, before, row):
        saved = copy.deepcopy(row)
        fen, moves = before.fen(en_passant="fen"), tuple(before.move_stack)
        result = coach.describe_opening(before, row)
        self.assertEqual(row, saved)
        self.assertEqual(before.fen(en_passant="fen"), fen)
        self.assertEqual(tuple(before.move_stack), moves)
        if result is None:
            return None
        after = before.copy(stack=True)
        after.push_uci(row["uci"])
        self.assertEqual(result["fen"], after.fen(en_passant="fen"))
        self.assertEqual(result["ply_index"], len(after.move_stack))
        self.assertLessEqual(len(result["summary"].split()), 40)
        self.assertEqual([section["id"] for section in result["sections"]], ["white", "black", "balance"])
        for section in result["sections"]:
            self.assertLessEqual(len(section["text"].split()), 80)
            self.assertNotIn("FORGED", section["text"])
        self.assertTrue(result["white"]["name"])
        self.assertTrue(result["black"]["name"])
        self.assertTrue(result["sources"])
        self.assertTrue(all(url.startswith("https://") for url in result["sources"]))
        self.assertTrue(all(square in chess.SQUARE_NAMES for square in result["focus_squares"]))
        self.assertLessEqual(result["recognition"]["matched_ply"], result["ply_index"])
        return result

    def test_eighteen_major_families_use_real_catalog_names_and_sources(self):
        for sans, expected, family in FAMILIES:
            with self.subTest(sans=sans):
                result = self.explain(*source(sans))
                self.assertIn(expected, result["recognition"]["name"])
                self.assertEqual(result["recognition"]["scope"], "current")
                self.assertIn(coach.SOURCE_ROOT + coach.PROFILES[family][2], result["sources"])

    def test_reti_entry_is_not_a_completed_sicilian_and_later_transposes(self):
        invitation = self.explain(*source("Nf3 c5"))
        self.assertEqual(invitation["white"]["name"], "Réti Opening")
        self.assertIn("Sicilian Invitation", invitation["recognition"]["name"])
        result = self.explain(*source("Nf3 c5 e4 d6 d4 cxd4 Nxd4 Nf6 Nc3 a6 Be2 e5"))
        self.assertEqual(result["white"]["name"], "Open Sicilian")
        self.assertIn("Réti", result["summary"])
        self.assertIn("Sicilian", result["summary"])
        self.assertEqual(result["recognition"]["match_type"], "transposition")

    def test_same_position_from_different_move_orders_has_same_opening_family(self):
        pairs = (
            ("Nf3 c5 e4 d6", "e4 c5 Nf3 d6"),
            ("Nf3 d5 d4 Nf6 c4 e6 Nc3 Nc6", "d4 d5 c4 e6 Nf3 Nf6 Nc3 Nc6"),
            ("c4 e6 Nc3 d5 d4 Nf6", "d4 d5 c4 e6 Nc3 Nf6"),
        )
        for first, second in pairs:
            a, b = self.explain(*source(first)), self.explain(*source(second))
            self.assertEqual(" ".join(a["fen"].split()[:4]), " ".join(b["fen"].split()[:4]))
            self.assertEqual(coach._family(a["recognition"]["name"]), coach._family(b["recognition"]["name"]))

    def test_last_known_name_is_explicit_and_undo_restores_prior_context(self):
        prior = self.explain(*source("Nf3"))
        later = self.explain(*source("Nf3 h5"))
        self.assertEqual(later["recognition"]["scope"], "last_known")
        self.assertEqual(later["recognition"]["matched_ply"], 1)
        self.assertIn("Last recognized at ply 1", later["summary"])
        self.assertEqual(self.explain(*source("Nf3")), prior)

    def test_full_standard_history_required_custom_fen_is_not_guessed(self):
        before, row = source(ITALIAN)
        self.assertIsNone(self.explain(chess.Board(before.fen(en_passant="fen")), row))
        partial = before.copy(stack=2)
        self.assertIsNone(self.explain(partial, row))
        before.chess960 = True
        self.assertIsNone(self.explain(before, row))

    def test_actual_move_and_frame_identity_are_validated(self):
        before, original = source(ITALIAN)
        for field, value in (("uci", "e1e8"), ("fen_before", chess.STARTING_FEN), ("fen_after", chess.STARTING_FEN), ("ply", 4)):
            row = copy.deepcopy(original)
            row[field] = value
            self.assertIsNone(self.explain(before, row))

    def test_horizon_includes_ply_24_but_not_25(self):
        board = chess.Board()
        for san in ITALIAN.split():
            board.push_san(san)
        while len(board.move_stack) < 25:
            for move in sorted(board.legal_moves, key=lambda move: move.uci()):
                board.push(move)
                if not board.is_game_over(claim_draw=False) and not board.is_repetition(2):
                    break
                board.pop()
            else:
                self.fail("synthetic extension could not continue")
        for size in (24, 25):
            after = chess.Board()
            for move in board.move_stack[:size]:
                after.push(move)
            before = after.copy(stack=True)
            actual = before.pop()
            row = {"uci": actual.uci(), "ply": size}
            result = self.explain(before, row)
            self.assertEqual(result is not None, size == 24)

    def test_before_twelve_plies_never_awards_an_edge_even_with_large_score(self):
        result = self.explain(*source("e4 c5 Nf3 d6 d4 cxd4 Nxd4 Nf6 Nc3 a6 Be2", cp=900, depth=20))
        self.assertEqual(result["assessment"]["status"], "too_early")
        self.assertIsNone(result["assessment"]["side"])

    def test_twelve_ply_thresholds_sign_and_all_depths_are_provisional(self):
        for depth in (4, 6, 8, 16):
            for cp in (-200, -100, -99, -50, -49, 0, 49, 50, 99, 100, 200):
                with self.subTest(depth=depth, cp=cp):
                    result = self.explain(*source(ITALIAN, cp=cp, depth=depth))
                    assessment = result["assessment"]
                    expected = "balanced" if abs(cp) < 50 else "slight_edge" if abs(cp) < 100 else "better_start"
                    self.assertEqual(assessment["status"], expected)
                    self.assertEqual(assessment["side"], None if abs(cp) < 50 else "white" if cp > 0 else "black")
                    self.assertTrue(assessment["provisional"])
                    self.assertEqual(assessment["score_cp_white"], cp)
                    self.assertIn(f"{cp / 100:+.2f}", result["sections"][2]["text"])
                    self.assertIn("provisional", result["sections"][2]["text"])

    def test_shallow_exact_estimate_is_shown_without_an_opening_verdict(self):
        result = self.explain(*source(ITALIAN, cp=-27, depth=3))
        assessment = result["assessment"]
        self.assertEqual((assessment["status"], assessment["reason"]), ("unavailable", "shallow_score"))
        self.assertIsNone(assessment["side"])
        self.assertIn("-0.27", result["sections"][2]["text"])
        self.assertIn("close to level", result["sections"][2]["text"])

    def test_uncertain_pending_and_unknown_grades_keep_names_but_withhold_verdict(self):
        for kind in (None, "uncertain", "pending", "unknown"):
            result = self.explain(*source(ITALIAN, cp=999, depth=12, classification=kind))
            self.assertTrue(result["recognition"]["name"])
            self.assertEqual(result["assessment"]["status"], "unavailable")
            self.assertIsNone(result["assessment"]["side"])

    def test_best_score_never_controls_actual_position_assessment(self):
        before, row = source(ITALIAN, cp=20, depth=6)
        baseline = self.explain(before, row)
        for best in (None, {"score_cp_white": 999999, "depth": 99}, {"score_cp_white": -999999, "mate_white": -1}):
            row["best"] = best
            self.assertEqual(self.explain(before, row), baseline)

    def test_invalid_bounded_missing_or_mate_score_never_awards_edge(self):
        before, original = source(ITALIAN, cp=300, depth=8)
        variants = []
        for field, value in (("bound", "upper"), ("bound", "lower"), ("bound", None), ("depth", None),
                             ("depth", -1), ("depth", True), ("score_cp_white", None), ("score_cp_white", False), ("mate_white", 3),
                             ("uci", "a1a8"), ("pv_uci", ["a1a8"]), ("pv_frames", [])):
            row = copy.deepcopy(original)
            row["played"][field] = value
            variants.append(row)
        row = copy.deepcopy(original)
        row["played"]["pv_uci"].append("e1e8")
        del row["played"]["pv_frames"]
        variants.append(row)
        row = copy.deepcopy(original)
        row["played"]["pv_frames"][0]["fen"] = chess.STARTING_FEN
        variants.append(row)
        row = copy.deepcopy(original)
        row["played"] = None
        variants.append(row)
        for row in variants:
            result = self.explain(before, row)
            self.assertEqual(result["assessment"]["status"], "unavailable")
            self.assertIsNone(result["assessment"]["side"])

    def test_actual_mate_and_automatic_draw_override_early_or_invalid_scores(self):
        result = self.explain(*source("f3 e5 g4 Qh4#", cp=999, depth=1, classification="uncertain"))
        self.assertEqual((result["assessment"]["status"], result["assessment"]["side"]), ("checkmate", "black"))
        self.assertIn("checkmate for Black", result["summary"])
        self.assertFalse(result["assessment"]["provisional"])
        repeated = "Nf3 Nf6 Ng1 Ng8 " * 4
        result = self.explain(*source(repeated, cp=999, depth=20))
        self.assertEqual(result["assessment"]["status"], "draw")
        self.assertIn("fivefold repetition", result["summary"])

    def test_claimable_threefold_is_not_called_automatic_game_end(self):
        result = self.explain(*source("Nf3 Nf6 Ng1 Ng8 " * 2))
        self.assertNotEqual(result["assessment"]["status"], "draw")

    def test_sicilian_open_closed_and_later_transition_follow_actual_pawns(self):
        closed = self.explain(*source("e4 c5 Nc3 Nc6 g3 g6 Bg2 Bg7 d3 d6 f4 e6"))
        self.assertEqual(closed["white"]["name"], "Closed Sicilian setup")
        opened = self.explain(*source("e4 c5 Nc3 Nc6 Nf3 d6 d4 cxd4 Nxd4 Nf6 Be2 e5"))
        self.assertEqual(opened["white"]["name"], "Open Sicilian")
        self.assertNotIn("White has kept the original d-pawn on d2", opened["sections"][0]["text"])
        queen = self.explain(*source("e4 c5 Nf3 d6 d4 cxd4 Qxd4 Nc6 Bb5 Bd7 Bxc6 Bxc6"))
        self.assertEqual(queen["white"]["name"], "Open Sicilian")

    def test_alapin_pawn_recapture_is_not_mislabeled_open_sicilian(self):
        result = self.explain(*source("e4 c5 c3 Nf6 e5 Nd5 d4 cxd4 cxd4 d6 Nf3 Nc6"))
        self.assertIn("Alapin", result["recognition"]["name"])
        self.assertNotEqual(result["white"]["name"], "Open Sicilian")

    def test_french_advance_and_exchange_have_different_verified_plans(self):
        advance = self.explain(*source("e4 e6 d4 d5 e5 c5 c3 Nc6 Nf3 Qb6 Bd3 Bd7"))
        exchange = self.explain(*source("e4 e6 d4 d5 exd5 exd5 Nf3 Nf6 Bd3 Bd6 O-O O-O"))
        self.assertEqual(advance["white"]["name"], "French Advance center")
        self.assertEqual(exchange["white"]["name"], "French Exchange setup")
        self.assertIn("d4-e5 pawn chain", advance["sections"][0]["text"])
        self.assertIn("already attacks the base on d4", advance["sections"][1]["text"])
        self.assertNotEqual(advance["sections"][0]["text"], exchange["sections"][0]["text"])
        self.assertNotIn("chain that isn't present", exchange["sections"][1]["text"])

    def test_jobava_alias_does_not_assume_c3_pawn_setup(self):
        result = self.explain(*source("d4 Nf6 Nc3 d5 Bf4 e6 e3 c5 Nf3 Nc6 Bb5 Bd6"))
        self.assertEqual(result["white"]["name"], "Rapport-Jobava System")
        self.assertIn("early Nc3 changes", result["sections"][0]["text"])

    def test_nimzo_queen_recapture_and_semi_slav_are_not_stale_current_claims(self):
        nimzo = self.explain(*source("d4 Nf6 c4 e6 Nc3 Bb4 Qc2 O-O a3 Bxc3+ Qxc3 d5"))
        self.assertIn("put pressure", nimzo["summary"])
        self.assertNotIn("pressures the c3-knight", nimzo["summary"])
        self.assertIn("queen recapture does not create doubled pawns", nimzo["sections"][0]["text"])
        slav = self.explain(*source("d4 d5 c4 c6 Nf3 Nf6 Nc3 e6 e3 Nbd7 Bd3 dxc4"))
        self.assertEqual(slav["black"]["name"], "Semi-Slav Defense")
        self.assertIn("added ...e6", slav["sections"][1]["text"])

    def test_shared_indian_start_and_other_defenses_keep_neutral_roles(self):
        for sans in ("d4 Nf6 c4 g6", "e4 d6 d4 Nf6 Nc3 g6", "e4 d5"):
            result = self.explain(*source(sans))
            self.assertEqual((result["white"]["name"], result["black"]["name"]), ("White setup", "Black response"))
            self.assertNotIn("kingside attack", result["summary"])

    def test_same_words_in_unrelated_opening_names_do_not_select_wrong_family(self):
        self.assertIsNone(coach._family("Grob Opening: London Defense"))
        self.assertIsNone(coach._family("Scandinavian Defense: Grünfeld Variation"))
        self.assertIsNone(coach._family("King's Gambit Declined: Petrov's Defense"))
        self.assertEqual(coach._family("Sicilian Defense: French Variation"), "sicilian")

    def test_unsupported_transposed_family_keeps_its_real_name(self):
        result = self.explain(*source("e4 e5 Nf3 Nf6 Bc4 Nxe4 Nc3 Nxc3 dxc3 d6 O-O Be7"))
        self.assertIn("Bishop's Opening", result["recognition"]["name"])
        self.assertEqual(result["white"]["name"], "White setup")

    def test_long_summary_fallback_does_not_invent_reti_entry(self):
        original = coach.PROFILES["english"]
        long = (*original[:3], "A verified opening description " * 30, *original[4:])
        with patch.dict(coach.PROFILES, {"english": long}):
            result = self.explain(*source("c4"))
        self.assertNotIn("Nf3", result["summary"])
        self.assertNotIn("Réti", result["summary"])

    def test_sources_and_literal_observation_do_not_use_best_eval(self):
        result = self.explain(*source("d4 d5 c4 e6 Nf3 Nf6 Nc3 Be7 e3 O-O Bd3 c5", cp=-27, depth=6))
        self.assertIn(coach.SOURCE_ROOT + "Queens-Gambit", result["sources"])
        self.assertIn("-0.27", result["sections"][2]["text"])
        self.assertIn("roughly balanced", result["sections"][2]["text"])
        self.assertIn("White has a pawn on d4", result["sections"][2]["text"])
        self.assertIn("Black has a pawn on d5", result["sections"][2]["text"])

    def test_optional_catalog_failure_does_not_break_other_coaching(self):
        before, row = source(ITALIAN)
        with patch.object(coach, "_catalog", side_effect=guide.CatalogError("missing checksum-protected data")):
            self.assertIsNone(self.explain(before, row))

    def test_no_continuation_enumeration_and_catalog_bytes_unchanged(self):
        paths = [ROOT / "data/openings" / name for name in ("a.tsv", "b.tsv", "c.tsv", "d.tsv", "e.tsv", "LICENSE", "manifest.json")]
        hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
        with patch.object(guide, "_continuations", side_effect=AssertionError("must not enumerate")), \
             patch.object(guide, "get_opening_guide", side_effect=AssertionError("must use named index")):
            self.assertIsNotNone(self.explain(*source(ITALIAN)))
        self.assertEqual({path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}, hashes)

    def test_returned_payload_does_not_mutate_shared_catalog_or_future_response(self):
        before, row = source(ITALIAN)
        expected = self.explain(before, row)
        changed = self.explain(before, row)
        changed["recognition"]["name"] = "Mutated"
        changed["sections"][0]["text"] = "Mutated"
        changed["sources"].clear()
        self.assertEqual(self.explain(before, row), expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
