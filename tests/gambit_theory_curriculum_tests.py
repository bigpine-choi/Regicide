"""Reproducible provenance, legal replay and split boundaries for theory roots."""
import copy
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

import chess

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import gambit_knowledge
import gambit_theory_curriculum as curriculum


def replay(moves):
    board = chess.Board()
    for uci in moves:
        move = chess.Move.from_uci(uci)
        if move not in board.legal_moves:
            raise AssertionError("Illegal case history: " + uci)
        board.push(move)
    return board


class TheoryCurriculumTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Real pinned-source regeneration is performed once. Most boundary
        # tests reuse those verified bytes rather than depend on runtime data.
        cls.catalog, cls.manifest = curriculum._load_catalog()

    def build(self, **kwargs):
        with patch.object(curriculum, "_load_catalog", return_value=(self.catalog, self.manifest)):
            return curriculum.build_curriculum(**kwargs)

    def test_real_four_family_roots_and_family_split(self):
        result = self.build()
        self.assertEqual(result["schema"], curriculum.SCHEMA)
        self.assertEqual(set(case["family_id"] for case in result["cases"]),
                         {"evans", "smith-morra", "benko", "albin"})
        for family, config in curriculum.FAMILIES.items():
            cases = [case for case in result["cases"] if case["family_id"] == family]
            self.assertGreater(len(cases), 0)
            self.assertLessEqual(len(cases), 8)
            self.assertEqual({case["split"] for case in cases}, {config["split"]})
            self.assertIn("offer", {case["stage"] for case in cases})
            self.assertIn("response", {case["stage"] for case in cases})
            self.assertIn("defense_theory", {case["training_role"] for case in cases})
        self.assertEqual(result["source"]["commit"], curriculum.PINNED_COMMIT)
        self.assertEqual(result["source"]["license"], "CC0-1.0")
        self.assertTrue(result["candidate_only"])
        self.assertFalse(result["learned_model"])

    def test_deterministic_bytes_and_seed_sampling(self):
        first = self.build(seed=42)
        second = self.build(seed=42)
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
        changed = self.build(seed=43)
        self.assertNotEqual(first["curriculum_id"], changed["curriculum_id"])
        # The seed should affect actual selected later roots, not merely an ID.
        self.assertNotEqual({case["case_id"] for case in first["cases"]},
                            {case["case_id"] for case in changed["cases"]})

    def test_standard_replay_legal_candidates_and_provenance(self):
        result = self.build()
        source_rows = {row["line_id"]: row for row in self.catalog["lines"]}
        for case in result["cases"]:
            board = replay(case["history_uci"])
            self.assertEqual(board.root().fen(), chess.STARTING_FEN)
            self.assertEqual(board.fen(en_passant="legal"), case["fen"])
            self.assertEqual(" ".join(board.fen(en_passant="legal").split()[:4]), case["position_key"])
            self.assertEqual(case["turn"], "white" if board.turn else "black")
            self.assertEqual(case["training_role"], "gambit_theory" if case["turn"] == case["owner"]
                             else "defense_theory")
            self.assertEqual(len(case["book_candidates"]), len(set(case["book_candidates"])))
            self.assertGreater(len(case["source_lines"]), 0)
            self.assertEqual(case["source_urls"], sorted({row["source_url"] for row in case["source_lines"]}))
            for reference in case["source_lines"]:
                original = source_rows[reference["line_id"]]
                self.assertEqual(reference["name"], original["name"])
                self.assertEqual(reference["source_url"], original["source_url"])
                self.assertEqual(reference["license"], "CC0-1.0")
            for uci in case["book_candidates"]:
                move = chess.Move.from_uci(uci)
                self.assertIn(move, board.legal_moves)
                supporting = []
                for reference in case["source_lines"]:
                    row = source_rows[reference["line_id"]]
                    supporting.extend(row["moves"][index] for index, key in enumerate(row["position_keys"][:-1])
                                      if key == case["position_key"])
                self.assertIn(uci, supporting)
                board.push(move)
                self.assertEqual(gambit_knowledge.position_key(board), case["candidate_position_keys"][uci])
                board.pop()

    def test_offer_and_response_are_structural_not_acceptance_labels(self):
        result = self.build()
        for family, config in curriculum.FAMILIES.items():
            cases = [case for case in result["cases"] if case["family_id"] == family]
            offer = next(case for case in cases if case["stage"] == "offer")
            response = next(case for case in cases if case["stage"] == "response")
            anchor = config["anchor"].split()
            self.assertEqual(offer["history_uci"], anchor[:-1])
            self.assertEqual(response["position_key"], gambit_knowledge.position_key(replay(anchor)))
            self.assertIn(anchor[-1], offer["book_candidates"])
            self.assertEqual(offer["turn"], config["owner"])
            self.assertNotEqual(response["turn"], config["owner"])
            self.assertNotIn("accepted", response)
            self.assertNotIn("offer_captured", response)
        self.assertIn("no capture or acceptance", result["stage_semantics"]["response"])

    def test_root_and_all_legal_successors_are_split_disjoint(self):
        result = self.build(roots_per_family=20)
        keys_by_split = {}
        for case in result["cases"]:
            board = replay(case["history_uci"])
            keys = {gambit_knowledge.position_key(board)}
            for move in list(board.legal_moves):
                board.push(move)
                keys.add(gambit_knowledge.position_key(board))
                board.pop()
            self.assertEqual(keys, set(case["split_guard_keys"]))
            keys_by_split.setdefault(case["split"], set()).update(keys)
        splits = sorted(keys_by_split)
        for index, split in enumerate(splits):
            for other in splits[index + 1:]:
                self.assertFalse(keys_by_split[split] & keys_by_split[other])

    def test_cross_split_overlap_is_excluded_and_explained(self):
        source = self.catalog["source"]
        configs = copy.deepcopy(curriculum.FAMILIES)
        for family in ("evans", "albin"):
            configs[family]["anchor"] = "e2e4 e7e5"
        moves = "e2e4 e7e5 g1f3 b8c6".split()
        boards = [replay(moves[:offset]) for offset in range(len(moves) + 1)]
        rows = []
        for family, name in (("evans", "Evans Gambit"), ("albin", "Albin Countergambit")):
            rows.append({"line_id": "synthetic-" + family, "name": name, "moves": moves,
                         "position_keys": [gambit_knowledge.position_key(board) for board in boards],
                         "source_license": source["license"], "catalog_commit": source["commit"],
                         "catalog_file": "c.tsv", "source_url":
                         "https://raw.githubusercontent.com/lichess-org/chess-openings/" + source["commit"] + "/c.tsv"})
        fake = {**self.catalog, "lines": rows}
        with patch.object(curriculum, "FAMILIES", configs), patch.object(
                curriculum, "_load_catalog", return_value=(fake, self.manifest)):
            result = curriculum.build_curriculum(families=["evans", "albin"])
        self.assertTrue(result["cases"])
        self.assertEqual({case["split"] for case in result["cases"]}, {"train"})
        dropped = [row for row in result["excluded"] if row["reason"] == "cross_split_position_overlap"]
        self.assertTrue(dropped)
        self.assertTrue(all(row["overlap_keys_by_split"]["train"] for row in dropped))

    def test_family_subset_and_root_cap(self):
        result = self.build(families=["benko", "evans"], roots_per_family=2)
        self.assertEqual(result["families"], ["evans", "benko"])
        self.assertEqual(result["counts"]["cases_by_family"], {"evans": 2, "benko": 2})
        self.assertTrue(any(row["reason"] == "family_root_limit" for row in result["excluded"]))

    def test_invalid_options_fail_before_loading_source(self):
        with patch.object(curriculum, "_load_catalog", side_effect=AssertionError("must not load")):
            for options in ({"seed": True}, {"seed": "42"}, {"roots_per_family": 0},
                            {"roots_per_family": 129}, {"families": "evans"}, {"families": []},
                            {"families": ["evans", "evans"]}, {"families": ["unknown"]}):
                with self.subTest(options=options), self.assertRaises(ValueError):
                    curriculum.build_curriculum(**options)

    def test_malformed_relevant_source_line_fails_closed(self):
        for mutation in ("illegal_move", "wrong_key", "wrong_provenance"):
            catalog = copy.deepcopy(self.catalog)
            row = next(row for row in catalog["lines"] if "Evans Gambit" in row["name"])
            if mutation == "illegal_move":
                row["moves"][0] = "e2e5"
            elif mutation == "wrong_key":
                row["position_keys"][0] = "wrong"
            else:
                row["source_url"] = "https://example.invalid/unpinned.tsv"
            with self.subTest(mutation=mutation), patch.object(
                    curriculum, "_load_catalog", return_value=(catalog, self.manifest)), self.assertRaises(ValueError):
                curriculum.build_curriculum()

    def test_missing_selected_family_fails_closed(self):
        catalog = {**self.catalog, "lines": [row for row in self.catalog["lines"]
                                            if "Evans Gambit" not in row["name"]]}
        with patch.object(curriculum, "_load_catalog", return_value=(catalog, self.manifest)), self.assertRaises(ValueError):
            curriculum.build_curriculum()

    def test_missing_or_tampered_pinned_source_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            empty = Path(directory) / "missing"
            with patch.object(curriculum, "SOURCE_DIRECTORY", empty), self.assertRaises(ValueError):
                curriculum.build_curriculum()
            copied = Path(directory) / "openings"
            shutil.copytree(ROOT / "data/openings", copied)
            (copied / "a.tsv").write_bytes((copied / "a.tsv").read_bytes() + b"\n")
            with patch.object(curriculum, "SOURCE_DIRECTORY", copied), self.assertRaises(ValueError):
                curriculum.build_curriculum()


if __name__ == "__main__":
    unittest.main()
