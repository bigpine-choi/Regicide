"""Reuse only immutable completed data from failed pre-fitting study attempts."""

import copy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import chess.pgn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import collect_learning_data as DATA
import collect_endgame_data as ENDGAME
import endgame_study as STUDY
import teacher_quality as QUALITY
import refine_endgame_holdout as HOLDOUT
from learning_corpus import _source
from neural_training_tests import fixture


class DataReuseTests(unittest.TestCase):
    def write_dataset(self, directory, games, rows, extra):
        directory.mkdir()
        DATA.write_jsonl(directory / "games.jsonl", games)
        DATA.write_jsonl(directory / "positions.jsonl", rows)
        pgns = []
        for game in games:
            parsed = chess.pgn.Game.from_board(DATA.replay_position(game["initial_fen"], game["history"]))
            parsed.headers["Result"] = game["result"]
            pgns.append(str(parsed))
        (directory / "games.pgn").write_text("\n\n".join(pgns) + "\n", encoding="utf-8", newline="\n")
        manifest = {"schema": DATA.SCHEMA, "status": "complete", "errors": [],
                    "games_path": "games.jsonl", "positions_path": "positions.jsonl", "pgn_path": "games.pgn",
                    "files_sha256": {name: DATA.sha256_file(directory / name)
                                     for name in ("games.jsonl", "positions.jsonl", "games.pgn")}, **extra}
        DATA.checkpoint_json(directory / "manifest.json", manifest)
        return _source(directory / "manifest.json", allow_empty_splits=True)

    def create_quality(self, origin, raw, name, binary, heldout):
        policy = QUALITY._configuration((10000, 50000, 200000), 30, 3)
        if heldout:
            policy["id"] = HOLDOUT.SCHEMA
        records, rows = [], []
        for index, original in enumerate(raw["positions"]):
            stages = []
            for budget in (10000, 50000, 200000):
                raw_info = (f"info depth 4 nodes {budget} score cp {original['teacher_cp_stm']} pv " +
                            " ".join(original["teacher_pv"]))
                label = {"teacher_cp_stm": original["teacher_cp_stm"], "teacher_depth": 4,
                         "teacher_nodes": budget, "teacher_pv": original["teacher_pv"],
                         "teacher_score_raw_info": raw_info}
                stages.append({"requested_nodes": budget, "best_move": original["teacher_pv"][0],
                               "cp_stm": original["teacher_cp_stm"], "completed_depth": 4,
                               "reported_nodes": budget, "pv": original["teacher_pv"],
                               "label": label, "exclusion": None})
            records.append({"source_row_index": index, "history": original["history"], "fen": original["fen"],
                            "stages": stages, "accepted": True, "exclusion": None})
            rows.append({**original, **stages[-1]["label"]})
        _, frozen = QUALITY._freeze_paths(raw, binary)
        if heldout:
            frozen.update({"initial_holdout_wrapper": DATA.sha256_file(HOLDOUT.__file__),
                           "endgame_collector": DATA.sha256_file(ENDGAME.__file__),
                           "endgame_seed_definition": DATA.sha256_file(ENDGAME.DEFAULT_SEEDS)})
        report = {"schema": HOLDOUT.SCHEMA if heldout else QUALITY.SCHEMA, "status": "complete", "errors": [],
                  "source_manifest_sha256": raw["sha256"], "source_files_sha256": raw["files_sha256"],
                  "policy": policy, "policy_sha256": QUALITY._digest(policy),
                  "frozen_inputs_sha256": frozen, "records": records, "queries_completed": 3 * len(records)}
        report_name = "holdout-quality-report.json" if heldout else "quality-report.json"
        extra = {"positions_by_split": {split: sum(row["split"] == split for row in rows) for split in DATA.SPLITS}}
        if heldout:
            extra["endgame_holdout_refinement"] = {"schema": HOLDOUT.SCHEMA, "source_manifest_sha256": raw["sha256"],
                                                  "policy_sha256": report["policy_sha256"]}
        else:
            extra.update({"teacher_quality_schema": QUALITY.SCHEMA, "source_manifest_sha256": raw["sha256"],
                          "source_files_sha256": raw["files_sha256"], "policy_sha256": report["policy_sha256"]})
        result = self.write_dataset(origin / name, raw["games"], rows, extra)
        DATA.checkpoint_json(origin / name / report_name, report)
        if not heldout:
            manifest = result["manifest"]
            manifest["quality_report_sha256"] = DATA.sha256_file(origin / name / report_name)
            DATA.checkpoint_json(result["path"], manifest)

    def setup_fixture(self, temporary):
        root = Path(temporary).resolve()
        state = root / "state"
        source_relative, target_relative = "runs/000005-source", "runs/000006-target"
        origin, destination = state / source_relative, state / target_relative
        origin.mkdir(parents=True)
        destination.mkdir()
        binary = origin / ("regicide.exe" if sys.platform == "win32" else "regicide")
        binary.write_bytes(b"mock binary; never executed")
        (destination / binary.name).write_bytes(binary.read_bytes())
        digest = DATA.sha256_file(binary)
        configuration = {"seed": 117, "train_repeats": 4, "teacher_quality_nodes": [10000, 50000, 200000],
                         "quality_cp_delta": 30, "quality_min_depth": 3}
        fixture_dir = root / "fixture"
        fixture_dir.mkdir()
        _, original_games, original_rows = fixture(fixture_dir)
        catalog = {"attempts_started": 6, "runs": [f"runs/{index:06d}-old" for index in range(1, 5)] +
                   [source_relative, target_relative], "datasets": [], "endgame_holdouts": []}
        for name, split, seed, indices, repeats in (
                ("endgame-train", "train", 118, [0], 128),
                ("endgame-heldout", "heldout", 117, [1, 2], 8)):
            games, rows = [], []
            for index in indices:
                for repeat in range(repeats):
                    game, row = copy.deepcopy(original_games[index]), copy.deepcopy(original_rows[index])
                    identifier = f"{name}-{index}-{repeat}"
                    game["game_id"] = row["game_id"] = identifier
                    row["teacher_nodes"] = 10000
                    games.append(game)
                    rows.append(row)
            seeds, openings = ENDGAME.load_seeds(ENDGAME.DEFAULT_SEEDS, split)
            sidecar = origin / (name + ".endgame-openings.json")
            DATA.checkpoint_json(sidecar, openings)
            raw = self.write_dataset(origin / name, games, rows, {
                "games_requested": len(games), "seed": seed,
                "engine": {"sha256": digest, "configured_options": DATA.OPTIONS, "selfplay_model": {"enabled": False}},
                "openings": {"sha256": DATA.sha256_file(sidecar)},
                "limits": {"selfplay_nodes": 1000, "teacher_nodes": 10000, "max_plies_total": 384,
                           "sample_every": 2, "random_opening_plies": 2, "time_limit": None},
                "endgame": {"schema": "regicide.endgame.collection.v1", "selected_split": split,
                            "seed_file_sha256": DATA.sha256_file(ENDGAME.DEFAULT_SEEDS), "seed_definition": seeds}})
            key = "datasets" if split == "train" else "endgame_holdouts"
            catalog[key].append(STUDY.CYCLE.dataset_reference(state, raw["path"]))
            self.create_quality(origin, raw, name + "-quality", binary, split == "heldout")
        _, starts = ENDGAME.load_seeds(ENDGAME.DEFAULT_SEEDS, "heldout")
        DATA.checkpoint_json(origin / "development-starts.json", {"schema": "regicide.endgame.development.openings.v1",
            "positions": [{"name": item["id"], "fen": item["initial_fen"], "moves": item["moves"]}
                          for item in starts["openings"]]})
        (origin / "diagnosis").mkdir()
        DATA.checkpoint_json(origin / "diagnosis/report.json", {"status": "complete"})
        (origin / "augmentation.partial").mkdir()
        (origin / "augmentation.partial/sentinel").write_bytes(b"preserve failed boundary evidence")
        report = {"schema": STUDY.CYCLE.SCHEMA, "study_schema": "regicide.endgame.study.v1", "policy": STUDY.POLICY,
                  "status": "failed", "attempt_index": 5, "binary_sha256": digest, "configuration": configuration,
                  "stages": [{"name": "immutable-augmentation"}], "diagnosis": {"summary": "fixture"}}
        DATA.checkpoint_json(origin / "cycle.json", report)
        return state, catalog, source_relative, destination, digest, configuration, origin

    def call(self, values):
        return STUDY.reuse_completed_data(*values[:6])

    def test_valid_reuse_preserves_failed_run_and_catalog_and_copies_all_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            values = self.setup_fixture(temporary)
            state, catalog, _, destination, _, _, origin = values
            self.assertEqual(state, state.resolve())
            self.assertEqual(destination, destination.resolve())
            self.assertEqual(origin, origin.resolve())
            before_catalog = copy.deepcopy(catalog)
            before = {str(path): path.read_bytes() for path in origin.rglob("*") if path.is_file()}
            result = self.call(values)
            self.assertEqual(result["source_attempt_index"], 5)
            self.assertEqual(result["diagnosis_summary"], {"summary": "fixture"})
            self.assertEqual(catalog, before_catalog)
            self.assertFalse((destination / "augmentation.partial").exists())
            for path, raw in before.items():
                self.assertEqual(Path(path).read_bytes(), raw)
            for relative, digest in result["files_sha256"].items():
                self.assertEqual(DATA.sha256_file(destination / relative), digest)
                self.assertEqual(DATA.sha256_file(origin / relative), digest)

    def test_refuses_fitting_started_candidate_nonfailed_or_current_attempt(self):
        for mutation in ("candidate", "curve", "stage", "status", "attempt"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                values = self.setup_fixture(temporary)
                origin = values[-1]
                report = json.loads((origin / "cycle.json").read_text())
                if mutation == "candidate":
                    report["candidate"] = None
                elif mutation == "curve":
                    (origin / "curve").mkdir()
                elif mutation == "stage":
                    report["stages"].append({"name": "nested-data-learning-curve"})
                elif mutation == "status":
                    report["status"] = "ready_for_validation"
                else:
                    report["attempt_index"] = 6
                DATA.checkpoint_json(origin / "cycle.json", report)
                with self.assertRaisesRegex(ValueError, "before any learning-curve"):
                    self.call(values)

    def test_refuses_wrong_engine_configuration_registration_or_nonempty_destination(self):
        for mutation in ("binary", "configuration", "registration", "duplicate", "destination"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                values = list(self.setup_fixture(temporary))
                if mutation == "binary":
                    values[4] = "0" * 64
                elif mutation == "configuration":
                    values[5] = {**values[5], "quality_cp_delta": 100}
                elif mutation == "registration":
                    values[1]["datasets"] = []
                elif mutation == "duplicate":
                    values[1]["datasets"] *= 2
                else:
                    (values[3] / "unrelated-model.nn").write_bytes(b"do not overwrite")
                with self.assertRaises(ValueError):
                    self.call(values)

    def test_refuses_quality_policy_hash_or_frozen_input_mutation(self):
        for mutation in ("policy", "hash", "frozen", "accepted"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                values = self.setup_fixture(temporary)
                directory = values[-1] / "endgame-train-quality"
                path = directory / "quality-report.json"
                report = json.loads(path.read_text())
                if mutation == "policy":
                    report["policy"]["max_last_two_cp_delta"] = 100
                    report["policy_sha256"] = QUALITY._digest(report["policy"])
                elif mutation == "frozen":
                    report["frozen_inputs_sha256"]["reserved_openings"] = "0" * 64
                elif mutation == "accepted":
                    report["records"][0]["accepted"] = False
                else:
                    report["queries_completed"] = 0
                DATA.checkpoint_json(path, report)
                if mutation != "hash":
                    manifest_path = directory / "manifest.json"
                    manifest = json.loads(manifest_path.read_text())
                    manifest["quality_report_sha256"] = DATA.sha256_file(path)
                    DATA.checkpoint_json(manifest_path, manifest)
                with self.assertRaises(ValueError):
                    self.call(values)

    def test_prepare_reuse_consumes_new_attempt_without_recollecting_or_reregistering(self):
        with tempfile.TemporaryDirectory() as temporary:
            state, catalog, source_relative, _, _, configuration, origin = self.setup_fixture(temporary)
            catalog["runs"].pop()
            catalog["attempts_started"] = 5
            references = copy.deepcopy((catalog["datasets"], catalog["endgame_holdouts"]))
            args = SimpleNamespace(binary=origin / ("regicide.exe" if sys.platform == "win32" else "regicide"),
                                   baseline=Path(temporary).resolve() / "fixture/datasetmanifest.json",
                                   seed=configuration["seed"], train_repeats=4,
                                   reuse_data_run=source_relative, remote_validation=True)

            def curve(baseline, additions, output):
                output.mkdir()
                relative = "added-001024/seed-2026101901/small-v2.nn"
                model = output / relative
                model.parent.mkdir(parents=True)
                model.write_bytes(b"mock candidate; no model training executed")
                return {"candidate": {"added_train": 1024, "seed": 2026101901, "model_path": relative,
                                      "model_sha256": DATA.sha256_file(model), "selected_epoch": 1}}

            with patch("learning_registry.load_champion", return_value=None), \
                    patch("diagnose_endgames.run_diagnosis") as diagnosis, \
                    patch("collect_endgame_data.collect") as collect, \
                    patch("refine_endgame_holdout.refine_holdout") as heldout, \
                    patch("teacher_quality.refine_dataset") as train, \
                    patch("endgame_learning_curve.run_curve", side_effect=curve), \
                    patch.object(STUDY, "combine_quality"), \
                    patch.object(STUDY, "compute_controls", return_value={}), \
                    patch.object(STUDY.CYCLE, "run_rule_checks", return_value={"status": "passed"}):
                result = STUDY.prepare(args, state, catalog)
            self.assertEqual(result["status"], "ready_for_validation")
            self.assertEqual(result["attempt_index"], 6)
            self.assertEqual(catalog["attempts_started"], 6)
            self.assertEqual((catalog["datasets"], catalog["endgame_holdouts"]), references)
            self.assertEqual(result["data_reuse"]["source_attempt_index"], 5)
            self.assertEqual(result["validation_profile"], "fixed-nodes-remote-time-v1")
            self.assertEqual(result["configuration"]["development_nodes"], 1000)
            self.assertEqual(result["configuration"]["validation_location"], "github-actions-windows")
            diagnosis.assert_not_called()
            collect.assert_not_called()
            heldout.assert_not_called()
            train.assert_not_called()


if __name__ == "__main__":
    unittest.main()
