"""Nested learning modules must be frozen and shipped with their regressions."""
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import learning_cycle as cycle
import endgame_study as study
import remote_learning_arena as remote


class SourceInventoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.sources = {
            "scripts/train_neural.py", "scripts/neural_search_data/engine.py",
            "scripts/neural_search_data/deeper/schema.py", "tests/neural_training_tests.py",
            "tests/neural_search_data/test_engine.py", "src/main.cpp",
            "include/regicide/board.hpp", ".github/workflows/rules.yml",
            ".github/workflows/remote.yaml", "tests/data/learning_openings_v1.json",
            "tests/data/learning_arena_openings_v1.json",
            "tests/data/endgame_learning_seeds_v1.json",
        }
        for relative in self.sources:
            self.write(relative, "fixture source\n")
        self.stack_cycle = patch.object(cycle, "ROOT", self.root)
        self.stack_study = patch.object(study, "ROOT", self.root)
        self.stack_cycle.start()
        self.stack_study.start()
        self.addCleanup(self.stack_cycle.stop)
        self.addCleanup(self.stack_study.stop)

    def write(self, relative, contents):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
        return path

    def test_nested_modules_and_regressions_match_remote_inventory(self):
        local = study.frozen_sources()
        self.assertEqual(set(local), self.sources)
        self.assertEqual(remote.source_inventory(self.root), set(local))
        expected_cycle = {name for name in self.sources if name.endswith((".py", ".json"))}
        self.assertEqual(set(cycle.implementation_hashes()), expected_cycle)
        for relative, digest in local.items():
            self.assertEqual(digest, remote.sha(remote.relative_path(self.root, relative)))

    def test_nested_tampering_changes_both_local_and_remote_hashes(self):
        relative = "scripts/neural_search_data/deeper/schema.py"
        cycle_before, study_before = cycle.implementation_hashes(), study.frozen_sources()
        remote_before = {name: remote.sha(self.root / name)
                         for name in remote.source_inventory(self.root)}
        path = self.write(relative, "changed nested module\n")
        expected = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertNotEqual(cycle_before[relative], expected)
        self.assertNotEqual(study_before[relative], expected)
        self.assertNotEqual(remote_before[relative], expected)
        self.assertEqual(cycle.implementation_hashes()[relative], expected)
        self.assertEqual(study.frozen_sources()[relative], expected)
        self.assertEqual(remote.sha(remote.relative_path(self.root, relative)), expected)

    def test_nested_addition_and_removal_change_every_inventory(self):
        relative = "tests/neural_search_data/deeper/new_regression.py"
        path = self.write(relative, "new regression\n")
        self.assertIn(relative, cycle.implementation_hashes())
        self.assertIn(relative, study.frozen_sources())
        self.assertIn(relative, remote.source_inventory(self.root))
        path.unlink()
        self.assertNotIn(relative, cycle.implementation_hashes())
        self.assertNotIn(relative, study.frozen_sources())
        self.assertNotIn(relative, remote.source_inventory(self.root))

    def test_non_source_files_and_directories_are_excluded(self):
        for relative in ("scripts/neural_search_data/__pycache__/engine.pyc",
                         "scripts/neural_search_data/notes.txt", "tests/other_fixture.json"):
            self.write(relative, "not a declared source\n")
        (self.root / "scripts/not_a_module.py").mkdir()
        self.assertEqual(set(study.frozen_sources()), self.sources)
        self.assertEqual(remote.source_inventory(self.root), self.sources)
        self.assertNotIn("scripts/not_a_module.py", cycle.implementation_hashes())

    def test_remote_path_security_still_rejects_traversal(self):
        for relative in ("../outside.py", "scripts/../outside.py", "/outside.py",
                         "scripts\\neural_search_data\\engine.py"):
            with self.subTest(relative=relative), self.assertRaises(ValueError):
                remote.relative_path(self.root, relative)


if __name__ == "__main__":
    unittest.main()
