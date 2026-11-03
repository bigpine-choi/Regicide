"""Source coverage, branch behavior and history boundaries of the gambit graph."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import chess

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import boss_openings
import gambit_knowledge as knowledge


def position(moves):
    board = chess.Board()
    for uci in moves.split():
        board.push_uci(uci)
    return board


EVANS = "e2e4 e7e5 g1f3 b8c6 f1c4 f8c5 b2b4"


class GambitKnowledgeTests(unittest.TestCase):
    def test_broad_pinned_catalog_has_both_colors_and_named_response_branches(self):
        meta = knowledge.metadata()
        self.assertTrue(meta["available"])
        self.assertEqual(meta["source"]["license"], "CC0-1.0")
        self.assertEqual(meta["source"]["commit"], boss_openings.CATALOG_COMMIT)
        self.assertGreaterEqual(meta["counts"]["lines"], 1300)
        self.assertGreaterEqual(meta["counts"]["positions"], 3000)
        self.assertGreater(meta["counts"]["accepted_lines"], 100)
        self.assertGreater(meta["counts"]["declined_lines"], 100)
        self.assertGreater(meta["counts"]["defense_lines"], 25)
        self.assertEqual({row["owner"] for row in knowledge.repertoire()}, {"white", "black"})
        self.assertTrue(meta["candidate_only"])
        self.assertFalse(meta["learned_evaluator"])

    def test_every_source_line_replays_legally_and_its_position_keys_match(self):
        seen = set()
        for row in knowledge.repertoire():
            with self.subTest(line=row["line_id"]):
                self.assertNotIn(row["line_id"], seen)
                seen.add(row["line_id"])
                self.assertEqual(row["source_license"], "CC0-1.0")
                self.assertIn(row["catalog_commit"], row["source_url"])
                self.assertIn(row["stage"], ("offer", "variation", "accepted", "declined", "defense"))
                board = chess.Board()
                for offset, uci in enumerate(row["moves"]):
                    self.assertEqual(knowledge.position_key(board), row["position_keys"][offset])
                    self.assertIn(chess.Move.from_uci(uci), board.legal_moves)
                    board.push_uci(uci)
                self.assertEqual(knowledge.position_key(board), row["position_keys"][-1])

    def test_rebuild_is_byte_reproducible_and_preserves_original_sources(self):
        paths = list((ROOT / "data/openings").glob("*"))
        before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths if p.is_file()}
        with tempfile.TemporaryDirectory() as directory:
            knowledge.build_catalog(directory)
            for name in ("catalog.json", "manifest.json"):
                self.assertEqual((Path(directory) / name).read_bytes(), (knowledge.DIRECTORY / name).read_bytes())
        after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths if p.is_file()}
        self.assertEqual(before, after)

    def test_accepted_evans_proposes_development_and_records_actual_capture(self):
        board = position(EVANS + " c5b4")
        before = board.copy(stack=True)
        rows = knowledge.proposals(board, "evans")
        self.assertIn("c2c3", {row["uci"] for row in rows})
        self.assertTrue(knowledge.context(board)["offer_captured"])
        self.assertEqual(knowledge.context(board)["stage"], "accepted")
        for row in rows:
            self.assertEqual(row["family_id"], "evans")
            self.assertEqual(row["role"], "knowledge_continuation")
            self.assertTrue(row["candidate_only"])
            self.assertEqual(row["loss_cap_cp"], 35)
            self.assertEqual(row["source_next_uci"], row["uci"])
        self.assertEqual(board.fen(), before.fen())
        self.assertEqual(board.move_stack, before.move_stack)

    def test_declined_evans_has_multiple_real_response_options_without_capture_claim(self):
        board = position(EVANS + " c5b6")
        rows = knowledge.proposals(board, "declined", limit=8)
        self.assertEqual({row["uci"] for row in rows}, {"a2a4", "b4b5", "c1b2"})
        self.assertTrue(all(row["stage"] == "declined" for row in rows))
        self.assertFalse(knowledge.context(board)["offer_captured"])

    def test_accurate_bishop_retreat_has_castling_and_center_continuations(self):
        board = position(EVANS + " c5b4 c2c3 b4a5")
        self.assertEqual({row["uci"] for row in knowledge.proposals(board, "defense", limit=8)}, {"e1g1", "d2d4"})

    def test_danish_continues_after_acceptance_and_stops_at_named_defense_leaf(self):
        accepted = position("e2e4 e7e5 d2d4 e5d4 c2c3 d4c3")
        self.assertIn("f1c4", {row["uci"] for row in knowledge.proposals(accepted, "danish")})
        accepted_again = position("e2e4 e7e5 d2d4 e5d4 c2c3 d4c3 f1c4 c3b2")
        self.assertIn("c1b2", {row["uci"] for row in knowledge.proposals(accepted_again, "danish")})
        defended = position("e2e4 e7e5 d2d4 e5d4 c2c3 d4c3 f1c4 c3b2 c1b2 d7d5")
        self.assertTrue(knowledge.context(defended)["offer_captured"])
        self.assertEqual(knowledge.proposals(defended, "danish", limit=8), [])

    def test_new_black_benko_continuation_has_explicit_inferred_anchor(self):
        board = position("d2d4 g8f6 c2c4 c7c5 d4d5 b7b5 c4b5")
        rows = knowledge.proposals(board, "benko")
        self.assertIn("a7a6", {row["uci"] for row in rows})
        context = knowledge.context(board)
        self.assertEqual(context["family_id"], "benko")
        self.assertEqual(context["owner"], "black")
        self.assertEqual(context["anchor_source"], "shortest_catalog_line")
        self.assertTrue(context["captured_anchor_piece"])
        self.assertIsNone(context["offer_captured"])

    def test_declined_morra_catalog_end_does_not_invent_a_followup(self):
        board = position("e2e4 c7c5 d2d4 c5d4 c2c3 g8f6")
        self.assertEqual(knowledge.context(board)["family_id"], "smith-morra")
        self.assertFalse(knowledge.context(board)["offer_captured"])
        self.assertEqual(knowledge.proposals(board, "ended"), [])

    def test_position_transposition_uses_legal_same_state_and_marks_its_source_path(self):
        first = position("d2d4 d7d5 c2c4 e7e6 b1c3 g8f6 c1g5 f8e7 e2e3 e8g8")
        second = position("d2d4 g8f6 c2c4 e7e6 b1c3 d7d5 c1g5 f8e7 e2e3 e8g8")
        self.assertEqual(knowledge.position_key(first), knowledge.position_key(second))
        self.assertNotEqual(first.move_stack, second.move_stack)
        rows = knowledge.proposals(first, "transpose", limit=8)
        transposed = next(row for row in rows if row["uci"] == "a1c1")
        self.assertTrue(transposed["transposition"])
        self.assertNotEqual(transposed["source_prefix_uci"], [move.uci() for move in first.move_stack])
        self.assertIn(chess.Move.from_uci(transposed["uci"]), first.legal_moves)

    def test_keys_keep_side_castling_and_legal_en_passant_but_ignore_counters(self):
        board = position("e2e4")
        no_ep = board.copy(stack=True)
        no_ep.ep_square = None
        self.assertEqual(knowledge.position_key(board), knowledge.position_key(no_ep))
        counters = board.copy(stack=True)
        counters.halfmove_clock, counters.fullmove_number = 8, 40
        self.assertEqual(knowledge.position_key(board), knowledge.position_key(counters))
        other = board.copy(stack=True)
        other.turn = not other.turn
        self.assertNotEqual(knowledge.position_key(board), knowledge.position_key(other))
        other = board.copy(stack=True)
        other.castling_rights = 0
        self.assertNotEqual(knowledge.position_key(board), knowledge.position_key(other))
        ep = chess.Board("4k3/8/8/3pP3/8/8/8/4K3 w - d6 0 1")
        other = ep.copy()
        other.ep_square = None
        self.assertNotEqual(knowledge.position_key(ep), knowledge.position_key(other))
        pinned = chess.Board("k3r3/8/8/3pP3/8/8/8/4K3 w - d6 0 1")
        other = pinned.copy()
        other.ep_square = None
        self.assertEqual(knowledge.position_key(pinned), knowledge.position_key(other))

    def test_full_history_is_required_and_unrecorded_edits_fail_closed(self):
        board = position(EVANS + " c5b4")
        self.assertTrue(knowledge.proposals(board, "good"))
        self.assertEqual(knowledge.proposals(chess.Board(board.fen()), "no-history"), [])
        board.castling_rights &= ~chess.BB_A1
        self.assertEqual(knowledge.proposals(board, "edited"), [])

    def test_repetition_checks_and_custom_roots_are_not_knowledge_turns(self):
        repeated = position("g1f3 g8f6 f3g1 f6g8")
        self.assertEqual(knowledge.proposals(repeated, "repeat"), [])
        self.assertFalse(knowledge.context(repeated)["active"])
        checked = position("e2e4 e7e5 d1h5 b8c6 f1c4 g8f6 h5f7")
        self.assertTrue(checked.is_check())
        self.assertEqual(knowledge.proposals(checked, "check"), [])
        self.assertEqual(knowledge.proposals(chess.Board("4k3/8/8/8/8/8/4P3/4K3 w - - 0 1"), "custom"), [])

    def test_withdrawing_offer_then_losing_another_piece_on_that_square_is_not_acceptance(self):
        board = position(EVANS + " c5b6 b4b5 g8f6 b1c3 b6c5 c3d5 d7d6 d5b4 c5b4")
        context = knowledge.context(board)
        self.assertEqual(context["family_id"], "evans")
        self.assertEqual(context["stage"], "offer_withdrawn")
        self.assertTrue(context["offer_withdrawn"])
        self.assertFalse(context["captured_anchor_piece"])
        self.assertFalse(context["offer_captured"])

    def test_undo_reconstructs_context_and_deleted_signature_does_not_survive(self):
        board = position(EVANS + " c5b4")
        self.assertTrue(knowledge.context(board)["offer_captured"])
        board.pop()
        board.pop()
        self.assertFalse(knowledge.context(board)["active"])

    def test_catalog_outer_declined_does_not_label_albin_countergambit_declined(self):
        rows = [row for row in knowledge.repertoire() if row["family_id"] == "albin"]
        self.assertTrue(rows)
        self.assertFalse(any(row["stage"] == "declined" for row in rows))
        board = position("d2d4 d7d5 c2c4 e7e5 d4e5")
        self.assertTrue(knowledge.context(board)["offer_captured"])

    def test_proposals_are_deterministic_deduplicated_and_do_not_mutate_catalog(self):
        board = position(EVANS + " c5b6")
        first = knowledge.proposals(board, "stable", limit=8)
        self.assertEqual(first, knowledge.proposals(board, "stable", limit=8))
        self.assertEqual(len(first), len({row["uci"] for row in first}))
        first[0]["source_prefix_uci"].append("invalid")
        self.assertNotEqual(first, knowledge.proposals(board, "stable", limit=8))
        rows = knowledge.repertoire()
        rows[0]["moves"].clear()
        self.assertTrue(knowledge.repertoire()[0]["moves"])
        for invalid in (0, 9, True, "4", None):
            self.assertEqual(knowledge.proposals(board, "invalid", limit=invalid), [])

    def test_corrupt_derived_catalog_returns_no_candidates_and_explicit_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)
            for name in ("manifest.json", "catalog.json"):
                (destination / name).write_bytes((knowledge.DIRECTORY / name).read_bytes())
            (destination / "catalog.json").write_bytes(b"{}\n")
            knowledge._load.cache_clear()
            try:
                with patch.object(knowledge, "DIRECTORY", destination):
                    self.assertFalse(knowledge.metadata()["available"])
                    self.assertEqual(knowledge.repertoire(), [])
                    self.assertEqual(knowledge.proposals(position(EVANS + " c5b4"), "corrupt"), [])
            finally:
                knowledge._load.cache_clear()

    def test_kings_and_nested_knights_gambit_provenance_does_not_depend_on_deduplication(self):
        board = position("e2e4 e7e5 f2f4 e5f4 g1f3 g7g5")
        decisions = [{"ply": 3, "line_id": "kings", "selected_uci": "f2f4", "candidate_uci": "f2f4"}]
        committed = knowledge.proposals(board, "audit", limit=8, decisions=decisions)
        ordinary = knowledge.proposals(board, "audit", limit=8)
        self.assertTrue(committed)
        self.assertEqual({row["uci"] for row in committed}, {row["uci"] for row in ordinary})
        self.assertNotEqual({row["line_id"] for row in committed}, {row["line_id"] for row in ordinary})
        self.assertTrue(all(knowledge.valid_proposal(board, row) for row in committed + ordinary))

    def test_edge_validation_rejects_mutated_provenance_stale_position_and_foreign_history(self):
        board = position(EVANS + " c5b6")
        row = knowledge.proposals(board, "edge")[0]
        self.assertTrue(knowledge.valid_proposal(board, row))
        changes = {"uci": "a2a3", "line_id": "invented", "name": "invented", "owner": "black",
                   "family_id": "kings", "family_name": "invented", "source_url": "https://invalid.example/",
                   "source_license": "unknown", "catalog_commit": "0" * 40, "catalog_sha256": "0" * 64,
                   "source_prefix_uci": [], "source_next_uci": "a2a3", "policy": "unknown",
                   "transposition": True, "stage": "accepted", "role": "signature",
                   "candidate_only": False, "loss_cap_cp": 9999}
        for field, value in changes.items():
            with self.subTest(field=field):
                self.assertFalse(knowledge.valid_proposal(board, {**copy.deepcopy(row), field: value}))
        self.assertFalse(knowledge.valid_proposal(chess.Board(board.fen()), row))
        advanced = board.copy(stack=True)
        advanced.push_uci(row["uci"])
        self.assertFalse(knowledge.valid_proposal(advanced, row))
        for value in (None, [], "row"):
            self.assertFalse(knowledge.valid_proposal(board, value))

    def test_valid_transposed_edge_and_key_rights_remain_bound_to_current_source(self):
        board = position("d2d4 d7d5 c2c4 e7e6 b1c3 g8f6 c1g5 f8e7 e2e3 e8g8")
        row = next(row for row in knowledge.proposals(board, "transpose", limit=8) if row["uci"] == "a1c1")
        self.assertTrue(row["transposition"])
        self.assertTrue(knowledge.valid_proposal(board, row))
        board.castling_rights = 0
        self.assertFalse(knowledge.valid_proposal(board, row))


if __name__ == "__main__":
    unittest.main()
