"""Follow-up audit tests; the bank class is an explicit preserved-run integration.

FollowupAuditTests needs the closed attempt-001 evidence and skips in a clean
clone without that evidence. SupplementAuditTests uses only temporary fixtures.
"""
from __future__ import annotations

import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import audit_neural_target_ab_v2 as audit
sys.path.insert(0, str(audit.v1.ROOT / "scripts"))
import neural_target_ab_data as data


@unittest.skipUnless((audit.OLD_ROOT/"heldout/test.jsonl").is_file(),
                     "integration requires the preserved, closed attempt-001 artifacts")
class FollowupAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        old = audit.OLD_ROOT
        paths = [old/"bank.json", old/"trainval/train.jsonl", old/"trainval/validation.jsonl", old/"heldout/test.jsonl"]
        cls.path = Path(data.make_bank(Path(cls.temporary.name)/"bank.json", exclusion_paths=paths,
                                      variants_per_family=4, variant_seed=20261127))
        cls.bank = audit.v1.read_json(cls.path)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_fresh_four_variant_bank_passes(self):
        result = audit.audit_bank(self.bank)
        self.assertEqual(result["families"], 80)
        self.assertEqual(result["variants"], 320)
        self.assertEqual(result["previous_attempt_exclusions"]["old_accepted_rows"], 1915)

    def test_metadata_and_three_variant_truncation_rejected(self):
        for key, value in (("variants_per_family", 2), ("variant_seed", 20261027), ("family_namespace", "")):
            bank = copy.deepcopy(self.bank)
            bank[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                audit.audit_bank(bank)
        bank = copy.deepcopy(self.bank)
        del bank["sources"][0]
        with self.assertRaisesRegex(ValueError, "320"):
            audit.audit_bank(bank)

    def test_duplicate_within_family_rejected(self):
        bank = copy.deepcopy(self.bank)
        replacement = copy.deepcopy(bank["sources"][0])
        replacement["seed_id"] = bank["sources"][1]["seed_id"]
        bank["sources"][1] = replacement
        with self.assertRaisesRegex(ValueError, "duplicate"):
            audit.audit_bank(bank)

    def test_old_parent_exclusion_removal_rejected_even_after_rehash(self):
        bank = copy.deepcopy(self.bank)
        old = audit.v1.read_json(audit.OLD_ROOT/"bank.json")
        target = old["sources"][0]["parent_feature_hash"]
        bank["exclusions"]["feature_hashes"].remove(target)
        with self.assertRaisesRegex(ValueError, "omits old"):
            audit.audit_prior_exclusions(bank)

    def test_exposed_old_artifact_receipt_removal_rejected(self):
        bank = copy.deepcopy(self.bank)
        path = str((audit.OLD_ROOT/"heldout/test.jsonl").resolve())
        bank["exclusions"]["artifacts"] = [item for item in bank["exclusions"]["artifacts"] if item["path"] != path]
        with self.assertRaisesRegex(ValueError, "omits an exposed"):
            audit.audit_prior_exclusions(bank)


class SupplementAuditTests(unittest.TestCase):
    def test_raw_cp_mate_and_bound_tampering_rejected(self):
        import json
        for kind, value, bound in (("cp", 71, ""), ("mate", -2, " lowerbound")):
            with tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary)
                lines = [("send", "go movetime 100"),
                         ("recv", f"info depth 4 score {kind} {value}{bound} nodes 10 pv g1f3"),
                         ("recv", "bestmove g1f3")]
                (path/"uci.jsonl").write_text("".join(json.dumps({"role": "candidate", "direction": direction,
                                                                  "line": line})+"\n" for direction, line in lines),
                                             encoding="utf-8")
                score = {"pov": "mover", "cp": value if kind == "cp" else None,
                         "mate": value if kind == "mate" else None, "lowerbound": bool(bound), "upperbound": False}
                document = {"games": [{"search_moves": [{"score": score}]}]}
                with mock.patch.object(audit, "ORIGINAL_MATCH_UCI_AUDIT", return_value={}):
                    self.assertEqual(audit.audit_match_uci(path, document)["raw_cp_mate_and_bound_reports_checked"], 1)
                    score["cp" if kind == "cp" else "mate"] += 1
                    with self.assertRaisesRegex(ValueError, "score/mate/bounds"):
                        audit.audit_match_uci(path, document)

    def test_individual_external_cpu_counter_changes_rejected(self):
        import json
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            rows = [{"utc": "2026-10-20T00:00:00+00:00", "processes": [
                {"pid": 1, "creation_ticks": 2, "cpu_ticks": 10, "owned": False}]},
                    {"utc": "2026-10-20T00:00:01+00:00", "processes": [
                {"pid": 1, "creation_ticks": 2, "cpu_ticks": 10, "owned": False}]}]
            def write():
                (path/"monitor.jsonl").write_text("".join(json.dumps(row)+"\n" for row in rows), encoding="utf-8")
            document = {"started_at": "2026-10-20T00:00:00.100000+00:00", "total_elapsed_seconds": .5}
            with mock.patch.object(audit, "ORIGINAL_MONITOR_AUDIT", return_value={}):
                write()
                self.assertTrue(audit.audit_monitor(path, document)["individual_external_cpu_counters_unchanged"])
                rows[1]["processes"][0]["cpu_ticks"] += 1
                write()
                with self.assertRaisesRegex(ValueError, "counter changed"):
                    audit.audit_monitor(path, document)


if __name__ == "__main__":
    unittest.main()
