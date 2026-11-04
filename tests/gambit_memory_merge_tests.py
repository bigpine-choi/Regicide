"""Conservative exact-state memory merge and immutable-input contracts."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import chess

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import gambit_practice_memory as memory
import merge_gambit_memory as merge_tool

IDENTITY = {"binary_sha256": "a" * 64, "evaluation_id": "classical_activity_v1"}
EVANS = "e2e4 e7e5 g1f3 b8c6 f1c4 f8c5 b2b4 c5b4".split()
KINGS = "e2e4 e7e5 f2f4 e5f4".split()


def candidate(board, uci, query_id="original-query", cp=20, depth=6):
    child = board.copy(stack=True)
    child.push_uci(uci)
    reply = min(child.legal_moves, key=lambda move: move.uci()).uci()
    return {"uci": uci, "offline_owner_cp": cp, "native_depth": depth,
            "requested_nodes": 100000, "query_id": query_id,
            "goal": "development", "pv_uci": [uci, reply]}


def artifact(history=EVANS, family="evans", record_id="source:1"):
    board = memory._board(list(history))
    uci = "c2c3" if family == "evans" and "c2c3" in [move.uci() for move in board.legal_moves] else min(board.legal_moves, key=lambda move: move.uci()).uci()
    value = {"schema": memory.SCHEMA, "policy": memory.POLICY, "status": "approved",
        "engine_identity": copy.deepcopy(IDENTITY), "heldout_family_ids": ["from", "benko"],
        "heldout_position_keys": [], "source_sha256": {"original-trainer.py": "c" * 64},
        "curriculum_sha256": "d" * 64,
        "records": [{"record_id": record_id, "case_id": "case-" + record_id, "family_id": family,
            "split": "train", "owner": "white" if board.turn else "black", "goal": "development",
            "fen": board.fen(en_passant="fen"), "history_uci": list(history),
            "position_key": memory.position_key(board), "candidates": [candidate(board, uci)]}],
        "acceptance": {"passed": True}}
    value["memory_id"] = memory.memory_digest(value)
    memory._validate(value)
    return value


class MergeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="regicide-memory-merge-")
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.base_value = artifact()
        self.base = self.save("base.json", self.base_value)
        self.base_bytes = self.base.read_bytes()

    def save(self, name, value):
        value["memory_id"] = memory.memory_digest(value)
        path = self.folder / name
        path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        return path

    def run_merge(self, additions=(), name="merged"):
        output = self.folder / name
        result = merge_tool.merge(self.base, additions, output)
        report = json.loads((output / "merge-report.json").read_text(encoding="utf-8"))
        return output, result, report

    def test_immutable_base_and_input_snapshots_remain_valid_and_recoverable(self):
        before = memory.proposals(memory._board(EVANS), "seed", engine_identity=IDENTITY, memory_path=self.base)
        extra_value = artifact(KINGS, "kings")
        extra = self.save("extra.json", extra_value)
        extra_bytes = extra.read_bytes()
        output, merged, report = self.run_merge([extra])
        self.assertEqual(self.base.read_bytes(), self.base_bytes)
        self.assertEqual(extra.read_bytes(), extra_bytes)
        self.assertEqual((output / "inputs/000-base.json").read_bytes(), self.base_bytes)
        self.assertEqual((output / "inputs/001-addition.json").read_bytes(), extra_bytes)
        self.assertEqual(memory.proposals(memory._board(EVANS), "seed", engine_identity=IDENTITY, memory_path=self.base), before)
        self.assertEqual(memory.memory_identity(output / "inputs/000-base.json")["memory_id"], self.base_value["memory_id"])
        self.assertFalse(report["activated"])
        self.assertTrue(report["input_bytes_unchanged"])
        self.assertEqual(len(merged["records"]), 2)

    def test_exact_duplicate_is_collapsed_without_losing_query_provenance(self):
        duplicate = self.save("duplicate.json", copy.deepcopy(self.base_value))
        _, result, report = self.run_merge([duplicate])
        self.assertEqual((len(result["records"]), len(result["records"][0]["candidates"])), (1, 1))
        self.assertEqual(result["records"][0]["candidates"][0], self.base_value["records"][0]["candidates"][0])
        self.assertEqual(len(report["duplicate_candidates"]), 1)
        origins = result["records"][0]["merge_provenance"]["candidate_origins"]["c2c3"]
        self.assertEqual({origin["input_id"] for origin in origins}, {"input-000", "input-001"})

    def test_higher_score_and_depth_cannot_replace_conflicting_base_evidence(self):
        changed = copy.deepcopy(self.base_value)
        changed["records"][0]["candidates"][0].update(offline_owner_cp=900, native_depth=20, query_id="new-deeper-query")
        _, result, report = self.run_merge([self.save("conflict.json", changed)])
        self.assertEqual(result["records"][0]["candidates"][0], self.base_value["records"][0]["candidates"][0])
        self.assertEqual(report["status"], "complete_with_held_conflicts")
        self.assertEqual(report["conflicts"][0]["kind"], "candidate_evidence_conflict")
        self.assertEqual(report["conflicts"][0]["held"]["query_id"], "new-deeper-query")

    def test_new_candidates_keep_evidence_and_runtime_verification_contract(self):
        extra = copy.deepcopy(self.base_value)
        extra["records"][0]["candidates"] = [candidate(memory._board(EVANS), "e1g1", query_id="new-castle")]
        output, result, _ = self.run_merge([self.save("new-candidate.json", extra)])
        proposals = memory.proposals(memory._board(EVANS), "seed", engine_identity=IDENTITY,
                                     memory_path=output / "operations_memory.json", limit=8)
        self.assertEqual({proposal["uci"] for proposal in proposals}, {"c2c3", "e1g1"})
        for proposal in proposals:
            self.assertTrue(proposal["candidate_only"])
            self.assertEqual(proposal["loss_cap_cp"], 35)
            self.assertTrue(memory.valid_proposal(memory._board(EVANS), proposal, engine_identity=IDENTITY,
                                                  memory_path=output / "operations_memory.json"))
        self.assertIn("new-castle", [item["query_id"] for item in result["records"][0]["candidates"]])

    def test_candidate_capacity_preserves_base_and_input_order_instead_of_score(self):
        board = memory._board(EVANS)
        moves = [move.uci() for move in board.legal_moves if move.uci() != "c2c3"][:8]
        self.assertEqual(len(moves), 8)
        extra = copy.deepcopy(self.base_value)
        extra["records"][0]["candidates"] = [candidate(board, uci, query_id="extra-" + uci, cp=999) for uci in moves]
        _, result, report = self.run_merge([self.save("capacity.json", extra)])
        saved = result["records"][0]["candidates"]
        self.assertEqual([item["uci"] for item in saved], ["c2c3", *moves[:7]])
        self.assertEqual(report["capacity_held_candidates"][0]["uci"], moves[-1])

    def test_colliding_record_ids_across_states_are_uniquely_namespaced(self):
        extra = self.save("other-state.json", artifact(KINGS, "kings", record_id="source:1"))
        _, result, report = self.run_merge([extra])
        identifiers = [row["record_id"] for row in result["records"]]
        self.assertEqual(len(set(identifiers)), 2)
        self.assertTrue(all(identifier.startswith("merged-") for identifier in identifiers))
        self.assertEqual({row["record_id"] for row in report["record_mapping"]}, {"source:1"})

    def test_same_fen_with_different_full_history_is_not_deduplicated(self):
        left = "g1f3 g8f6 b1c3 b8c6".split()
        right = "b1c3 b8c6 g1f3 g8f6".split()
        a, b = artifact(left, "other"), artifact(right, "other")
        self.assertEqual(a["records"][0]["fen"], b["records"][0]["fen"])
        self.base = self.save("history-a.json", a)
        _, result, _ = self.run_merge([self.save("history-b.json", b)])
        self.assertEqual(len(result["records"]), 2)

    def test_family_inconsistency_is_held_without_relabeling_the_base(self):
        changed = copy.deepcopy(self.base_value)
        changed["records"][0]["family_id"] = "different-family"
        _, result, report = self.run_merge([self.save("family.json", changed)])
        self.assertEqual(result["records"][0]["family_id"], "evans")
        self.assertEqual(report["conflicts"][0]["kind"], "family_inconsistency")

    def test_engine_or_evaluator_mismatch_is_rejected_before_output_creation(self):
        for key, value in (("binary_sha256", "b" * 64), ("evaluation_id", "other")):
            with self.subTest(key=key):
                extra = artifact(KINGS, "kings")
                extra["engine_identity"][key] = value
                with self.assertRaises(ValueError):
                    self.run_merge([self.save(key + ".json", extra)], name=key)
                self.assertFalse((self.folder / key).exists())

    def test_holdout_union_rechecks_and_excludes_base_families_roots_and_children(self):
        for kind in ("family", "root", "child"):
            with self.subTest(kind=kind):
                extra = artifact(KINGS, "kings")
                if kind == "family":
                    extra["heldout_family_ids"].append("evans")
                else:
                    board = memory._board(EVANS)
                    if kind == "child":
                        board.push_uci("c2c3")
                    extra["heldout_position_keys"].append(memory.position_key(board))
                _, result, report = self.run_merge([self.save(kind + ".json", extra)], name=kind)
                self.assertEqual({row["family_id"] for row in result["records"]}, {"kings"})
                self.assertEqual(len(report["excluded_rows"]), 1)
                self.assertEqual(report["excluded_rows"][0]["input_id"], "input-000")

    def test_union_that_excludes_every_row_is_rejected_without_output(self):
        base = copy.deepcopy(self.base_value)
        base["heldout_family_ids"].append("kings")
        self.base = self.save("mutual-base.json", base)
        extra = artifact(KINGS, "kings")
        extra["heldout_family_ids"].append("evans")
        with self.assertRaises(ValueError):
            self.run_merge([self.save("mutual-other.json", extra)])
        self.assertFalse((self.folder / "merged").exists())

    def test_source_and_curriculum_heterogeneity_is_explicitly_derived(self):
        extra = artifact(KINGS, "kings")
        extra["source_sha256"] = {"new-trainer.py": "e" * 64}
        extra["curriculum_sha256"] = "f" * 64
        _, result, _ = self.run_merge([self.save("different-source.json", extra)])
        self.assertNotIn("curriculum_sha256", result)
        self.assertNotIn("source_sha256", result)
        self.assertEqual(len(result["source_inputs"]), 2)
        self.assertEqual(result["source_inputs"][1]["curriculum_sha256"], "f" * 64)
        self.assertEqual(len(result["derived_source_inputs_sha256"]), 64)
        self.assertEqual(len(result["derived_curriculum_inputs_sha256"]), 64)

    def test_unapproved_or_corrupt_identity_inputs_are_rejected(self):
        for change in (lambda value: value.update(status="experimental"),
                       lambda value: value["acceptance"].update(passed=False),
                       lambda value: value.update(acceptance=True)):
            extra = artifact(KINGS, "kings")
            change(extra)
            with self.assertRaises(ValueError):
                self.run_merge([self.save("bad.json", extra)])
        bad = self.save("bad-digest.json", artifact(KINGS, "kings"))
        value = json.loads(bad.read_text(encoding="utf-8"))
        value["memory_id"] = "wrong"
        bad.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.run_merge([bad])

    def test_duplicate_json_keys_are_not_accepted_as_valid_input(self):
        path = self.folder / "duplicate-keys.json"
        encoded = json.dumps(self.base_value)
        path.write_text('{"status":"experimental",' + encoded[1:], encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Duplicate JSON key"):
            self.run_merge([path])
        self.assertFalse((self.folder / "merged").exists())

    def test_existing_active_and_sources_outputs_are_refused(self):
        existing = self.folder / "existing"
        existing.mkdir()
        marker = existing / "keep.txt"
        marker.write_bytes(b"unchanged")
        for target in (existing, memory.DEFAULT_MEMORY, ROOT.parent / "sources/merge-must-not-create"):
            with self.subTest(target=target), self.assertRaises((ValueError, FileExistsError)):
                merge_tool.merge(self.base, [], target)
        self.assertEqual(marker.read_bytes(), b"unchanged")

    def test_atomic_file_publication_preserves_a_competing_file(self):
        target = self.folder / "publication.json"
        competing = b"another writer's bytes"
        def race(source, destination):
            Path(destination).write_bytes(competing)
            raise FileExistsError("Another writer published first")
        with patch.object(merge_tool.os, "link", side_effect=race), self.assertRaises(FileExistsError):
            merge_tool._write_new(target, b"our bytes")
        self.assertEqual(target.read_bytes(), competing)
        self.assertFalse(list(self.folder.glob(".merge-*.tmp")))


if __name__ == "__main__":
    unittest.main()
