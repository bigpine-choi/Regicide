"""Remote request provenance, bounded ZIPs and result-only publication controls."""

import copy
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import remote_learning_arena as REMOTE


def command(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.DEVNULL).decode().strip()


class RemoteArenaTests(unittest.TestCase):
    def fixture(self, temporary):
        root = Path(temporary)
        command(root, "init", "--quiet")
        command(root, "config", "user.name", "Fixture")
        command(root, "config", "user.email", "fixture@example.invalid")
        command(root, "config", "core.autocrlf", "false")
        for relative in ("scripts/remote_learning_arena.py", "tests/remote_learning_arena_tests.py",
                         "src/main.cpp", "include/regicide/board.hpp", ".github/workflows/remote-learning-arena.yml",
                         "tests/data/learning_openings_v1.json", "tests/data/learning_arena_openings_v1.json",
                         "tests/data/endgame_learning_seeds_v1.json"):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture source\n")
        command(root, "add", ".")
        command(root, "commit", "--quiet", "-m", "frozen implementation")
        implementation = command(root, "rev-parse", "HEAD")
        payload = root / "docs/checks/study/input"
        payload.mkdir(parents=True)
        binary = b"MZ" + bytes(range(256)) * 4
        with zipfile.ZipFile(payload / "engine.zip", "w", zipfile.ZIP_DEFLATED) as zipped:
            zipped.writestr("regicide.exe", binary)
        (payload / "candidate.nn").write_bytes(b"fixture model\n")
        request = {"schema": REMOTE.SCHEMA, "repository": "owner/Regicide", "attempt_index": 6,
                   "pairs": 32, "movetime_ms": 100, "seed": 2026101909,
                   "binary_zip": "docs/checks/study/input/engine.zip",
                   "binary_zip_sha256": REMOTE.sha(payload / "engine.zip"),
                   "binary_sha256": hashlib.sha256(binary).hexdigest(),
                   "candidate_model": "docs/checks/study/input/candidate.nn",
                   "candidate_sha256": REMOTE.sha(payload / "candidate.nn"),
                   "champion_model": None, "champion_sha256": None,
                   "source_files_sha256": {relative: REMOTE.sha(root / relative)
                                            for relative in sorted(REMOTE.source_inventory(root))},
                   "source_hash_basis": "git_blob_bytes", "implementation_commit": implementation,
                   "prepared_run": "runs/000006-fixture", "output_prefix": "docs/checks/study/remote-result"}
        path = payload / "remote-request.json"
        REMOTE.json_write(path, request)
        command(root, "add", ".")
        command(root, "commit", "--quiet", "-m", "new frozen request")
        return root, path.relative_to(root).as_posix(), request, implementation, command(root, "rev-parse", "HEAD")

    def context(self, head):
        return patch.dict(os.environ, {"GITHUB_ACTIONS": "true", "RUNNER_OS": "Windows",
            "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": head, "GITHUB_REPOSITORY": "owner/Regicide"})

    def run_fixture(self, root, path, before, head, *, status="complete", callback=None):
        def arena(binary, candidate, champion, output, **kwargs):
            output.mkdir()
            result = {"status": status, "decision": {"passed": False},
                      "valid_for_rejection": status == "rejected_early_futile"}
            REMOTE.json_write(output / "arena.json", result)
            (output / "games.pgn").write_text("fixture evidence\n")
            if callback:
                callback()
            return result
        with self.context(head), patch.object(REMOTE, "assert_no_result_branch"), \
                patch.object(REMOTE.ARENA, "run_arena", side_effect=arena) as run, \
                patch.object(REMOTE.ARENA, "verify_match_records") as verify, \
                patch.object(REMOTE.ARENA, "verify_early_rejection") as early:
            result = REMOTE.execute_request(root, path, before)
        return result, run, verify, early

    def test_valid_request_binds_blob_working_files_payloads_and_ancestor(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, path, request, before, head = self.fixture(temporary)
            actual, digest, commit = REMOTE.validate_request(root, path, "owner/Regicide")
            self.assertEqual(actual, request)
            self.assertEqual(digest, REMOTE.sha(root / path))
            self.assertEqual(commit, head)
            self.assertEqual(REMOTE.discover_request(root, before), path)
            REMOTE.assert_fresh_request(root, before, request)
            self.assertEqual(REMOTE.previous_commit(root, "0" * 40), before)

    def test_source_freeze_and_payload_mutation_fail_closed(self):
        for target in ("src/main.cpp", "docs/checks/study/input/candidate.nn", "docs/checks/study/input/engine.zip"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as temporary:
                root, path, request, _, _ = self.fixture(temporary)
                (root / target).write_bytes(b"tampered\n")
                with self.assertRaisesRegex(ValueError, "SHA256 mismatch"):
                    REMOTE.validate_request(root, path, "owner/Regicide")

    def test_missing_inventory_wrong_repository_limits_and_champion_pair_rejected(self):
        changes = [("source_files_sha256", {}), ("repository", "someone/else"), ("pairs", 31),
                   ("movetime_ms", 99), ("attempt_index", True), ("seed", -1),
                   ("source_hash_basis", "working_tree"), ("champion_model", "candidate.nn"),
                   ("output_prefix", "scripts/remote-result"), ("output_prefix", "docs/checks/x/remote-result-other")]
        with tempfile.TemporaryDirectory() as temporary:
            root, path, request, _, _ = self.fixture(temporary)
            for field, value in changes:
                changed = copy.deepcopy(request)
                changed[field] = value
                REMOTE.json_write(root / path, changed)
                with self.subTest(field=field, value=value), self.assertRaises((ValueError, subprocess.SubprocessError)):
                    REMOTE.validate_request(root, path, "owner/Regicide")

    def test_generic_attempt_and_optional_champion_allowed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, path, request, _, _ = self.fixture(temporary)
            request.update(attempt_index=12, pairs=64, movetime_ms=200,
                           champion_model=request["candidate_model"], champion_sha256=request["candidate_sha256"])
            REMOTE.json_write(root / path, request)
            actual, _, _ = REMOTE.validate_request(root, path, "owner/Regicide")
            self.assertEqual(actual["attempt_index"], 12)

    def test_traversal_and_symlink_paths_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for value in ("../escape", "/absolute", "C:/drive", "a\\b", "a/../b", "./a", "a//b", "a\nname"):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    REMOTE.relative_path(root, value, must_exist=False)
            target = root / "target"
            target.write_bytes(b"x")
            link = root / "link"
            try:
                link.symlink_to(target)
            except OSError:
                return  # Windows CI accounts may lack symlink creation privilege.
            with self.assertRaisesRegex(ValueError, "symlinks"):
                REMOTE.relative_path(root, "link")

    def test_zip_exact_binary_success_and_unsafe_entries_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, _, request, _, _ = self.fixture(temporary)
            output = root / "unpacked.exe"
            REMOTE.extract_binary(root / request["binary_zip"], output, request["binary_sha256"])
            self.assertEqual(REMOTE.sha(output), request["binary_sha256"])
            for names in (("../regicide.exe",), ("folder/regicide.exe",), ("regicide.exe", "extra")):
                archive = root / "bad.zip"
                with zipfile.ZipFile(archive, "w") as zipped:
                    for name in names:
                        zipped.writestr(name, b"MZx")
                with self.subTest(names=names), self.assertRaisesRegex(ValueError, "only the top-level"):
                    REMOTE.extract_binary(archive, output, request["binary_sha256"])

    def test_zip_symlink_expansion_and_hash_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive, output = root / "bad.zip", root / "out.exe"
            entry = zipfile.ZipInfo("regicide.exe")
            entry.create_system = 3
            entry.external_attr = (stat.S_IFLNK | 0o777) << 16
            with zipfile.ZipFile(archive, "w") as zipped:
                zipped.writestr(entry, b"MZtarget")
            with self.assertRaisesRegex(ValueError, "unsupported type"):
                REMOTE.extract_binary(archive, output, "0" * 64)
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
                zipped.writestr("regicide.exe", b"MZ" + b"x" * 100000)
            with self.assertRaisesRegex(ValueError, "expansion"):
                REMOTE.extract_binary(archive, output, "0" * 64)
            with zipfile.ZipFile(archive, "w") as zipped:
                zipped.writestr("regicide.exe", b"MZwrong")
            with self.assertRaisesRegex(ValueError, "SHA256 mismatch"):
                REMOTE.extract_binary(archive, output, "0" * 64)

    def test_completed_remote_comparison_records_binding_and_no_promotion(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, path, request, before, head = self.fixture(temporary)
            report, run, verify, early = self.run_fixture(root, path, before, head)
            self.assertEqual(report["status"], "complete")
            self.assertFalse(report["promotion_performed"])
            self.assertEqual(report["checked_out_commit"], head)
            self.assertEqual(report["request_sha256"], REMOTE.sha(root / path))
            self.assertEqual(run.call_args.kwargs, {"pairs": 32, "movetime_ms": 100,
                "seed": request["seed"], "attempt_index": 6, "stop_when_impossible": True})
            self.assertIsNone(run.call_args.args[2])
            verify.assert_called_once()
            early.assert_not_called()
            directory = root / report["result_path"]
            self.assertEqual(REMOTE.sha(directory / "request.json"), report["request_sha256"])
            manifest = json.loads((directory / "content-manifest.json").read_text())
            self.assertFalse(list(directory.rglob("*.exe")))
            for entry in manifest["files"]:
                self.assertEqual(REMOTE.sha(directory / entry["path"]), entry["sha256"])

    def test_futile_rejection_validated_separately_and_failed_arena_not_strength(self):
        for status, expected in (("rejected_early_futile", "complete"), ("failed", "failed")):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temporary:
                root, path, _, before, head = self.fixture(temporary)
                report, _, verify, early = self.run_fixture(root, path, before, head, status=status)
                self.assertEqual(report["status"], expected)
                self.assertFalse(report["promotion_performed"])
                self.assertTrue((root / report["result_path"] / "content-manifest.json").exists())
                verify.assert_not_called()
                self.assertEqual(early.call_count, int(status == "rejected_early_futile"))

    def test_mutating_input_after_arena_blocks_valid_remote_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, path, request, before, head = self.fixture(temporary)
            report, _, _, _ = self.run_fixture(root, path, before, head,
                callback=lambda: (root / request["candidate_model"]).write_bytes(b"changed"))
            self.assertEqual(report["status"], "failed")
            self.assertTrue(report["errors"])
            self.assertNotIn("decision", report)

    def test_local_wrong_checkout_or_retry_cannot_execute_arena(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, path, _, before, head = self.fixture(temporary)
            for field, value in (("GITHUB_ACTIONS", "false"), ("RUNNER_OS", "Linux"),
                                 ("GITHUB_RUN_ATTEMPT", "2"), ("GITHUB_SHA", "0" * 40)):
                with self.context(head), patch.dict(os.environ, {field: value}), \
                        patch.object(REMOTE.ARENA, "run_arena") as run:
                    with self.subTest(field=field), self.assertRaisesRegex(ValueError, "first run"):
                        REMOTE.execute_request(root, path, before)
                    run.assert_not_called()

    def test_repeated_attempt_freshness_and_unchanged_request_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, path, request, _, head = self.fixture(temporary)
            with self.assertRaisesRegex(ValueError, "already requested"):
                REMOTE.assert_fresh_request(root, head, request)
            with self.assertRaisesRegex(ValueError, "exactly one"):
                REMOTE.discover_request(root, head)

    def test_publication_stages_only_results_and_keeps_request_commit_as_parent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, path, _, before, head = self.fixture(temporary)
            report, _, _, _ = self.run_fixture(root, path, before, head)
            original = REMOTE.git
            pushes = []
            def local_git(root, *args, **kwargs):
                if args[0] == "push":
                    pushes.append(args)
                    return b""
                return original(root, *args, **kwargs)
            with self.context(head), patch.object(REMOTE, "assert_no_result_branch"), patch.object(REMOTE, "git", side_effect=local_git):
                REMOTE.publish_result(root, report["result_path"])
            self.assertEqual(command(root, "rev-parse", "HEAD^"), head)
            self.assertEqual(command(root, "branch", "--show-current"), report["result_branch"])
            changed = command(root, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").splitlines()
            self.assertTrue(all(value.startswith(report["result_path"] + "/") for value in changed))
            self.assertEqual(pushes, [("push", "origin", "HEAD:refs/heads/" + report["result_branch"])])


if __name__ == "__main__":
    unittest.main()
