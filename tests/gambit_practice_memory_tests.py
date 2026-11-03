"""Exact-state native practice memory boundaries, without external engines."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

import chess

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import gambit_practice_memory as memory
import train_gambit_operations as training


IDENTITY = {"binary_sha256": "a" * 64, "evaluation_id": "classical_activity_v1"}


def board():
    return memory._board("e2e4 e7e5 g1f3 b8c6 f1c4 f8c5 b2b4 c5b4".split())


def artifact():
    position = board()
    row = {"record_id": "evans:1", "case_id": "evans", "family_id": "evans", "split": "train",
        "owner": "white", "goal": "development", "fen": position.fen(en_passant="fen"),
        "history_uci": [move.uci() for move in position.move_stack], "position_key": memory.position_key(position),
        "candidates": [{"uci": "c2c3", "offline_owner_cp": 20, "native_depth": 6, "requested_nodes": 100000,
                        "query_id": "evans:1:c2c3", "goal": "attack_maintenance",
                        "pv_uci": ["c2c3", "b4a5", "d2d4", "e5d4"]},
                       {"uci": "e1g1", "offline_owner_cp": 0, "native_depth": 6, "requested_nodes": 100000,
                        "query_id": "evans:1:e1g1", "goal": "defensive_transition",
                        "pv_uci": ["e1g1", "g8f6"]}]}
    value = {"schema": memory.SCHEMA, "policy": memory.POLICY, "status": "approved",
             "engine_identity": IDENTITY, "heldout_family_ids": ["benko", "from"],
             "heldout_position_keys": [], "records": [row], "counts": {"memory_records": 1},
             "acceptance": {"passed": True}}
    value["memory_id"] = memory.memory_digest(value)
    return value


class MemoryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="regicide-operations-memory-")
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "memory.json"
        self.value = artifact()
        self.save()

    def save(self):
        self.value["memory_id"] = memory.memory_digest(self.value)
        self.path.write_text(json.dumps(self.value), encoding="utf-8")

    def proposals(self, position=None, **kwargs):
        return memory.proposals(position or board(), "seed", engine_identity=IDENTITY,
                                memory_path=self.path, **kwargs)

    def test_known_state_returns_native_ordered_proposals_and_provenance(self):
        before = board()
        rows = self.proposals(before)
        self.assertEqual([row["uci"] for row in rows], ["c2c3", "e1g1"])
        self.assertEqual(rows[0]["source"], "native_practice_memory")
        self.assertEqual(rows[0]["loss_cap_cp"], 35)
        self.assertEqual(rows[0]["offline_evidence"]["native_depth"], 6)
        self.assertTrue(memory.valid_proposal(before, rows[0], engine_identity=IDENTITY, memory_path=self.path))
        rows[0]["offline_evidence"]["offline_owner_cp"] = 9999
        self.assertFalse(memory.valid_proposal(before, rows[0], engine_identity=IDENTITY, memory_path=self.path))
        self.assertEqual(self.proposals()[0]["offline_evidence"]["offline_owner_cp"], 20)

    def test_changed_state_and_fen_only_position_cannot_reuse_memory(self):
        position = board()
        position.push_uci("c2c3")
        self.assertFalse(self.proposals(position))
        self.assertFalse(self.proposals(chess.Board(board().fen())))
        position = board()
        position.halfmove_clock += 1
        self.assertFalse(self.proposals(position))

    def test_other_binary_evaluator_and_experimental_artifact_are_inactive(self):
        for identity in [{**IDENTITY, "binary_sha256": "b" * 64},
                         {**IDENTITY, "evaluation_id": "other"}, None]:
            self.assertFalse(memory.proposals(board(), "seed", engine_identity=identity, memory_path=self.path))
        self.value["status"] = "experimental"
        self.save()
        self.assertFalse(self.proposals())
        self.value["status"] = "approved"
        self.value["acceptance"]["passed"] = False
        self.save()
        self.assertFalse(self.proposals())

    def test_heldout_family_and_transposed_child_keys_are_rejected(self):
        self.value["records"][0]["family_id"] = "benko"
        self.save()
        self.assertFalse(self.proposals())
        self.value = artifact()
        child = board()
        child.push_uci("c2c3")
        self.value["heldout_position_keys"] = [memory.position_key(child)]
        self.save()
        self.assertFalse(self.proposals())

    def test_corrupt_checksum_illegal_pv_and_shallow_labels_are_rejected(self):
        for change in [lambda value: value["records"][0]["candidates"][0].update(native_depth=3),
                       lambda value: value["records"][0]["candidates"][0].update(pv_uci=["c2c3"]),
                       lambda value: value["records"][0]["candidates"][0].update(pv_uci=["c2c3", "e8e1"]),
                       lambda value: value["records"][0].update(owner="black")]:
            self.value = artifact()
            change(self.value)
            self.save()
            self.assertFalse(self.proposals())
        self.value = artifact()
        self.value["memory_id"] = "incorrect"
        self.path.write_text(json.dumps(self.value), encoding="utf-8")
        self.assertFalse(self.proposals())

    def test_metadata_and_missing_memory_are_explicit(self):
        self.assertEqual(memory.metadata(self.path)["records"], 1)
        self.assertEqual(memory.memory_identity(self.path)["status"], "approved")
        absent = self.path.with_name("absent")
        self.assertFalse(memory.metadata(absent)["available"])
        self.assertEqual(memory.memory_identity(absent)["status"], "unavailable")

    def test_record_representative_is_not_confused_by_context_deduplication(self):
        second = copy.deepcopy(self.value["records"][0])
        second.update(record_id="other:1", case_id="other", family_id="other")
        self.value["records"].append(second)
        self.save()
        rows = self.proposals(context={"family_id": "other"})
        self.assertEqual(rows[0]["record_id"], "other:1")
        self.assertTrue(memory.valid_proposal(board(), rows[0], engine_identity=IDENTITY, memory_path=self.path))

    def test_returned_source_and_offline_scores_cannot_be_forged(self):
        rows = self.proposals()
        for key, value in [("uci", "h2h3"), ("memory_id", "old"), ("source", "other"),
                           ("goal", "material_recovery"), ("owner", "black")]:
            forged = copy.deepcopy(rows[0])
            forged[key] = value
            self.assertFalse(memory.valid_proposal(board(), forged, engine_identity=IDENTITY, memory_path=self.path))

    def test_goals_are_observed_state_changes_not_named_opening_labels(self):
        before = {"material_debt_cp": 200, "owner_in_check": False, "king_danger": 4,
                  "developed_minors": 1, "coordinated_pressure": 0, "rook_files": 0}
        variants = [(dict(before, material_debt_cp=100), "material_recovery"),
                    (dict(before, king_danger=2), "defensive_transition"),
                    (dict(before, developed_minors=2), "development"),
                    (dict(before, coordinated_pressure=3), "attack_maintenance"),
                    (dict(before), "consolidation")]
        for after, expected in variants:
            self.assertEqual(training.classify(before, after), expected)

    def test_candidate_pv_replay_and_actual_facts_keep_boards_unchanged(self):
        position = board()
        original = position.fen(), list(position.move_stack)
        line, after = training.legal_line(position, ["c2c3", "b4a5", "d2d4", "e5d4"])
        self.assertEqual(len(line), 4)
        self.assertEqual(training.facts(position, "white")["material_debt_cp"], 100)
        self.assertGreaterEqual(training.facts(after, "white")["material_debt_cp"], 0)
        self.assertEqual((position.fen(), position.move_stack), original)


if __name__ == "__main__":
    unittest.main()
