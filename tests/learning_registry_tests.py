"""Registry integrity and compare-and-swap tests; native checks are mocked here."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
import learning_registry as registry
import learning_arena as arena
from learning_arena_tests import replay_fixture, rule_game


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.binary = self.root / "regicide.exe"
        self.binary.write_bytes(b"fake executable for integrity unit tests only")
        self.candidate = self.root / "candidate.nn"
        self.candidate.write_bytes(b"fake model for integrity unit tests only")
        self.native = patch.object(registry, "validate_native_model", return_value={"score_cp": 0})
        self.native.start()
        self.addCleanup(self.native.stop)
        self.addCleanup(self.temporary.cleanup)

    def document(self, relative="models/000001-model.nn"):
        target = self.state / "models" / "000001-model.nn"
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(self.candidate.read_bytes())
        return {"schema": registry.SCHEMA, "relative_model_path": relative, "generation": 1,
                "model_sha256": registry.sha256(target), "engine_sha256": registry.sha256(self.binary)}

    def save(self, document):
        (self.state / "champion.json").write_text(json.dumps(document), encoding="utf-8")

    def passing_report(self, champion=None):
        output = self.root / "arena"
        output.mkdir()
        report = {"artifact_directory": str(output), "candidate_sha256": registry.sha256(self.candidate),
                  "binary_sha256": registry.sha256(self.binary), "champion_sha256": champion,
                  "attempt_index": 1}
        (output / "arena.json").write_text(json.dumps(report), encoding="utf-8")
        return report

    def full_synthetic_artifacts(self):
        """Legal mate histories for rule tests, with the opening generator mocked.

        This is explicitly synthetic evidence, not an actual arena strength run.
        Only load_openings and the native parser are mocked in its positive test.
        """
        report = replay_fixture()
        output = self.root / "full-arena"
        output.mkdir()
        (output / "candidate.nn").write_bytes(self.candidate.read_bytes())
        report.update(status="complete", eligible=True, promotion_passed=True, requested_pairs=32,
                      artifact_directory=str(output), binary_sha256=registry.sha256(self.binary),
                      candidate_sha256=registry.sha256(self.candidate), champion_sha256=None,
                      started_utc="2026-10-18T00:00:00+00:00")
        reserve = {"seed": 123, "openings": [{"moves": []} for _ in range(32)]}
        report["opening_reserve"] = reserve
        games = []
        for index in range(32):
            games.append(rule_game(2 * index + 1, index + 1,
                                   "e2e4 f7f6 d2d4 g7g5 d1h5".split(), True))
            games.append(rule_game(2 * index + 2, index + 1,
                                   "f2f3 e7e5 g2g4 d8h4".split(), False))
        report["games"] = games
        report["decision"] = arena.promotion_decision([1] * 32, 1, complete=True, clean=True)
        from collect_learning_data import OPTIONS
        report["engines"] = {role: {"binary_sha256": report["binary_sha256"],
            "model_sha256": report["candidate_sha256"] if role == "candidate" else None,
            "options": dict(OPTIONS, UseNeuralEvaluation=role == "candidate")}
            for role in ("baseline", "candidate")}
        report["engines"]["candidate"]["options"]["EvalFile"] = str(output / "candidate.nn")
        report["resource_guard"].update(snapshot_count=2, external_cpu_increase_ticks=0,
            observed_external_engine_cpu_increase=False, failure=None,
            transport_engines={"baseline": {"pid": 1}, "candidate": {"pid": 2}},
            owned_engines=[{"pid": 1}, {"pid": 2}])
        rows = [{"contaminated": False, "external_cpu_delta_ticks": 0,
                 "gap_seconds": .01, "snapshot_duration_seconds": .001}] * 2
        (output / "resource-samples.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        report["resource_samples_sha256"] = registry.sha256(output / "resource-samples.jsonl")
        (output / "games.pgn").write_text(arena._pgn(report), encoding="utf-8")
        report["pgn_sha256"] = registry.sha256(output / "games.pgn")
        (output / "arena.json").write_text(json.dumps(report), encoding="utf-8")
        return report, output, reserve

    def test_absent_registry_is_classical(self):
        self.assertIsNone(registry.load_champion(self.state, self.binary))

    def test_load_resolves_verified_immutable_model(self):
        self.save(self.document())
        item = registry.load_champion(self.state, self.binary)
        self.assertEqual(Path(item["model_path"]).read_bytes(), self.candidate.read_bytes())
        self.assertEqual(item["generation"], 1)

    def test_replaced_model_or_binary_rejected(self):
        for change in ("model", "binary"):
            self.save(self.document())
            path = self.binary if change == "binary" else self.state / "models/000001-model.nn"
            path.write_bytes(b"replacement")
            with self.assertRaisesRegex(ValueError, "SHA256|different engine"):
                registry.load_champion(self.state, self.binary)

    def test_path_traversal_and_absolute_paths_rejected(self):
        for path in ("../candidate.nn", str(self.candidate), "models/../candidate.nn",
                     "models\\model.nn", "outside/model.nn"):
            self.save(self.document(path))
            with self.assertRaises(ValueError):
                registry.load_champion(self.state, self.binary)

    def test_malformed_json_shapes_and_identity_types_raise_value_error(self):
        for document in ([], None, {"schema": registry.SCHEMA, "generation": 1,
                                   "model_sha256": None, "engine_sha256": 3}):
            self.save(document)
            with self.assertRaises(ValueError):
                registry.load_champion(self.state, self.binary)

    def test_fake_cached_pass_without_saved_evidence_rejected(self):
        with self.assertRaises(ValueError):
            registry.promote(self.state, self.binary, self.candidate,
                             {"decision": {"passed": True}}, None)
        self.assertFalse((self.state / "champion.json").exists())

    def test_failed_promotion_keeps_existing_champion(self):
        self.save(self.document())
        before = (self.state / "champion.json").read_bytes()
        report = self.passing_report()
        with patch.object(registry, "_verify_artifacts", side_effect=ValueError("unfinished game")):
            with self.assertRaises(ValueError):
                registry.promote(self.state, self.binary, self.candidate, report,
                                 registry.load_champion(self.state, self.binary)["model_sha256"])
        self.assertEqual(before, (self.state / "champion.json").read_bytes())

    def test_compare_and_swap_rejects_stale_champion(self):
        self.save(self.document())
        with self.assertRaisesRegex(ValueError, "compare-and-swap"):
            registry.promote(self.state, self.binary, self.candidate, {}, None)

    def test_validated_publication_creates_new_immutable_model(self):
        report = self.passing_report()
        decision = {"passed": True}
        with patch.object(registry, "_verify_artifacts", return_value=decision):
            champion = registry.promote(self.state, self.binary, self.candidate, report, None)
        self.assertEqual(champion["generation"], 1)
        self.assertEqual(Path(champion["model_path"]).read_bytes(), self.candidate.read_bytes())
        self.assertEqual(champion["model_sha256"], registry.sha256(self.candidate))
        self.assertEqual(champion["decision"], decision)
        self.assertNotIn("model_path", json.loads((self.state / "champion.json").read_text()))

    def test_candidate_identity_mismatch_prevents_publication(self):
        report = self.passing_report()
        self.candidate.write_bytes(b"changed after arena")
        with patch.object(registry, "_verify_artifacts", return_value={"passed": True}):
            with self.assertRaisesRegex(ValueError, "identities"):
                registry.promote(self.state, self.binary, self.candidate, report, None)
        self.assertFalse((self.state / "champion.json").exists())

    def test_insufficient_or_dirty_full_evidence_cannot_promote(self):
        item = replay_fixture()
        item.update(status="complete", promotion_passed=True, eligible=True)
        item["opening_reserve"] = arena.load_openings(arena.DEFAULT_OPENINGS, 1, 123)
        with self.assertRaises(ValueError):
            registry._verify_artifacts(item, self.root)

    def test_short_time_control_cannot_weaken_gate(self):
        item = replay_fixture()
        item.update(status="complete", movetime_ms=99)
        with self.assertRaisesRegex(ValueError, "100 ms"):
            registry._verify_artifacts(item, self.root)

    def test_atomic_manifest_failure_retains_previous_bytes(self):
        self.save(self.document())
        before = (self.state / "champion.json").read_bytes()
        with patch.object(registry.os, "replace", side_effect=OSError("simulated disk failure")):
            with self.assertRaises(OSError):
                registry._atomic_json(self.state / "champion.json", {"new": True})
        self.assertEqual((self.state / "champion.json").read_bytes(), before)

    def test_full_rule_evidence_and_artifact_checks_allow_atomic_promotion(self):
        report, output, reserve = self.full_synthetic_artifacts()
        with patch.object(registry, "load_openings", return_value=reserve):
            champion = registry.promote(self.state, self.binary, self.candidate, report, None)
        self.assertEqual(champion["generation"], 1)
        self.assertEqual(champion["arena_sha256"], registry.sha256(output / "arena.json"))

    def test_atomic_commit_has_no_fallible_native_validation_or_disk_read_afterward(self):
        report = self.passing_report()
        committed = False
        original_atomic, original_load, original_sha = registry._atomic_json, registry.load_champion, registry.sha256

        def atomic_commit(path, document):
            nonlocal committed
            original_atomic(path, document)
            committed = True

        def guarded_load(*arguments):
            if committed:
                self.fail("champion must not be re-read after the atomic commit")
            return original_load(*arguments)

        def guarded_sha(*arguments):
            if committed:
                self.fail("promotion must not read file bytes after the atomic commit")
            return original_sha(*arguments)

        def guarded_native(*arguments):
            if committed:
                self.fail("promotion must not start native validation after the atomic commit")
            return {"score_cp": 0}

        with (patch.object(registry, "_verify_artifacts", return_value={"passed": True}),
              patch.object(registry, "_atomic_json", side_effect=atomic_commit),
              patch.object(registry, "load_champion", side_effect=guarded_load),
              patch.object(registry, "sha256", side_effect=guarded_sha),
              patch.object(registry, "validate_native_model", side_effect=guarded_native)):
            champion = registry.promote(self.state, self.binary, self.candidate, report, None)
        self.assertTrue(committed)
        self.assertEqual(champion["generation"], 1)
        self.assertEqual(Path(champion["model_path"]).read_bytes(), self.candidate.read_bytes())

    def test_full_evidence_rejects_pgn_tampering(self):
        report, output, reserve = self.full_synthetic_artifacts()
        with (output / "games.pgn").open("a", encoding="utf-8") as stream:
            stream.write("tampered")
        with patch.object(registry, "load_openings", return_value=reserve):
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                registry.promote(self.state, self.binary, self.candidate, report, None)
        self.assertFalse((self.state / "champion.json").exists())

    def test_rehashed_contaminated_resource_evidence_is_still_rejected(self):
        report, output, reserve = self.full_synthetic_artifacts()
        rows = [{"contaminated": True, "external_cpu_delta_ticks": 0,
                 "gap_seconds": .01, "snapshot_duration_seconds": .001}] * 2
        (output / "resource-samples.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        report["resource_samples_sha256"] = registry.sha256(output / "resource-samples.jsonl")
        (output / "arena.json").write_text(json.dumps(report), encoding="utf-8")
        with patch.object(registry, "load_openings", return_value=reserve):
            with self.assertRaisesRegex(ValueError, "contaminated"):
                registry.promote(self.state, self.binary, self.candidate, report, None)

    def test_custom_opening_data_cannot_bypass_fixed_reserve(self):
        report, output, _ = self.full_synthetic_artifacts()
        with self.assertRaisesRegex(ValueError, "fixed held-out"):
            registry.promote(self.state, self.binary, self.candidate, report, None)


if __name__ == "__main__":
    unittest.main()
