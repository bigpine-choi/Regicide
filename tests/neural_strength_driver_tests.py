"""Small input-binding tests; no engine is launched and no match is played."""

import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import run_neural_strength_comparison as driver


COMMON = {"Hash": 16, "Move Overhead": 20, "UsePositionalEvaluation": True,
          "UseQuiescence": True, "UseTranspositionTable": True, "UseFastMoves": True,
          "UseQuietMoveOrdering": True, "UseQuiescenceChecks": True, "PVS": True,
          "UseSEE": True, "UseIncrementalNeuralEvaluation": True}


class DriverTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        baseline, candidate = self.root / "baseline.exe", self.root / "candidate.exe"
        baseline.write_bytes(b"byte-identical engine copies")
        candidate.write_bytes(baseline.read_bytes())
        model, openings, validation = (self.root / name for name in
                                      ("model.nnue", "openings.json", "validation.json"))
        model.write_bytes(b"test model, never executed")
        openings.write_text('{"openings": []}', encoding="utf-8")
        validation.write_text(json.dumps({"all_passed": True, "completed_utc": driver.utc_now()}),
                              encoding="utf-8")
        self.freeze = {"schema_version": 1,
                       "engine_source_commit": driver.comparison.git_command("rev-parse", "HEAD"),
                       "files": {name: driver.digest(ROOT / name) for name in driver.CRITICAL_SOURCES},
                       "common_effective_options": COMMON.copy(), "time_controls_ms": [100, 1000],
                       "max_plies": 500, "draw_policy": "claim"}
        for role, path, key in (("baseline", baseline, "binary"), ("candidate", candidate, "binary"),
                                ("model", model, "path"), ("openings", openings, "path"),
                                ("prior_validation", validation, "path")):
            self.freeze[role] = {key: str(path), "sha256": driver.digest(path)}
        self.freeze["model"]["checksum"] = 123
        self.path = self.root / "freeze.json"
        self.save_freeze()

    def tearDown(self):
        self.directory.cleanup()

    def save_freeze(self):
        self.path.write_text(json.dumps(self.freeze), encoding="utf-8")
        self.expected = driver.digest(self.path)

    def test_valid_bindings_and_distinct_identical_binary_paths(self):
        driver.validate_freeze(self.freeze, 100)
        evidence = driver.assert_frozen(self.path, self.freeze, self.expected)
        self.assertEqual(evidence["engine_source_commit"], self.freeze["engine_source_commit"])
        self.assertEqual(len(evidence["files"]), len(driver.CRITICAL_SOURCES) + 5)

    def test_changed_model_and_freeze_rejected(self):
        model = Path(self.freeze["model"]["path"])
        model.write_bytes(model.read_bytes() + b"changed")
        with self.assertRaisesRegex(ValueError, "Frozen input changed"):
            driver.assert_frozen(self.path, self.freeze, self.expected)
        self.path.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "freeze file changed"):
            driver.assert_frozen(self.path, self.freeze, self.expected)

    def test_wrong_head_failed_validation_and_same_path_rejected(self):
        with patch.object(driver.comparison, "git_command", return_value="f" * 40):
            with self.assertRaisesRegex(ValueError, "HEAD differs"):
                driver.assert_frozen(self.path, self.freeze, self.expected)
        self.freeze["candidate"]["binary"] = self.freeze["baseline"]["binary"]
        with self.assertRaisesRegex(ValueError, "distinct binary copy paths"):
            driver.validate_freeze(self.freeze, 100)
        validation = Path(self.freeze["prior_validation"]["path"])
        validation.write_text(json.dumps({"all_passed": False, "completed_utc": driver.utc_now()}),
                              encoding="utf-8")
        self.freeze["prior_validation"]["sha256"] = driver.digest(validation)
        self.save_freeze()
        with self.assertRaisesRegex(ValueError, "validation gate did not pass"):
            driver.assert_frozen(self.path, self.freeze, self.expected)

    def engines(self):
        defaults = {**COMMON, "UseNNUEEvaluation": False, "NeuralModelFile": "<builtin>"}
        return {role: SimpleNamespace(id={"name": "Regicide test"}, options={
            name: SimpleNamespace(default=value, type="check" if type(value) is bool else "string")
            for name, value in defaults.items()}) for role in ("baseline", "candidate")}

    def test_option_symmetry_and_intended_neural_difference(self):
        requested = {"UseNNUEEvaluation": True, "UseIncrementalNeuralEvaluation": True,
                     "NeuralModelFile": self.freeze["model"]["path"]}
        evidence = driver.check_options(self.engines(), self.freeze, requested)
        baseline, candidate = (evidence["effective_options"][role] for role in ("baseline", "candidate"))
        self.assertFalse(baseline["UseNNUEEvaluation"])
        self.assertTrue(candidate["UseNNUEEvaluation"])
        self.assertEqual(set(name for name in baseline if baseline[name] != candidate[name]),
                         {"UseNNUEEvaluation", "NeuralModelFile"})

    def test_wrong_engine_default_rejected(self):
        engines = self.engines()
        engines["candidate"].options["Hash"].default = 32
        with self.assertRaisesRegex(ValueError, "Unexpected candidate advertised default: Hash"):
            driver.check_options(engines, self.freeze, {})

    def test_preflight_failure_preserved_without_engine_launch_or_overwrite(self):
        output = self.root / "failed-block"
        with patch.object(driver, "assert_frozen", side_effect=ValueError("changed input")), \
                patch.object(driver.Awake, "start") as wake, \
                patch.object(driver.guarded, "execute_guarded_match") as launch:
            self.assertEqual(driver.run_block(self.path, 100, output), 2)
        wake.assert_not_called()
        launch.assert_not_called()
        state = driver.read_json(output / "driver-state.json")
        self.assertFalse(state["valid_for_comparison"])
        self.assertEqual(state["error"], "ValueError: changed input")
        with self.assertRaisesRegex(ValueError, "Preserve the previous output"):
            driver.run_block(self.path, 100, output)


if __name__ == "__main__":
    unittest.main()
