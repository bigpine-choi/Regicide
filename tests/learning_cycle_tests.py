"""Cycle orchestration regressions, not playing-strength experiments.

Self-play, training, rule-runner and arena stages are deliberately mocked here.
State files, immutable artifact hashes, counters and cross-process locks are real.
Actual chess correctness and arena evidence have their own integration checks.
"""

import contextlib
import copy
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import learning_cycle as CYCLE
import collect_learning_data as COLLECT
import analyze_learning_losses as ANALYZE
import learning_corpus as CORPUS
import learning_arena as ARENA
import learning_registry as REGISTRY
import train_neural as TRAIN
from neural_training_tests import fixture
import chess.pgn


def save_json(path, document):
    Path(path).write_text(json.dumps(document, sort_keys=True) + "\n", encoding="utf-8")


class MockStages:
    """Mock expensive stages while retaining their saved-file API contracts."""
    def __init__(self, state, *, champion=None, arena_pass=False):
        self.state, self.champion = state, champion
        self.arena_pass = arena_pass
        self.collections, self.corpora, self.trainings, self.arenas = [], [], [], []
        self.promotions = []
        self.analysis_error = self.rules_error = self.promotion_error = None
        self.arena_status = "complete"
        self.first_attempt_offset = None

    def __enter__(self):
        self.stack = contextlib.ExitStack()
        for module, name, replacement in (
                (COLLECT, "collect", self.collect),
                (ANALYZE, "run_analysis", self.analyze),
                (CORPUS, "build_corpus", self.corpus),
                (ARENA, "run_arena", self.arena),
                (ARENA, "validate_native_model", self.native),
                (REGISTRY, "load_champion", self.load_champion),
                (REGISTRY, "promote", self.promote),
                (CYCLE, "model_version", self.model_version),
                (CYCLE, "run_rule_checks", self.rules),
                (CYCLE, "command", self.command)):
            self.stack.enter_context(mock.patch.object(module, name, side_effect=replacement))
        self.stack.enter_context(mock.patch.object(CYCLE.secrets, "randbits", side_effect=range(101, 201)))
        return self

    def __exit__(self, *arguments):
        return self.stack.__exit__(*arguments)

    def collect(self, args):
        # The statistical attempt must already be durably consumed.
        catalog = json.loads((self.state / "catalog.json").read_text())
        if self.first_attempt_offset is None:
            self.first_attempt_offset = catalog["attempts_started"] - 1
        if catalog["attempts_started"] != self.first_attempt_offset + len(self.collections) + 1:
            raise AssertionError("collection started before saving the attempt counter")
        self.collections.append(copy.copy(args))
        output = Path(args.output)
        output.mkdir()
        manifest, games, positions = fixture(output)
        (output / "datasetmanifest.json").unlink()
        pgn = []
        for game in games:
            parsed = chess.pgn.Game.from_board(TRAIN.replay(game["initial_fen"], game["history"]))
            parsed.headers["Result"] = game["result"]
            pgn.append(str(parsed))
        (output / "games.pgn").write_text("\n\n".join(pgn), encoding="utf-8")
        model = getattr(args, "model", None)
        if model is not None:
            shutil.copyfile(model, output / "selfplay-model.nn")
        manifest.update({"seed": args.seed, "errors": [], "pgn_path": "games.pgn",
                         "engine": {"selfplay_model": {"enabled": model is not None,
                             "snapshot_path": "selfplay-model.nn" if model is not None else None,
                             "sha256": CYCLE.sha256(model) if model is not None else None}},
                         "files_sha256": {name: CYCLE.sha256(output / name)
                             for name in ("games.jsonl", "positions.jsonl", "games.pgn")}})
        save_json(output / "manifest.json", manifest)
        return manifest

    def analyze(self, manifest, binary, output, **options):
        if self.analysis_error is not None:
            raise self.analysis_error
        output = Path(output)
        output.mkdir()
        (output / "positions.jsonl").write_text("", encoding="utf-8")
        report = {"status": "complete", "mock": True, "positions_path": "positions.jsonl",
                  "positions_sha256": CYCLE.sha256(output / "positions.jsonl"),
                  "source_dataset_manifest_sha256": CYCLE.sha256(manifest)}
        save_json(output / "loss-report.json", report)
        return report

    def corpus(self, datasets, output, diagnosis_paths=(), fixed_holdout=True):
        self.corpora.append({"datasets": list(map(Path, datasets)),
                             "diagnoses": list(map(Path, diagnosis_paths)),
                             "fixed_holdout": fixed_holdout})
        output = Path(output)
        output.mkdir()
        manifest = {"status": "complete", "mock": True,
                    "positions_by_split": {"train": len(datasets), "validation": 1, "test": 1},
                    "holdout_rows_sha256": {"validation": "1" * 64, "test": "2" * 64}}
        save_json(output / "manifest.json", manifest)
        return manifest

    def command(self, executable, arguments, log, **options):
        arguments = list(map(str, arguments))
        if Path(arguments[0]).name != "train_neural.py":
            raise AssertionError("only the training subprocess is mocked by this helper")
        self.trainings.append(arguments)
        output = Path(arguments[arguments.index("--output") + 1])
        output.mkdir()
        version = int(arguments[arguments.index("--model-version") + 1])
        model = output / f"small-v{version}.nn"
        model.write_text(json.dumps({"mock_version": version, "mock_candidate": len(self.trainings)}), encoding="utf-8")
        initial = (Path(arguments[arguments.index("--initial-model") + 1])
                   if "--initial-model" in arguments else None)
        save_json(output / "report.json", {"status": "complete", "mock": True,
                  "model_version": version, "model_path": model.name,
                  "target": "mock teacher target", "loss": "mock mean squared error",
                  "model_sha256": CYCLE.sha256(model),
                  "initial_model_sha256": CYCLE.sha256(initial) if initial is not None else None})
        Path(log).write_text("MOCK training stage; not a strength measurement\n", encoding="utf-8")

    def native(self, binary, candidate):
        return {"model_sha256": CYCLE.sha256(candidate), "score_cp": 0, "mock": True}

    def model_version(self, model):
        return json.loads(Path(model).read_text(encoding="utf-8"))["mock_version"]

    def rules(self, binary, candidate, output):
        if self.rules_error is not None:
            raise self.rules_error
        Path(output).mkdir()
        return {"status": "passed", "mock": True, "scope": "orchestrator test only"}

    def arena(self, binary, candidate, champion, output, **options):
        self.arenas.append({"binary": Path(binary), "candidate": Path(candidate),
                            "champion": Path(champion) if champion is not None else None, **options})
        Path(output).mkdir()
        result = {"status": self.arena_status, "mock": True,
                  "decision": {"passed": self.arena_pass, "mock": True},
                  "promotion_passed": self.arena_pass,
                  "candidate_sha256": CYCLE.sha256(candidate)}
        save_json(Path(output) / "arena.json", result)
        return result

    def load_champion(self, state, binary):
        return copy.deepcopy(self.champion)

    def promote(self, state, binary, candidate, arena, expected_champion_sha):
        self.promotions.append({"candidate": Path(candidate), "arena": arena,
                                "expected_champion_sha": expected_champion_sha})
        if self.promotion_error is not None:
            raise self.promotion_error
        generation = self.champion["generation"] + 1 if self.champion else 1
        destination = Path(state) / "models" / f"mock-generation-{generation}.nn"
        destination.parent.mkdir(exist_ok=True)
        shutil.copyfile(candidate, destination)
        self.champion = {"model_path": str(destination), "model_sha256": CYCLE.sha256(destination),
                         "generation": generation}
        save_json(Path(state) / "champion.json", self.champion)
        return copy.deepcopy(self.champion)


class CycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.state = self.root / "state"
        self.state.mkdir()
        self.binary = self.root / "regicide.exe"
        self.binary.write_bytes(b"MOCK ENGINE; NEVER EXECUTED\n")
        self.args = CYCLE.parser().parse_args(["--binary", str(self.binary), "--state", str(self.state),
                                               "--epochs", "1", "--arena-pairs", "1"])

    def catalog(self):
        return CYCLE.catalog_read(self.state)

    def run_round(self, catalog):
        with contextlib.redirect_stdout(io.StringIO()):
            return CYCLE.run_round(self.args, self.state, catalog)

    def incumbent(self, version=None):
        model = self.state / "models/incumbent.nn"
        model.parent.mkdir(exist_ok=True)
        model.write_text(json.dumps({"mock_version": version or self.args.model_version,
                                     "mock_incumbent": True}), encoding="utf-8")
        champion = {"model_path": str(model), "model_sha256": CYCLE.sha256(model), "generation": 1}
        save_json(self.state / "champion.json", champion)
        return champion

    def test_os_lock_rejects_another_process_and_releases_on_context_exception(self):
        script = ("import sys; sys.path.insert(0, sys.argv[1]); from learning_cycle import StateLock\n"
                  "try:\n with StateLock(sys.argv[2]): pass\n"
                  "except RuntimeError:\n sys.exit(37)\n")
        with CYCLE.StateLock(self.state):
            process = subprocess.run([sys.executable, "-c", script, str(ROOT / "scripts"), str(self.state)],
                                     capture_output=True, text=True, timeout=10)
            self.assertEqual(process.returncode, 37, process.stderr)
        with self.assertRaisesRegex(RuntimeError, "deliberate"):
            with CYCLE.StateLock(self.state):
                raise RuntimeError("deliberate")
        with CYCLE.StateLock(self.state):
            pass

    def test_os_lock_is_released_when_owning_process_is_terminated(self):
        script = ("import sys,time; sys.path.insert(0, sys.argv[1]); from learning_cycle import StateLock\n"
                  "with StateLock(sys.argv[2]):\n print('LOCKED', flush=True)\n time.sleep(30)\n")
        process = subprocess.Popen([sys.executable, "-c", script, str(ROOT / "scripts"), str(self.state)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), "LOCKED")
            with self.assertRaises(RuntimeError):
                with CYCLE.StateLock(self.state):
                    pass
        finally:
            process.terminate()
            process.communicate(timeout=10)
        with CYCLE.StateLock(self.state):
            pass

    def test_windows_keep_awake_restores_previous_flags_on_normal_or_exception_exit(self):
        previous = 0x80000002
        for fail in (False, True):
            with self.subTest(exception_exit=fail):
                with mock.patch.object(CYCLE.sys, "platform", "win32"):
                    with mock.patch.object(CYCLE, "windows_execution_state", return_value=previous) as setter:
                        if fail:
                            with self.assertRaisesRegex(RuntimeError, "body failed"):
                                with CYCLE.KeepAwake():
                                    raise RuntimeError("body failed")
                        else:
                            with CYCLE.KeepAwake():
                                pass
                self.assertEqual(setter.call_args_list,
                                 [mock.call(0x80000001), mock.call(previous | 0x80000000)])

    def test_keep_awake_is_noop_outside_windows(self):
        with mock.patch.object(CYCLE.sys, "platform", "linux"):
            with mock.patch.object(CYCLE, "windows_execution_state") as setter:
                with CYCLE.KeepAwake():
                    pass
        setter.assert_not_called()

    def test_keep_awake_entry_failure_propagates_without_entering_body(self):
        entered = False
        with mock.patch.object(CYCLE.sys, "platform", "win32"):
            with mock.patch.object(CYCLE, "windows_execution_state", side_effect=OSError("hold failed")) as setter:
                with self.assertRaisesRegex(OSError, "hold failed"):
                    with CYCLE.KeepAwake():
                        entered = True
        self.assertFalse(entered)
        setter.assert_called_once_with(0x80000001)

    def test_actual_bootstrap_snapshot_keeps_original_source_and_heldout_hashes(self):
        self.assertEqual(CYCLE.BOOTSTRAP, ROOT / "docs/checks/2026-10-18-neural-v1/data/manifest.json")
        before = {path.name: CYCLE.sha256(path) for path in CYCLE.BOOTSTRAP.parent.iterdir() if path.is_file()}
        catalog = self.catalog()
        reference = catalog["bootstrap"]
        self.assertEqual(reference["manifest"], "bootstrap/manifest.json")
        self.assertEqual(reference["files_sha256"]["manifest.json"], CYCLE.sha256(CYCLE.BOOTSTRAP))
        after = {path.name: CYCLE.sha256(path) for path in CYCLE.BOOTSTRAP.parent.iterdir() if path.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(reference, CYCLE.bootstrap_snapshot(self.state))

    def test_bootstrap_remains_frozen_when_original_repository_copy_changes(self):
        source = self.root / "source"
        shutil.copytree(CYCLE.BOOTSTRAP.parent, source)
        with mock.patch.object(CYCLE, "BOOTSTRAP", source / "manifest.json"):
            catalog = self.catalog()
            CYCLE.atomic_json(self.state / "catalog.json", catalog)
            (source / "games.pgn").write_text("changed repository copy", encoding="utf-8")
            frozen = CYCLE.catalog_read(self.state)
        self.assertEqual(frozen["bootstrap"], catalog["bootstrap"])

    def test_frozen_bootstrap_tampering_is_rejected(self):
        catalog = self.catalog()
        CYCLE.atomic_json(self.state / "catalog.json", catalog)
        with (self.state / "bootstrap/games.pgn").open("a", encoding="utf-8") as stream:
            stream.write("changed")
        with self.assertRaisesRegex(ValueError, "bootstrap|Bootstrap|holdout"):
            CYCLE.catalog_read(self.state)

    def test_missing_catalog_with_existing_history_cannot_reset_attempt_budget(self):
        for name in ("runs", "champion.json", "latest.json"):
            state = self.root / name.replace(".", "-")
            state.mkdir()
            if name == "runs":
                (state / name).mkdir()
            else:
                (state / name).write_text("{}", encoding="utf-8")
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "Missing learning catalog"):
                CYCLE.catalog_read(state)

    def test_counter_run_length_or_identity_mismatch_is_rejected(self):
        original = self.catalog()
        cases = [dict(original, attempts_started=1),
                 dict(original, attempts_started=True),
                 dict(original, attempts_started=-1),
                 dict(original, attempts_started=2, runs=["runs/000001-repeat", "runs/000001-repeat"]),
                 dict(original, attempts_started=1, runs=["runs/000002-out-of-order"])]
        for case in cases:
            CYCLE.atomic_json(self.state / "catalog.json", case)
            with self.subTest(case=case["attempts_started"]), self.assertRaises(ValueError):
                CYCLE.catalog_read(self.state)

    def test_two_rejected_rounds_accumulate_data_and_change_indices_and_seeds(self):
        catalog = self.catalog()
        with MockStages(self.state) as stages:
            first = self.run_round(catalog)
            second = self.run_round(CYCLE.catalog_read(self.state))
        self.assertFalse(first["adopted"])
        self.assertFalse(second["adopted"])
        self.assertEqual([first["attempt_index"], second["attempt_index"]], [1, 2])
        self.assertNotEqual(first["seed"], second["seed"])
        self.assertNotEqual(first["arena_seed"], second["arena_seed"])
        self.assertEqual([len(item["datasets"]) for item in stages.corpora], [2, 3])
        self.assertEqual([len(item["diagnoses"]) for item in stages.corpora], [1, 2])
        self.assertTrue(all(item["fixed_holdout"] for item in stages.corpora))
        self.assertTrue(all(item["datasets"][0] == self.state / "bootstrap/manifest.json" for item in stages.corpora))
        self.assertFalse(any("arena" in str(path) for item in stages.corpora for path in item["datasets"]))
        self.assertEqual(stages.promotions, [])
        self.assertFalse((self.state / "champion.json").exists())

    def test_rejected_candidates_never_replace_or_warm_start_from_another_rejected_model(self):
        catalog = self.catalog()
        champion = self.incumbent()
        original_bytes = (self.state / "champion.json").read_bytes()
        with MockStages(self.state, champion=champion) as stages:
            self.run_round(catalog)
            self.run_round(CYCLE.catalog_read(self.state))
        self.assertEqual((self.state / "champion.json").read_bytes(), original_bytes)
        for arguments, collection, arena in zip(stages.trainings, stages.collections, stages.arenas):
            self.assertEqual(arguments[arguments.index("--initial-model") + 1], champion["model_path"])
            self.assertEqual(collection.model, Path(champion["model_path"]))
            self.assertEqual(arena["champion"], Path(champion["model_path"]))

    def test_classical_parent_uses_random_initialization_not_the_weak_shipped_model(self):
        catalog = self.catalog()
        with MockStages(self.state) as stages:
            self.run_round(catalog)
        self.assertNotIn("--initial-model", stages.trainings[0])
        self.assertIsNone(stages.collections[0].model)
        self.assertIsNone(stages.arenas[0]["champion"])

    def test_default_v2_and_explicit_legacy_version_are_validated(self):
        self.assertEqual(CYCLE.parser().parse_args([]).model_version, 2)
        self.assertEqual(CYCLE.parser().parse_args(["--model-version", "1"]).model_version, 1)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            CYCLE.parser().parse_args(["--model-version", "3"])

    def test_default_v2_training_records_bounded_classical_residual(self):
        with MockStages(self.state) as stages:
            report = self.run_round(self.catalog())
        arguments = stages.trainings[0]
        self.assertEqual(arguments[arguments.index("--model-version") + 1], "2")
        self.assertEqual(report["evaluation_design"]["model_version"], 2)
        self.assertEqual(report["evaluation_design"]["residual_bound_cp"], 100)
        self.assertEqual(report["evaluation_design"]["default_l2"], .0001)
        self.assertEqual(report["training"]["model_path"], "small-v2.nn")
        self.assertEqual(report["initialization"]["mode"], "seeded_random")

    def test_legacy_incumbent_is_kept_as_opponent_but_not_warm_start_for_v2(self):
        catalog = self.catalog()
        champion = self.incumbent(version=1)
        before = (self.state / "champion.json").read_bytes()
        with MockStages(self.state, champion=champion) as stages:
            report = self.run_round(catalog)
        self.assertNotIn("--initial-model", stages.trainings[0])
        self.assertEqual(stages.collections[0].model, Path(champion["model_path"]))
        self.assertEqual(stages.arenas[0]["champion"], Path(champion["model_path"]))
        self.assertEqual(report["initialization"]["incumbent_model_version"], 1)
        self.assertEqual(report["initialization"]["mode"], "seeded_random")
        self.assertIn("incompatible_model_format", report["initialization"]["reason"])
        self.assertEqual((self.state / "champion.json").read_bytes(), before)

    def test_explicit_legacy_v1_can_still_warm_start_legacy_incumbent(self):
        catalog = self.catalog()
        self.args.model_version = 1
        champion = self.incumbent(version=1)
        with MockStages(self.state, champion=champion) as stages:
            report = self.run_round(catalog)
        arguments = stages.trainings[0]
        self.assertEqual(arguments[arguments.index("--model-version") + 1], "1")
        self.assertEqual(arguments[arguments.index("--initial-model") + 1], champion["model_path"])
        self.assertEqual(report["initialization"]["mode"], "warm_start")
        self.assertEqual(report["evaluation_design"]["format"], "REGICIDE_NN_V1 768 32 16 600")
        self.assertEqual(report["training"]["model_path"], "small-v1.nn")

    def test_version_mismatch_in_training_report_blocks_arena_and_preserves_champion(self):
        catalog = self.catalog()
        champion = self.incumbent(version=2)
        before = (self.state / "champion.json").read_bytes()
        with MockStages(self.state, champion=champion, arena_pass=True) as stages:
            def wrong_report(*arguments, **options):
                stages.command(*arguments, **options)
                output = Path(arguments[1][arguments[1].index("--output") + 1])
                path = output / "report.json"
                training = json.loads(path.read_text(encoding="utf-8"))
                training["model_version"] = 1
                save_json(path, training)
            with mock.patch.object(CYCLE, "command", side_effect=wrong_report):
                with self.assertRaisesRegex(ValueError, "identities disagree"):
                    self.run_round(catalog)
        self.assertEqual(stages.arenas, [])
        self.assertEqual(stages.promotions, [])
        self.assertEqual((self.state / "champion.json").read_bytes(), before)

    def test_candidate_path_from_training_report_cannot_escape_model_directory(self):
        with MockStages(self.state) as stages:
            def escaped_report(*arguments, **options):
                stages.command(*arguments, **options)
                output = Path(arguments[1][arguments[1].index("--output") + 1])
                path = output / "report.json"
                training = json.loads(path.read_text(encoding="utf-8"))
                training["model_path"] = "../../outside-model.nn"
                save_json(path, training)
            with mock.patch.object(CYCLE, "command", side_effect=escaped_report):
                with self.assertRaisesRegex(ValueError, "escapes"):
                    self.run_round(self.catalog())
        self.assertEqual(stages.arenas, [])
        self.assertEqual(stages.promotions, [])

    def test_existing_two_attempts_resume_as_three_after_model_version_change(self):
        self.args.model_version = 1
        with MockStages(self.state):
            self.run_round(self.catalog())
            self.run_round(CYCLE.catalog_read(self.state))
        before = CYCLE.catalog_read(self.state)
        self.args.model_version = 2
        with MockStages(self.state) as stages:
            third = self.run_round(CYCLE.catalog_read(self.state))
        after = CYCLE.catalog_read(self.state)
        self.assertEqual(third["attempt_index"], 3)
        self.assertEqual(stages.arenas[0]["attempt_index"], 3)
        self.assertEqual(after["attempts_started"], 3)
        self.assertEqual(after["runs"][:2], before["runs"])
        self.assertEqual(after["bootstrap"], before["bootstrap"])
        self.assertEqual(len(after["datasets"]), 3)

    def test_source_freeze_includes_classical_helper_and_all_python_regressions(self):
        hashes = CYCLE.implementation_hashes()
        for relative in ("scripts/classical_evaluation.py", "tests/learning_cycle_tests.py",
                         "tests/neural_training_tests.py", "tests/learning_runtime_tests.py"):
            self.assertIn(relative, hashes)
            self.assertEqual(hashes[relative], CYCLE.sha256(ROOT / relative))

    def test_actual_model_formats_are_loaded_without_reinterpreting_weights(self):
        from neural_model import export_model, initialize_model
        for version in (1, 2):
            path = self.root / f"real-v{version}.nn"
            export_model(initialize_model(seed=97, version=version), path)
            self.assertEqual(CYCLE.model_version(path), version)

    def test_actual_candidate_format_mismatch_blocks_even_with_matching_file_hash(self):
        with MockStages(self.state) as stages:
            def wrong_candidate(*arguments, **options):
                stages.command(*arguments, **options)
                output = Path(arguments[1][arguments[1].index("--output") + 1])
                path = output / "report.json"
                training = json.loads(path.read_text(encoding="utf-8"))
                candidate = output / training["model_path"]
                candidate.write_text(json.dumps({"mock_version": 1}), encoding="utf-8")
                training["model_sha256"] = CYCLE.sha256(candidate)
                save_json(path, training)
            with mock.patch.object(CYCLE, "command", side_effect=wrong_candidate):
                with self.assertRaisesRegex(ValueError, "identities disagree"):
                    self.run_round(self.catalog())
        self.assertEqual(stages.arenas, [])
        self.assertEqual(stages.promotions, [])

    def test_failed_analysis_preserves_completed_data_and_spent_attempt(self):
        catalog = self.catalog()
        with MockStages(self.state) as stages:
            stages.analysis_error = RuntimeError("mock analysis failure")
            with self.assertRaisesRegex(RuntimeError, "analysis failure"):
                self.run_round(catalog)
        saved = CYCLE.catalog_read(self.state)
        self.assertEqual(saved["attempts_started"], 1)
        self.assertEqual(len(saved["datasets"]), 1)
        self.assertNotIn("diagnosis", saved["datasets"][0])
        self.assertTrue(CYCLE.contained(self.state, saved["datasets"][0]["manifest"]).is_file())
        self.assertEqual(json.loads((self.state / "latest.json").read_text())["status"], "failed")
        self.assertEqual(stages.trainings, [])

    def test_next_round_reuses_data_from_a_failed_analysis_without_reusing_attempt(self):
        catalog = self.catalog()
        with MockStages(self.state) as stages:
            stages.analysis_error = RuntimeError("first analysis interrupted")
            with self.assertRaises(RuntimeError):
                self.run_round(catalog)
            stages.analysis_error = None
            second = self.run_round(CYCLE.catalog_read(self.state))
        self.assertEqual(second["attempt_index"], 2)
        self.assertEqual(len(stages.corpora[0]["datasets"]), 3)
        self.assertEqual(len(stages.corpora[0]["diagnoses"]), 1)
        self.assertEqual(CYCLE.catalog_read(self.state)["attempts_started"], 2)

    def test_rule_failure_prevents_arena_and_promotion_and_keeps_champion(self):
        catalog = self.catalog()
        champion = self.incumbent()
        original = (self.state / "champion.json").read_bytes()
        with MockStages(self.state, champion=champion, arena_pass=True) as stages:
            stages.rules_error = RuntimeError("mock rule regression")
            with self.assertRaisesRegex(RuntimeError, "rule regression"):
                self.run_round(catalog)
        self.assertEqual(stages.arenas, [])
        self.assertEqual(stages.promotions, [])
        self.assertEqual((self.state / "champion.json").read_bytes(), original)

    def test_failed_arena_never_calls_promotion_even_with_a_spurious_pass_flag(self):
        catalog = self.catalog()
        champion = self.incumbent()
        original = (self.state / "champion.json").read_bytes()
        with MockStages(self.state, champion=champion, arena_pass=True) as stages:
            stages.arena_status = "failed"
            with self.assertRaisesRegex(ValueError, "Arena invalid"):
                self.run_round(catalog)
        self.assertEqual(stages.promotions, [])
        self.assertEqual((self.state / "champion.json").read_bytes(), original)

    def test_mock_passing_arena_calls_registry_and_next_round_uses_new_champion(self):
        catalog = self.catalog()
        champion = self.incumbent()
        with MockStages(self.state, champion=champion, arena_pass=True) as stages:
            first = self.run_round(catalog)
            adopted = copy.deepcopy(stages.champion)
            stages.arena_pass = False
            second = self.run_round(CYCLE.catalog_read(self.state))
        self.assertTrue(first["adopted"])
        self.assertFalse(second["adopted"])
        self.assertEqual(len(stages.promotions), 1)
        self.assertEqual(stages.promotions[0]["expected_champion_sha"], champion["model_sha256"])
        self.assertEqual(stages.collections[1].model, Path(adopted["model_path"]))
        self.assertEqual(stages.trainings[1][stages.trainings[1].index("--initial-model") + 1], adopted["model_path"])
        self.assertEqual(second["selected_model_sha256"], adopted["model_sha256"])

    def test_registry_error_keeps_incumbent_and_records_failed_cycle(self):
        catalog = self.catalog()
        champion = self.incumbent()
        original = (self.state / "champion.json").read_bytes()
        with MockStages(self.state, champion=champion, arena_pass=True) as stages:
            stages.promotion_error = ValueError("mock evidence rejected")
            with self.assertRaisesRegex(ValueError, "evidence rejected"):
                self.run_round(catalog)
        self.assertEqual((self.state / "champion.json").read_bytes(), original)
        latest = json.loads((self.state / "latest.json").read_text())
        self.assertEqual(latest["status"], "failed")
        self.assertFalse(latest["adopted"])

    def test_code_changes_before_or_during_arena_prevent_adoption(self):
        for during_arena in (False, True):
            state = self.root / ("during" if during_arena else "before")
            state.mkdir()
            original_state = self.state
            self.state = state
            try:
                catalog = self.catalog()
                champion = self.incumbent()
                original = (state / "champion.json").read_bytes()
                snapshots = [{"code": "same"}, {"code": "same"}, {"code": "changed"}] if during_arena else [
                    {"code": "same"}, {"code": "changed"}]
                with MockStages(state, champion=champion, arena_pass=True) as stages:
                    with mock.patch.object(CYCLE, "implementation_hashes", side_effect=snapshots):
                        with self.assertRaisesRegex(ValueError, "implementation changed"):
                            self.run_round(catalog)
                self.assertEqual(len(stages.arenas), 1 if during_arena else 0)
                self.assertEqual(stages.promotions, [])
                self.assertEqual((state / "champion.json").read_bytes(), original)
            finally:
                self.state = original_state

    def test_installed_binary_change_during_arena_prevents_promotion(self):
        catalog = self.catalog()
        champion = self.incumbent()
        original = (self.state / "champion.json").read_bytes()
        with MockStages(self.state, champion=champion, arena_pass=True) as stages:
            def changed_binary(*arguments, **keywords):
                result = stages.arena(*arguments, **keywords)
                self.binary.write_bytes(b"CHANGED MOCK ENGINE")
                return result
            with mock.patch.object(ARENA, "run_arena", side_effect=changed_binary):
                with self.assertRaisesRegex(ValueError, "Installed engine changed"):
                    self.run_round(catalog)
        self.assertEqual(stages.promotions, [])
        self.assertEqual((self.state / "champion.json").read_bytes(), original)

    def test_candidate_change_before_arena_blocks_match_and_preserves_incumbent(self):
        catalog = self.catalog()
        champion = self.incumbent()
        original = (self.state / "champion.json").read_bytes()
        with MockStages(self.state, champion=champion, arena_pass=True) as stages:
            def changed_candidate(binary, candidate, output):
                result = stages.rules(binary, candidate, output)
                Path(candidate).write_bytes(b"CHANGED CANDIDATE BEFORE ARENA")
                return result
            with mock.patch.object(CYCLE, "run_rule_checks", side_effect=changed_candidate):
                with self.assertRaisesRegex(ValueError, "Candidate or frozen engine changed before the arena"):
                    self.run_round(catalog)
        self.assertEqual(stages.arenas, [])
        self.assertEqual(stages.promotions, [])
        self.assertEqual((self.state / "champion.json").read_bytes(), original)

    def test_candidate_change_during_arena_blocks_adoption_and_preserves_incumbent(self):
        catalog = self.catalog()
        champion = self.incumbent()
        original = (self.state / "champion.json").read_bytes()
        with MockStages(self.state, champion=champion, arena_pass=True) as stages:
            def changed_candidate(binary, candidate, champion, output, **options):
                result = stages.arena(binary, candidate, champion, output, **options)
                Path(candidate).write_bytes(b"CHANGED CANDIDATE DURING ARENA")
                return result
            with mock.patch.object(ARENA, "run_arena", side_effect=changed_candidate):
                with self.assertRaisesRegex(ValueError, "Candidate or frozen engine changed during the arena"):
                    self.run_round(catalog)
        self.assertEqual(len(stages.arenas), 1)
        self.assertEqual(stages.promotions, [])
        self.assertEqual((self.state / "champion.json").read_bytes(), original)

    def test_accumulated_pgn_or_diagnosis_tampering_is_rejected(self):
        catalog = self.catalog()
        with MockStages(self.state):
            self.run_round(catalog)
        saved = CYCLE.catalog_read(self.state)
        reference = saved["datasets"][0]
        pgn = CYCLE.contained(self.state, reference["manifest"]).parent / "games.pgn"
        original = pgn.read_bytes()
        pgn.write_bytes(original + b"tampered")
        with self.assertRaisesRegex(ValueError, "accumulated dataset"):
            CYCLE.catalog_read(self.state)
        pgn.write_bytes(original)
        diagnosis = CYCLE.contained(self.state, reference["diagnosis"]["path"]) / "positions.jsonl"
        diagnosis.write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "loss analysis"):
            CYCLE.catalog_read(self.state)

    def test_accumulated_selfplay_champion_snapshot_tampering_is_rejected(self):
        catalog = self.catalog()
        champion = self.incumbent()
        with MockStages(self.state, champion=champion):
            self.run_round(catalog)
        saved = CYCLE.catalog_read(self.state)
        snapshot = CYCLE.contained(self.state, saved["datasets"][0]["manifest"]).parent / "selfplay-model.nn"
        snapshot.write_bytes(b"changed self-play model")
        with self.assertRaisesRegex(ValueError, "self-play model snapshot|Self-play model snapshot"):
            CYCLE.catalog_read(self.state)

    def test_state_artifact_paths_cannot_escape_or_be_absolute(self):
        for value in ("../outside", str(self.root / "absolute"), "", None):
            with self.subTest(path=value), self.assertRaises(ValueError):
                CYCLE.contained(self.state, value)

    def test_main_creates_state_and_uses_exclusive_context(self):
        state = self.root / "new-state"
        with mock.patch.object(CYCLE, "windows_execution_state", return_value=0x80000000):
            with mock.patch.object(CYCLE, "run_round", return_value={}) as run:
                self.assertEqual(CYCLE.main(["--binary", str(self.binary), "--state", str(state)]), 0)
        self.assertTrue((state / "cycle.lock").is_file())
        self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
