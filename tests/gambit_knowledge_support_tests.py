"""Own-search extension authority, actual runtime reach and stale-state guards."""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch

import chess

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import gambit_knowledge as knowledge
import gambit_knowledge_support as support
import boss_play


class NativeSupportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data, cls.index, cls.manifest, error = support.load()
        if error:
            raise AssertionError(error)

    def test_declared_native_only_study_has_both_defenses_for_all_five_families(self):
        self.assertEqual(self.data["counts"]["seeds"], 10)
        self.assertEqual(self.data["counts"]["branches"], 20)
        self.assertEqual(set(self.data["counts"]["families"]),
                         {"blackmar-diemer", "smith-morra", "evans", "kings", "budapest"})
        self.assertEqual(self.data["origin"], "Regicide own search only")
        self.assertEqual(self.manifest["engine_returncode"], 0)
        self.assertEqual(self.data["engine_identity"]["binary_sha256"],
                         "5011da4c5e6ef01c452bd7125e4e22b372d2c9cfb858ed8759057325acb0e6ba")
        self.assertEqual(json.loads((support.DIRECTORY / "policy.json").read_text())["status"], "experimental")

    def test_all_native_labels_match_the_preserved_original_uci_queries(self):
        with zipfile.ZipFile(support.DIRECTORY / "native_continuations_evidence.zip") as archive:
            raw = archive.read("native-uci.jsonl")
            original = json.loads(archive.read("research-report-original.json"))
        self.assertEqual(hashlib.sha256(raw).hexdigest(), self.data["native_uci_sha256"])
        self.assertEqual(original["queries"], self.data["queries"])
        self.assertEqual(original["entries"], self.data["entries"])
        lines = [json.loads(line) for line in raw.splitlines()]
        for query in self.data["queries"].values():
            segment = lines[query["uci_begin_seq"] - 1:query["uci_end_seq"]]
            sent = [line["line"] for line in segment if line["direction"] == "send"]
            received = [line["line"] for line in segment if line["direction"] == "recv"]
            self.assertIn("position startpos moves " + " ".join(query["history_uci"]), sent)
            self.assertIn("ucinewgame", sent)
            self.assertEqual(received[-1].split()[:2], ["bestmove", query["bestmove_uci"]])
            commands = [line.split() for line in sent if line.startswith("go ")]
            self.assertEqual(len(commands), 1)
            command = commands[0]
            self.assertEqual(int(command[command.index("depth") + 1]), query["requested_depth"])
            self.assertEqual(int(command[command.index("nodes") + 1]), query["requested_nodes"])
            if query["root_moves"] is None:
                self.assertNotIn("searchmoves", command)
            else:
                self.assertEqual(set(command[command.index("searchmoves") + 1:]), set(query["root_moves"]))
            witnesses = []
            for line in received:
                words = line.split()
                if not line.startswith("info depth ") or "score" not in words or "pv" not in words:
                    continue
                at = words.index("score")
                if (int(words[2]) == query["depth"] and words[at + 1] == query["score"]["kind"]
                        and int(words[at + 2]) == query["score"]["stm"]
                        and "lowerbound" not in words and "upperbound" not in words
                        and words[words.index("pv") + 1:] == query["pv_uci"]):
                    witnesses.append(line)
            self.assertTrue(witnesses, query["query_id"])

    def test_actual_new_positions_reach_priority_runtime_candidates_and_remain_read_only(self):
        base = knowledge.repertoire()
        base_edges = {(row["position_keys"][i], uci) for row in base for i, uci in enumerate(row["moves"])}
        novel_positions = set()
        for entry in self.data["entries"]:
            board = support.board_for(entry["history_uci"])
            saved = board.copy(stack=True)
            rows = knowledge.runtime_proposals(board, "actual-runtime", limit=1)
            self.assertTrue(rows, entry["record_id"])
            self.assertEqual(rows[0]["source"], support.SOURCE)
            self.assertTrue(knowledge.valid_proposal(board, rows[0]))
            self.assertTrue(knowledge.context(board)["has_any_continuation"])
            self.assertTrue(knowledge.context(board)["has_native_continuation"])
            self.assertEqual(rows[0]["loss_cap_cp"], 0)
            self.assertTrue(rows[0]["candidate_only"])
            self.assertEqual(board.fen(), saved.fen())
            self.assertEqual(board.move_stack, saved.move_stack)
            plan = boss_play.plan(board, 1000, "support-host-plan", engine_identity=self.data["engine_identity"])
            candidate = next((row for row in plan.get("knowledge_candidates", [])
                              if row["uci"] == entry["uci"]), None)
            self.assertIsNotNone(candidate, entry["record_id"])
            if candidate["source"] == support.SOURCE:
                self.assertTrue(knowledge.valid_proposal(board, candidate))
                self.assertEqual(candidate["loss_cap_cp"], 0)
            else:
                # Existing independently valid memory can name the same move.
                # Adding another study must not erase that earlier evidence.
                self.assertEqual(candidate["source"], "native_practice_memory")
                self.assertTrue(boss_play.gambit_practice_memory.valid_proposal(
                    board, candidate, engine_identity=self.data["engine_identity"]))
            if (entry["position_key"], entry["uci"]) not in base_edges:
                novel_positions.add(entry["position_key"])
        self.assertGreaterEqual(len(novel_positions), 30)
        meta = knowledge.metadata()
        self.assertEqual(meta["extension_manifest_sha256"], self.manifest["manifest_sha256"])
        self.assertEqual(meta["support_id"], self.data["support_id"])

    def test_proposals_reject_forged_provenance_even_for_the_same_legal_move(self):
        entry = self.data["entries"][0]
        board = support.board_for(entry["history_uci"])
        original = knowledge.runtime_proposals(board, "mutation", limit=1)[0]
        for field, wrong in {"support_id":"stale", "extension_manifest_sha256":"stale",
                "family_id":"foreign", "query_id":"unrelated", "line_id":"unrelated",
                "engine_identity":{}, "loss_cap_cp":35, "fen":"wrong",
                "source_prefix_uci":[], "native_depth":1}.items():
            with self.subTest(field=field):
                changed = copy.deepcopy(original)
                changed[field] = wrong
                self.assertFalse(knowledge.valid_proposal(board, changed))

    def test_same_fen_without_the_real_history_or_foreign_family_does_not_authorize_an_edge(self):
        entry = self.data["entries"][0]
        board = support.board_for(entry["history_uci"])
        row = knowledge.runtime_proposals(board, "history", limit=1)[0]
        self.assertFalse(knowledge.valid_proposal(chess.Board(board.fen()), row))
        self.assertEqual(support.proposals(board, active_families={"not-this-family"}, seed="x"), [])
        self.assertFalse(support.valid_proposal(board, row, active_families=set()))

    def _temporary_data(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        directory = Path(folder.name)
        for name in ("catalog.json", "native_continuations.json", "native_continuations_manifest.json"):
            shutil.copyfile(support.DIRECTORY / name, directory / name)
        return directory

    def _rewrite(self, directory, data):
        data["support_id"] = support.digest({key:value for key,value in data.items() if key != "support_id"})
        raw = (json.dumps(data, ensure_ascii=False, sort_keys=True) + "\n").encode()
        (directory / "native_continuations.json").write_bytes(raw)
        manifest = copy.deepcopy(self.manifest)
        manifest.pop("manifest_sha256", None)
        manifest["support_id"] = data["support_id"]
        manifest["data_sha256"] = hashlib.sha256(raw).hexdigest()
        (directory / "native_continuations_manifest.json").write_text(json.dumps(manifest))

    def test_loader_checks_semantics_even_when_a_mutated_file_has_a_matching_checksum(self):
        for field in ("family", "restricted", "incomplete", "history", "depth", "origin"):
            with self.subTest(field=field):
                directory = self._temporary_data()
                data = copy.deepcopy(self.data)
                entry = data["entries"][0]
                query = data["queries"][entry["query_id"]]
                if field == "family": entry["family_id"] = "foreign"
                elif field == "restricted": query["root_moves"] = [entry["uci"]]
                elif field == "incomplete": query["completed"] = False
                elif field == "history": query["history_uci"] = []
                elif field == "depth": query["depth"] = 3
                else: data["origin"] = "external teacher"
                self._rewrite(directory, data)
                self.assertIsNotNone(support.load(directory)[3])

    def test_new_manifest_invalidates_old_proposals_without_an_explicit_cache_clear(self):
        directory = self._temporary_data()
        entry = self.data["entries"][0]
        board = support.board_for(entry["history_uci"])
        with patch.object(support, "DIRECTORY", directory):
            old = support.proposals(board, active_families={entry["family_id"]}, seed="old", limit=1)[0]
            changed = copy.deepcopy(self.data)
            changed["revision_note"] = "second installed support revision"
            self._rewrite(directory, changed)
            self.assertFalse(support.valid_proposal(board, old, active_families={entry["family_id"]}))
            fresh = support.proposals(board, active_families={entry["family_id"]}, seed="new", limit=1)[0]
            self.assertNotEqual(fresh["extension_manifest_sha256"], old["extension_manifest_sha256"])
            self.assertTrue(support.valid_proposal(board, fresh, active_families={entry["family_id"]}))

    def test_corrupt_support_fails_closed_without_changing_the_original_catalog(self):
        directory = self._temporary_data()
        original = (support.DIRECTORY / "catalog.json").read_bytes()
        (directory / "native_continuations.json").write_bytes(b"{}")
        self.assertIsNotNone(support.load(directory)[3])
        self.assertEqual((support.DIRECTORY / "catalog.json").read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
