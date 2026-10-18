"""Remote import integrity; all chess games are declared synthetic unit fixtures."""

import copy
import contextlib
import hashlib
import json
from pathlib import Path, PureWindowsPath
import sys
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import collect_learning_data as DATA
import learning_arena as ARENA
import learning_registry as REGISTRY
import learning_cycle as CYCLE
import remote_arena_import as IMPORT
from learning_arena_tests import rule_game
import learning_registry_tests as REGISTRY_TESTS

IMPLEMENTATION, HEAD, RESULT = "1" * 40, "2" * 40, "3" * 40


class RemoteImportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.state = self.root / "state"
        self.run = self.state / "runs/000006-synthetic"
        self.run.mkdir(parents=True)
        self.binary = self.root / "regicide.exe"
        self.binary.write_bytes(b"synthetic executable; never executed")
        self.candidate = self.root / "candidate.nn"
        self.candidate.write_bytes(b"synthetic model; native parser mocked")
        self.report, source_arena, self.reserve = REGISTRY_TESTS.RegistryTests.full_synthetic_artifacts(self)
        self.result_directory = self.root / "received"
        self.result_directory.mkdir()
        source_arena.rename(self.result_directory / "arena")
        self.report.update(attempt_index=6,
            decision=ARENA.promotion_decision([1] * 32, 6, complete=True, clean=True),
            native_model_validation={"candidate": {"model_sha256": DATA.sha256_file(self.candidate), "score_cp": 0}})
        self.payload = self.repo / "payload"
        self.payload.mkdir()
        (self.payload / "engine.zip").write_bytes(b"frozen ZIP bytes; worker extraction separately tested")
        (self.payload / "candidate.nn").write_bytes(self.candidate.read_bytes())
        source_files = {"scripts/fixture.py": b"# synthetic frozen source\n",
                        ".github/workflows/remote-learning-arena.yml": b"name: synthetic workflow\n",
                        "tests/data/learning_openings_v1.json": b"{}\n",
                        "tests/data/learning_arena_openings_v1.json": b"{}\n",
                        "tests/data/endgame_learning_seeds_v1.json": b"{}\n"}
        for name, raw in source_files.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        self.request = {"schema": "regicide.learning.remote-request.v1", "attempt_index": 6,
            "pairs": 32, "movetime_ms": 100, "seed": 123, "binary_zip": "payload/engine.zip",
            "binary_zip_sha256": DATA.sha256_file(self.payload / "engine.zip"),
            "binary_sha256": DATA.sha256_file(self.binary), "candidate_model": "payload/candidate.nn",
            "candidate_sha256": DATA.sha256_file(self.candidate), "champion_model": None, "champion_sha256": None,
            "implementation_commit": IMPLEMENTATION, "source_hash_basis": "git_blob_bytes",
            "source_files_sha256": {name: hashlib.sha256(raw).hexdigest() for name, raw in source_files.items()},
            "prepared_run": self.run.relative_to(self.state).as_posix(), "output_prefix": "docs/checks/unit/remote-result"}
        DATA.checkpoint_json(self.payload / "remote-request.json", self.request)
        request_sha = DATA.sha256_file(self.payload / "remote-request.json")
        branch = "validation/endgame-6-" + request_sha[:12]
        self.result_path = self.request["output_prefix"] + "/" + request_sha[:12]
        original_path = PureWindowsPath("D:/a/Regicide/Regicide") / self.result_path / "arena"
        self.report["artifact_directory"] = str(original_path)
        self.report["engines"]["candidate"]["options"]["EvalFile"] = str(original_path / "candidate.nn")
        self.report["stop_when_impossible"] = True
        self.report["futility_certificate"] = None
        self.report["valid_for_rejection"] = False
        self.remote = {"schema": "regicide.learning.remote-result.v1", "status": "complete", "errors": [],
            "promotion_performed": False, "request_path": "payload/remote-request.json", "request_sha256": request_sha,
            "checked_out_commit": HEAD, "implementation_commit": IMPLEMENTATION,
            "prepared_run": self.request["prepared_run"], "attempt_index": 6, "result_branch": branch,
            "result_path": self.result_path, "host": {"platform": "Windows-unit-fixture", "github": {
                "GITHUB_REPOSITORY": "bigpine-choi/Regicide", "GITHUB_SHA": HEAD, "GITHUB_RUN_ID": "123456",
                "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SERVER_URL": "https://github.com", "RUNNER_OS": "Windows"}},
            "arena_status": "complete", "decision": self.report["decision"], "valid_for_rejection": False}
        self.receipt = {"schema": IMPORT.RECEIPT_SCHEMA, "repository": IMPORT.REPOSITORY,
            "workflow_path": IMPORT.WORKFLOW, "event": "push", "status": "completed", "conclusion": "success",
            "run_attempt": 1, "run_id": "123456", "head_sha": HEAD, "result_commit": RESULT, "result_branch": branch}
        fixed_candidate = self.run / "curve/selected.nn"
        fixed_candidate.parent.mkdir()
        fixed_candidate.write_bytes(self.candidate.read_bytes())
        self.study = {"attempt_index": 6, "binary_sha256": DATA.sha256_file(self.binary),
            "checkout": {"commit": IMPLEMENTATION}, "previous_model_sha256": None,
            "candidate": {"model_path": "curve/selected.nn", "model_sha256": DATA.sha256_file(fixed_candidate)},
            "remote_request": {"path": "payload/remote-request.json", "sha256": request_sha,
                "expected_result_branch": branch, "seed": 123,
                "payload_sha256": {path.relative_to(self.repo).as_posix(): DATA.sha256_file(path) for path in self.payload.iterdir()}}}
        self.bad_parent, self.bad_blob = False, False
        self.bad_git_mode = False
        self.refresh_result()

    def refresh_result(self):
        arena = self.result_directory / "arena"
        (arena / "games.pgn").write_text(ARENA._pgn(self.report), encoding="utf-8", newline="")
        self.report["pgn_sha256"] = DATA.sha256_file(arena / "games.pgn")
        DATA.checkpoint_json(arena / "arena.json", self.report)
        self.remote.update(arena_status=self.report["status"], decision=self.report["decision"],
                           valid_for_rejection=self.report.get("valid_for_rejection", False))
        DATA.checkpoint_json(self.result_directory / "remote-result.json", self.remote)
        (self.result_directory / "request.json").write_bytes((self.payload / "remote-request.json").read_bytes())
        entries = [{"path": path.relative_to(self.result_directory).as_posix(), "sha256": DATA.sha256_file(path), "bytes": path.stat().st_size}
                   for path in sorted(self.result_directory.rglob("*")) if path.is_file() and path.name != "content-manifest.json"]
        DATA.checkpoint_json(self.result_directory / "content-manifest.json", {"schema": "regicide.learning.remote-content.v1", "files": entries})
        self.git_files = {path.relative_to(self.repo).as_posix(): path.read_bytes() for path in self.repo.rglob("*") if path.is_file()}
        self.git_blobs = {}
        records = []
        for path in sorted(self.result_directory.rglob("*")):
            if path.is_file():
                raw = path.read_bytes()
                blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
                self.git_blobs[blob] = raw
                records.append((self.result_path + "/" + path.relative_to(self.result_directory).as_posix(), blob))
        self.git_tree = records

    def git(self, root, *args, **kwargs):
        if args[0] == "rev-parse":
            return (args[-1].split("^")[0] + "\n").encode()
        if args[:3] == ("show", "-s", "--format=%P"):
            return (("4" * 40 if self.bad_parent else HEAD) + "\n").encode()
        if args[0] == "show":
            return self.git_files[args[1].split(":", 1)[1]]
        if args[0] == "merge-base":
            return b""
        if args[0] == "diff-tree":
            return ("\n".join(path for path, _ in self.git_tree) + "\n").encode()
        if args[0] == "ls-tree":
            mode = "120000" if self.bad_git_mode else "100644"
            return b"".join(f"{mode} blob {blob}\t{path}\0".encode() for path, blob in self.git_tree)
        if args[0] == "cat-file":
            return b"tampered Git blob" if self.bad_blob else self.git_blobs[args[-1]]
        raise AssertionError("unexpected Git invocation: " + repr(args))

    def invoke(self, *, real_git=False, freeze_error=None):
        git_context = contextlib.nullcontext() if real_git else patch.object(IMPORT.REMOTE, "git", side_effect=self.git)
        with patch.object(IMPORT, "ROOT", self.repo), git_context, \
             patch.object(ARENA, "load_openings", return_value=self.reserve), \
             patch.object(REGISTRY, "load_openings", return_value=self.reserve), \
             patch.object(REGISTRY, "validate_native_model", return_value={"score_cp": 0}), \
             patch("endgame_study.verify_frozen", side_effect=freeze_error), patch.object(CYCLE, "catalog_read", return_value={}):
            return IMPORT.verify_and_import(self.run, self.state, self.binary, self.study, self.result_directory, self.receipt)

    def test_full_positive_uses_original_registry_after_path_only_relocation(self):
        original = {path.relative_to(self.result_directory).as_posix(): path.read_bytes() for path in self.result_directory.rglob("*") if path.is_file()}
        result = self.invoke()
        self.assertTrue(result["adopted"])
        self.assertEqual(result["generation"], 1)
        champion = json.loads((self.state / "champion.json").read_text())
        self.assertEqual(champion["model_sha256"], self.request["candidate_sha256"])
        for name, raw in original.items():
            self.assertEqual((self.run / "remote-evidence" / name).read_bytes(), raw)
        local = json.loads((self.run / "arena/arena.json").read_text())
        expected = copy.deepcopy(self.report)
        expected["artifact_directory"] = str(self.run / "arena")
        expected["engines"]["candidate"]["options"]["EvalFile"] = str(self.run / "arena/candidate.nn")
        self.assertEqual(local, expected)
        audit = json.loads((self.run / "remote-import.json").read_text())
        self.assertEqual(set(audit["path_only_relocations"]), {"artifact_directory", "engines.candidate.options.EvalFile"})

    def make_losses(self, early=False):
        games = []
        for index in range(6 if early else 32):
            games.append(rule_game(2 * index + 1, index + 1, "f2f3 e7e5 g2g4 d8h4".split(), True))
            games.append(rule_game(2 * index + 2, index + 1, "e2e4 f7f6 d2d4 g7g5 d1h5".split(), False))
        self.report["games"] = games
        self.report["decision"] = ARENA.promotion_decision([0] * (6 if early else 32), 6, complete=not early, clean=not early)
        self.report.update(eligible=not early, promotion_passed=False, completed=not early,
                           status="rejected_early_futile" if early else "complete", valid_for_rejection=early)
        self.report["resource_guard"]["valid_for_comparison"] = not early
        self.report["futility_certificate"] = ARENA.futility_certificate([0] * 6, 32, 6) if early else None
        self.refresh_result()

    def test_complete_nonpass_is_fully_checked_and_retains_classical(self):
        self.make_losses()
        result = self.invoke()
        self.assertFalse(result["adopted"])
        self.assertEqual(result["arena"]["status"], "complete")
        self.assertFalse((self.state / "champion.json").exists())

    def test_early_rejection_verifies_factual_prefix_and_never_promotes(self):
        self.make_losses(early=True)
        result = self.invoke()
        self.assertFalse(result["adopted"])
        self.assertEqual(result["arena"]["status"], "rejected_early_futile")
        self.assertTrue(result["arena"]["valid_for_rejection"])
        self.assertEqual(len(json.loads((self.run / "arena/arena.json").read_text())["games"]), 12)
        self.assertFalse((self.state / "champion.json").exists())

    def test_bad_workflow_receipt_rejected_without_local_evidence_or_champion(self):
        original = copy.deepcopy(self.receipt)
        for key, value in (("conclusion", "failure"), ("run_attempt", 2), ("repository", "other/repo"),
                           ("workflow_path", ".github/workflows/other.yml"), ("event", "workflow_dispatch")):
            self.receipt = {**original, key: value}
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.invoke()
            self.assertFalse((self.run / "remote-evidence").exists())
            self.assertFalse((self.state / "champion.json").exists())

    def test_wrong_git_parent_and_blob_fail_even_with_matching_local_manifest(self):
        self.bad_parent = True
        with self.assertRaisesRegex(ValueError, "direct single-parent"):
            self.invoke()
        self.bad_parent, self.bad_blob = False, True
        with self.assertRaisesRegex(ValueError, "Git blob bytes"):
            self.invoke()

    def test_git_symlink_mode_and_modified_workflow_source_are_rejected(self):
        self.bad_git_mode = True
        with self.assertRaisesRegex(ValueError, "nonregular result"):
            self.invoke()
        self.bad_git_mode = False
        self.git_files[".github/workflows/remote-learning-arena.yml"] = b"changed workflow"
        with self.assertRaisesRegex(ValueError, "source blob changed"):
            self.invoke()

    def test_remote_host_facts_must_match_authenticated_receipt(self):
        self.remote["host"]["github"]["GITHUB_RUN_ID"] = "different-run"
        self.refresh_result()
        with self.assertRaisesRegex(ValueError, "host GitHub"):
            self.invoke()

    def test_remote_failure_or_claimed_remote_promotion_is_never_accepted(self):
        original = copy.deepcopy(self.remote)
        for key, value in (("status", "failed"), ("promotion_performed", True)):
            self.remote = {**original, key: value}
            self.refresh_result()
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "provenance"):
                self.invoke()
            self.assertFalse((self.state / "champion.json").exists())

    def test_evalfile_must_originally_name_exact_windows_snapshot(self):
        self.report["engines"]["candidate"]["options"]["EvalFile"] = "C:\\unrelated\\candidate.nn"
        self.refresh_result()
        with self.assertRaisesRegex(ValueError, "remote EvalFile"):
            self.invoke()

    def test_content_extra_file_or_modified_bytes_are_not_imported(self):
        path = self.result_directory / "unexpected.txt"
        path.write_bytes(b"unexpected")
        with self.assertRaisesRegex(ValueError, "manifest"):
            self.invoke()
        path.unlink()
        with (self.result_directory / "arena/resource-samples.jsonl").open("ab") as handle:
            handle.write(b" ")
        with self.assertRaisesRegex(ValueError, "manifest"):
            self.invoke()

    def test_fake_manifest_traversal_and_missing_files_rejected(self):
        content = self.result_directory / "content-manifest.json"
        value = json.loads(content.read_text())
        value["files"][0]["path"] = "../outside"
        DATA.checkpoint_json(content, value)
        with self.assertRaises(ValueError):
            self.invoke()

    def test_authenticated_dirty_resource_evidence_still_rejected(self):
        self.report["resource_guard"]["external_cpu_increase_ticks"] = 1
        self.refresh_result()
        with self.assertRaisesRegex(ValueError, "resource identity"):
            self.invoke()

    def test_changed_local_payload_or_candidate_prevents_any_import(self):
        (self.payload / "candidate.nn").write_bytes(b"changed payload")
        with self.assertRaisesRegex(ValueError, "payload changed"):
            self.invoke()

    def test_forged_futility_certificate_cannot_be_imported(self):
        self.make_losses(early=True)
        self.report["futility_certificate"]["unplayed_pairs"] = 0
        self.refresh_result()
        with self.assertRaisesRegex(ValueError, "certificate"):
            self.invoke()

    def test_nooverwrite_existing_remote_evidence(self):
        target = self.run / "remote-evidence"
        target.mkdir()
        (target / "keep").write_bytes(b"keep")
        with self.assertRaisesRegex(ValueError, "overwrite"):
            self.invoke()
        self.assertEqual((target / "keep").read_bytes(), b"keep")

    def test_changed_source_freeze_blocks_even_a_negative_final_decision(self):
        self.make_losses()
        with self.assertRaisesRegex(ValueError, "source changed"):
            self.invoke(freeze_error=ValueError("source changed during import"))
        self.assertFalse((self.state / "champion.json").exists())

    def test_manifest_authorized_extra_file_is_still_outside_the_contract(self):
        (self.result_directory / "extra.txt").write_bytes(b"undeclared contract payload")
        self.refresh_result()
        with self.assertRaisesRegex(ValueError, "contract files"):
            self.invoke()

    def test_previously_absent_champion_cannot_change_before_import(self):
        model = self.state / "models/000001-old.nn"
        model.parent.mkdir()
        model.write_bytes(b"different previous model")
        DATA.checkpoint_json(self.state / "champion.json", {"schema": REGISTRY.SCHEMA,
            "generation": 1, "relative_model_path": "models/000001-old.nn",
            "model_sha256": DATA.sha256_file(model), "engine_sha256": DATA.sha256_file(self.binary)})
        with self.assertRaisesRegex(ValueError, "previous champion changed"):
            self.invoke()

    def test_real_temporary_git_parent_and_blob_objects_bind_the_import(self):
        """Only a disposable synthetic fixture repository receives commits here."""
        def git(*args):
            return subprocess.check_output(["git", "-C", str(self.repo), *args], stderr=subprocess.STDOUT)
        def commit(message):
            git("-c", "user.name=Synthetic integrity tests", "-c", "user.email=synthetic@example.invalid",
                "commit", "--quiet", "-m", message)
            return git("rev-parse", "HEAD").decode().strip()
        git("init", "--quiet")
        (self.repo / ".gitattributes").write_text("* -text -whitespace\n", encoding="utf-8")
        git("add", "--", ".gitattributes", "scripts", ".github", "tests")
        implementation = commit("Synthetic implementation fixture")
        self.request["implementation_commit"] = implementation
        DATA.checkpoint_json(self.payload / "remote-request.json", self.request)
        request_sha = DATA.sha256_file(self.payload / "remote-request.json")
        branch = "validation/endgame-6-" + request_sha[:12]
        self.study["checkout"]["commit"] = implementation
        self.study["remote_request"].update(sha256=request_sha, expected_result_branch=branch,
            payload_sha256={path.relative_to(self.repo).as_posix(): DATA.sha256_file(path) for path in self.payload.iterdir()})
        git("add", "--", "payload")
        head = commit("Synthetic request fixture")
        self.result_path = self.request["output_prefix"] + "/" + request_sha[:12]
        self.remote.update(request_sha256=request_sha, implementation_commit=implementation,
                           checked_out_commit=head, result_branch=branch, result_path=self.result_path)
        self.remote["host"]["github"]["GITHUB_SHA"] = head
        remote_path = PureWindowsPath("D:/a/Regicide/Regicide") / self.result_path / "arena"
        self.report["artifact_directory"] = str(remote_path)
        self.report["engines"]["candidate"]["options"]["EvalFile"] = str(remote_path / "candidate.nn")
        self.refresh_result()
        destination = self.repo / self.result_path
        destination.parent.mkdir(parents=True)
        shutil.copytree(self.result_directory, destination)
        git("add", "--", self.result_path)
        result_commit = commit("Synthetic result fixture")
        self.receipt.update(head_sha=head, result_commit=result_commit, result_branch=branch)
        result = self.invoke(real_git=True)
        self.assertTrue(result["adopted"])
        self.assertEqual(result["remote_import"]["result_commit"], result_commit)


if __name__ == "__main__":
    unittest.main()
