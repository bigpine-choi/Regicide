"""Accepted-sacrifice provenance and leakage boundaries for operations practice."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

import chess

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import build_gambit_operations_curriculum as curriculum
import gambit_knowledge


class CurriculumTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path = ROOT / "data/gambits/operations_curriculum.json"
        cls.data = json.loads(cls.path.read_text(encoding="utf-8"))
        cls.catalog = json.loads((ROOT / "data/gambits/catalog.json").read_text(encoding="utf-8"))
        cls.sources = {row["line_id"]: row for row in cls.catalog["lines"]}

    def test_schema_and_predeclared_family_splits_cover_both_colors(self):
        self.assertEqual(self.data["schema"], curriculum.SCHEMA)
        self.assertEqual(self.data["policy"], curriculum.POLICY)
        self.assertEqual(len(self.data["cases"]), 48)
        self.assertEqual(len(self.data["families"]), 12)
        self.assertEqual(self.data["counts"]["by_split"], {"train": 32, "validation": 8, "holdout": 8})
        self.assertEqual(self.data["counts"]["by_owner"], {"white": 28, "black": 20})
        self.assertEqual({row["family_id"] for row in self.data["cases"] if row["split"] == "holdout"},
                         {"benko", "from"})
        self.assertIn("previous separate", self.data["split_policy"]["previous_experiment_note"])

    def test_all_positions_replay_full_legal_history_and_have_positive_material_debt(self):
        for row in self.data["cases"]:
            with self.subTest(case=row["case_id"]):
                board = curriculum.replay(row["moves"])
                self.assertEqual(row["history_uci"], row["moves"])
                self.assertEqual(row["root_fen"], chess.STARTING_FEN)
                self.assertEqual(board.fen(en_passant="fen"), row["fen"])
                self.assertEqual(curriculum.position_key(board), row["position_key"])
                self.assertEqual(len(board.move_stack), row["root_ply"])
                self.assertEqual(board.turn, row["owner"] == "white")
                self.assertEqual(row["material_debt_cp"], -curriculum.balance(board, row["owner"]))
                self.assertGreater(row["material_debt_cp"], 0)
                self.assertFalse(board.is_check())
                self.assertFalse(board.is_game_over(claim_draw=True))
                self.assertFalse(board.is_repetition(2))
                self.assertGreater(board.legal_moves.count(), 1)

    def test_every_recorded_acceptance_captures_the_original_offered_piece(self):
        for row in self.data["cases"]:
            with self.subTest(case=row["case_id"]):
                signature, acceptance = curriculum.proof(row["moves"], row["owner"],
                    row["signature"]["ply"], row["acceptance"]["capture_ply"])
                self.assertEqual(signature, row["signature"])
                self.assertEqual(acceptance, row["acceptance"])
                self.assertTrue(acceptance["actual_capture"])
                self.assertEqual(signature["offered_piece"], acceptance["victim_piece"])
                self.assertEqual(signature["offered_square"], acceptance["victim_square"])
                self.assertGreater(acceptance["victim_value_cp"], 0)
                before = chess.Board(acceptance["before_fen"])
                move = chess.Move.from_uci(acceptance["capture_uci"])
                self.assertIn(move, before.legal_moves)
                self.assertTrue(before.is_capture(move))
                self.assertNotEqual(before.turn, row["owner"] == "white")

    def test_owner_mismatch_wrong_capture_or_withdrawn_offer_cannot_establish_acceptance(self):
        with self.assertRaises(ValueError):
            curriculum.proof("e2e4 e7e5 f2f4 e5f4".split(), "black", 3, 4)
        with self.assertRaises(ValueError):
            curriculum.proof("e2e4 e7e5 f2f4 g7g6".split(), "white", 3, 4)
        # The original f4 pawn moves to f5 before ...gxf5. That later capture
        # does not retroactively count as accepting the original square offer.
        withdrawn = "e2e4 e7e5 f2f4 g7g6 f4f5 g6f5".split()
        self.assertTrue(curriculum.replay(withdrawn).is_valid())
        with self.assertRaises(ValueError):
            curriculum.proof(withdrawn, "white", 3, 6)

    def test_sourced_and_authored_parts_are_separate_and_source_hashes_match(self):
        raw = (ROOT / "data/gambits/catalog.json").read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), self.data["source"]["catalog_sha256"])
        self.assertEqual(self.data["source"]["license"], "CC0-1.0")
        self.assertEqual(self.data["source"]["builder_sha256"],
                         hashlib.sha256(Path(curriculum.__file__).read_bytes()).hexdigest())
        for filename, expected in self.data["source"]["files"].items():
            self.assertEqual(hashlib.sha256((ROOT / "data/openings" / filename).read_bytes()).hexdigest(), expected)
        for row in self.data["cases"]:
            with self.subTest(case=row["case_id"]):
                source = self.sources[row["source"]["catalog_line_id"]]
                length = row["source"]["source_prefix_plies"]
                self.assertEqual(row["moves"][:length], source["moves"][:length])
                self.assertEqual(row["source"]["source_moves"], source["moves"])
                self.assertEqual(row["source"]["url"], source["source_url"])
                self.assertEqual(row["source"]["license"], "CC0-1.0")
                if row["variation"]["kind"] == "catalog_prefix":
                    self.assertEqual(row["moves"], source["moves"][:row["root_ply"]])
                else:
                    self.assertEqual(row["variation"]["kind"], "project_authored")
                    self.assertGreater(row["root_ply"], length)
                    self.assertTrue(row["variation"]["notes"])

    def test_vienna_acceptance_is_honestly_authored_after_a_sourced_signature(self):
        rows = [row for row in self.data["cases"] if row["family_id"] == "vienna"]
        self.assertEqual(len(rows), 4)
        for row in rows:
            self.assertEqual(row["variation"]["kind"], "project_authored")
            self.assertEqual(row["source"]["source_prefix_plies"], 5)
            self.assertEqual(row["moves"][:6], "e2e4 e7e5 b1c3 g8f6 f2f4 e5f4".split())
            self.assertEqual(row["split"], "validation")

    def test_sacrifice_facts_are_separate_from_family_confirmation_and_net_debt(self):
        for row in self.data["cases"]:
            length = max(row["acceptance"]["capture_ply"], row["family_confirmation_ply"])
            self.assertEqual(row["recognition_prefix_uci"], row["moves"][:length])
            self.assertLessEqual(length, row["root_ply"])
            self.assertEqual(row["family_id"], self.data["families"][row["family_id"]]["family_id"])
            if row["family_id"] == "scotch":
                self.assertEqual(row["acceptance"]["capture_ply"], 6)
                self.assertEqual(row["family_confirmation_ply"], 7)
                self.assertEqual(row["recognition_prefix_uci"][-1], "f1c4")
        # Stafford's first recapture exercise owes more than the final pawn;
        # gross offered-knight value is not reported as final sacrifice cost.
        stafford = [row for row in self.data["cases"] if row["family_id"] == "stafford"]
        self.assertTrue(any(row["material_debt_cp"] == 400 for row in stafford))
        self.assertTrue(any(row["material_debt_cp"] == 100 for row in stafford))

    def test_whole_family_and_all_legal_immediate_children_do_not_cross_splits(self):
        groups = {name: set() for name in ("train", "validation", "holdout")}
        root_keys = set()
        for row in self.data["cases"]:
            self.assertEqual(self.data["families"][row["family_id"]]["split"], row["split"])
            board = curriculum.replay(row["moves"])
            self.assertNotIn(row["position_key"], root_keys)
            root_keys.add(row["position_key"])
            groups[row["split"]].add(curriculum.position_key(board))
            for move in list(board.legal_moves):
                board.push(move)
                groups[row["split"]].add(curriculum.position_key(board))
                board.pop()
        for first, second in (("train", "validation"), ("train", "holdout"), ("validation", "holdout")):
            self.assertFalse(groups[first] & groups[second])

    def test_off_book_labels_follow_runtime_context_not_only_a_raw_source_edge(self):
        for row in self.data["cases"]:
            with self.subTest(case=row["case_id"]):
                board = curriculum.replay(row["moves"])
                actual = bool(gambit_knowledge.proposals(board, "operations-curriculum", limit=8))
                self.assertEqual(row["has_catalog_continuation"], actual)
                self.assertEqual(row["book_status"], "known_continuation" if actual else "catalog_leaf_or_authored_off_book")
        self.assertEqual(self.data["counts"]["off_book_roots"], 32)
        # A global source edge is not necessarily available on this actual
        # family's history; keep that distinction explicit.
        self.assertTrue(any(row["source_graph_edge_exists"] and not row["has_catalog_continuation"]
                            for row in self.data["cases"]))

    def test_every_family_includes_post_book_practice_and_goals_are_topics_not_answers(self):
        for family in self.data["families"]:
            rows = [row for row in self.data["cases"] if row["family_id"] == family]
            self.assertEqual(len(rows), 4)
            self.assertTrue(any(not row["has_catalog_continuation"] for row in rows), family)
        topics = {topic for row in self.data["cases"] for topic in row["goal_tags"]}
        self.assertEqual(topics, {"development", "attack_maintenance", "material_recovery", "defensive_transition"})
        self.assertFalse(self.data["claims"]["best_defense_verified"])
        self.assertFalse(self.data["claims"]["goal_tags_are_correct_move_labels"])
        for row in self.data["cases"]:
            self.assertIn("not prescribed correct moves", row["goal_status"])
            self.assertIn("not claimed best defense", row["variation"]["response_status"])

    def test_digest_and_rebuild_bytes_are_exactly_reproducible(self):
        unsigned = copy.deepcopy(self.data)
        actual = unsigned.pop("curriculum_sha256")
        self.assertEqual(curriculum.digest(unsigned), actual)
        with tempfile.TemporaryDirectory(prefix="regicide-operations-curriculum-") as folder:
            target = Path(folder) / "curriculum.json"
            rebuilt = curriculum.build(target)
            self.assertEqual(rebuilt, self.data)
            self.assertEqual(target.read_bytes(), self.path.read_bytes())


if __name__ == "__main__":
    unittest.main()
