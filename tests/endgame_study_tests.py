"""Study orchestration: immutable inputs, fixed candidates and persistent attempts."""

import contextlib
import copy
import io
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
import endgame_study as STUDY
from neural_training_tests import fixture


class EndgameStudyTests(unittest.TestCase):
    def dataset(self, directory, games, rows):
        directory.mkdir()
        for name, values in (("games.jsonl", games), ("positions.jsonl", rows)):
            (directory / name).write_bytes(b"".join((json.dumps(row, sort_keys=True) + "\r\n").encode() for row in values))
        pgns = []
        for game in games:
            board = DATA.replay_position(game["initial_fen"], game["history"])
            parsed = chess.pgn.Game.from_board(board)
            parsed.headers["Result"] = game["result"]
            pgns.append(str(parsed))
        (directory / "games.pgn").write_bytes(("\r\n\r\n".join(pgns) + "\r\n\r\n").encode())
        manifest = {"schema": DATA.SCHEMA, "status": "complete", "games_path": "games.jsonl",
                    "positions_path": "positions.jsonl", "pgn_path": "games.pgn",
                    "files_sha256": {name: DATA.sha256_file(directory / name) for name in ("games.jsonl", "positions.jsonl", "games.pgn")}}
        DATA.checkpoint_json(directory / "manifest.json", manifest)
        return directory / "manifest.json"

    def composition_fixture(self, temporary):
        root = Path(temporary)
        original = root / "original"
        original.mkdir()
        _, games, rows = fixture(original)
        train = self.dataset(root / "train", games[:1], rows[:1])
        heldout = self.dataset(root / "heldout", games[1:], rows[1:])
        return train, heldout, root / "combined", games, rows

    def curve_fixture(self, run):
        arms = []
        for count in (0, 128, 512):
            runs = []
            for seed in (101, 202):
                relative = f"added-{count:06d}/seed-{seed}/small-v2.nn"
                path = run / "curve" / relative
                path.parent.mkdir(parents=True)
                path.write_bytes(f"fixture model {count} {seed}".encode())
                runs.append({"seed": seed, "model_path": relative, "model_sha256": DATA.sha256_file(path),
                             "selected_epoch": 7, "test": {"mse": 1000 if count == 512 else 0}})
            arms.append({"added_train": count, "runs": runs})
        first = arms[-1]["runs"][0]
        candidate = {"added_train": 512, "seed": 101, "model_path": first["model_path"],
                     "model_sha256": first["model_sha256"], "selected_epoch": first["selected_epoch"]}
        curve = {"status": "complete", "training_seeds": [101, 202], "arms": arms,
                 "targets_added_train": [0, 128, 512, 1024], "feasible_added_train": [0, 128, 512],
                 "candidate": candidate}
        DATA.checkpoint_json(run / "curve/report.json", curve)
        return curve, {**candidate, "model_path": "curve/" + candidate["model_path"]}

    def validation_fixture(self, temporary):
        state = Path(temporary) / "state"
        run_relative = "runs/000005-fixture"
        run = state / run_relative
        run.mkdir(parents=True)
        binary = Path(temporary) / "installed-engine"
        binary.write_bytes(b"fixture engine")
        snapshot = run / ("regicide.exe" if sys.platform == "win32" else "regicide")
        snapshot.write_bytes(binary.read_bytes())
        curve, candidate = self.curve_fixture(run)
        report = {"status": "ready_for_validation", "policy": STUDY.POLICY, "attempt_index": 5,
                  "installed_binary": str(binary), "binary_sha256": DATA.sha256_file(binary),
                  "implementation_sha256": {"fixture": "frozen"}, "study_artifact_sha256": {},
                  "previous_model_sha256": None, "candidate": candidate, "adopted": False}
        DATA.checkpoint_json(run / "cycle.json", report)
        catalog = {"attempts_started": 5, "runs": [run_relative]}
        args = SimpleNamespace(run=run_relative, binary=binary)
        return args, state, run, report, catalog, curve

    def test_combine_preserves_every_heldout_byte_and_all_games_and_pgn(self):
        with tempfile.TemporaryDirectory() as temporary:
            train, heldout, output, _, _ = self.composition_fixture(temporary)
            before = {str(path): path.read_bytes() for parent in (train.parent, heldout.parent) for path in parent.iterdir()}
            result = STUDY.combine_quality(train, heldout, output)
            self.assertEqual(result["positions_by_split"], {"validation": 1, "test": 1, "train": 1})
            for name in ("positions.jsonl", "games.jsonl", "games.pgn"):
                self.assertEqual((output / name).read_bytes(), (heldout.parent / name).read_bytes() + (train.parent / name).read_bytes())
                self.assertEqual(result["files_sha256"][name], DATA.sha256_file(output / name))
            for name, raw in before.items():
                self.assertEqual(Path(name).read_bytes(), raw)

    def test_combine_refuses_existing_output_without_rewriting_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            train, heldout, output, _, _ = self.composition_fixture(temporary)
            output.mkdir()
            (output / "sentinel").write_bytes(b"keep")
            with self.assertRaises(FileExistsError):
                STUDY.combine_quality(train, heldout, output)
            self.assertEqual((output / "sentinel").read_bytes(), b"keep")

    def test_combine_rejects_swapped_train_and_holdout_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            train, heldout, output, _, _ = self.composition_fixture(temporary)
            with self.assertRaisesRegex(ValueError, "heldout contains training"):
                STUDY.combine_quality(heldout, train, output)
            self.assertFalse(output.exists())

    def test_combine_rejects_game_id_collisions(self):
        with tempfile.TemporaryDirectory() as temporary:
            train, heldout, output, games, rows = self.composition_fixture(temporary)
            train_game, train_row = copy.deepcopy(games[0]), copy.deepcopy(rows[0])
            train_game["game_id"] = train_row["game_id"] = games[1]["game_id"]
            collision = self.dataset(Path(temporary) / "collision", [train_game], [train_row])
            with self.assertRaisesRegex(ValueError, "IDs collide"):
                STUDY.combine_quality(collision, heldout, output)
            self.assertFalse(output.exists())

    def test_combine_rejects_family_split_collision(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, heldout, output, games, rows = self.composition_fixture(temporary)
            train_game, train_row = copy.deepcopy(games[0]), copy.deepcopy(rows[0])
            train_game["family"] = train_row["family"] = games[1]["family"]
            collision = self.dataset(Path(temporary) / "collision", [train_game], [train_row])
            with self.assertRaisesRegex(ValueError, "family crosses split"):
                STUDY.combine_quality(collision, heldout, output)
            self.assertFalse((output / "manifest.json").exists())

    def test_combine_rejects_same_actual_input_even_under_new_game_and_family_ids(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, heldout, output, games, rows = self.composition_fixture(temporary)
            train_game, train_row = copy.deepcopy(games[1]), copy.deepcopy(rows[1])
            for row in (train_game, train_row):
                row.update(game_id="new-id", family="new-train-family", split="train")
            collision = self.dataset(Path(temporary) / "collision", [train_game], [train_row])
            with self.assertRaisesRegex(ValueError, "canonical position|neural feature"):
                STUDY.combine_quality(collision, heldout, output)
            self.assertFalse(output.exists())
            staged = output.with_name(output.name + ".partial")
            self.assertEqual(json.loads((staged / "manifest.json").read_text())["status"], "failed")

    def test_label_free_game_of_wrong_split_is_also_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, heldout, output, games, rows = self.composition_fixture(temporary)
            mixed = self.dataset(Path(temporary) / "mixed", games[:2], rows[:1])
            with self.assertRaisesRegex(ValueError, "game split"):
                STUDY.combine_quality(mixed, heldout, output)
            self.assertFalse(output.exists())

    def test_fixed_candidate_ignores_better_test_metrics_or_alternative_seed(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            curve, candidate = self.curve_fixture(run)
            chosen = STUDY.fixed_candidate(curve, {"candidate": candidate}, run)
            self.assertEqual(chosen, run / candidate["model_path"])
            self.assertIn("added-000512/seed-101", chosen.as_posix())

    def test_fixed_candidate_rederivation_rejects_report_curve_and_seed_forgery(self):
        for mutation in ("report", "curve", "second_seed", "seed_order"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                run = Path(temporary)
                curve, candidate = self.curve_fixture(run)
                report = {"candidate": candidate}
                if mutation == "report":
                    report["candidate"] = {**candidate, "added_train": 128}
                elif mutation == "curve":
                    curve["candidate"]["added_train"] = 128
                elif mutation == "second_seed":
                    curve["arms"][-1]["runs"][0]["seed"] = 202
                else:
                    curve["training_seeds"] = [202, 101]
                with self.assertRaisesRegex(ValueError, "fixed largest-arm"):
                    STUDY.fixed_candidate(curve, report, run)

    def test_candidate_bytes_or_path_escape_cannot_pass(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            curve, candidate = self.curve_fixture(run)
            (run / candidate["model_path"]).write_bytes(b"changed model")
            with self.assertRaisesRegex(ValueError, "bytes changed"):
                STUDY.fixed_candidate(curve, {"candidate": candidate}, run)
            relative = "../escape.nn"
            curve["arms"][-1]["runs"][0]["model_path"] = relative
            curve["candidate"]["model_path"] = relative
            candidate["model_path"] = "curve/" + relative
            # contained() confines to the run, so a path outside that run must fail.
            curve["arms"][-1]["runs"][0]["model_path"] = "../../escape.nn"
            curve["candidate"]["model_path"] = "../../escape.nn"
            candidate["model_path"] = "curve/../../escape.nn"
            with self.assertRaises(ValueError):
                STUDY.fixed_candidate(curve, {"candidate": candidate}, run)

    def test_preregistered_arm_order_and_feasible_sizes_cannot_be_rewritten(self):
        for mutation in ("order", "feasible", "target"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                run = Path(temporary)
                curve, candidate = self.curve_fixture(run)
                if mutation == "order":
                    curve["arms"] = list(reversed(curve["arms"]))
                elif mutation == "feasible":
                    curve["feasible_added_train"] = [0, 128]
                else:
                    curve["targets_added_train"] = [0, 128, 1024]
                with self.assertRaisesRegex(ValueError, "arm order or sizes"):
                    STUDY.fixed_candidate(curve, {"candidate": candidate}, run)

    def test_verify_frozen_blocks_source_artifact_and_installed_engine_mutation(self):
        for mutation in ("source", "artifact", "engine"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                args, _, run, report, _, _ = self.validation_fixture(temporary)
                artifact = run / "evidence.json"
                artifact.write_bytes(b"frozen evidence")
                report["study_artifact_sha256"] = {"evidence.json": DATA.sha256_file(artifact)}
                if mutation == "artifact":
                    artifact.write_bytes(b"changed evidence")
                elif mutation == "engine":
                    args.binary.write_bytes(b"changed engine")
                actual_sources = {"fixture": "changed"} if mutation == "source" else {"fixture": "frozen"}
                with patch.object(STUDY, "frozen_sources", return_value=actual_sources), self.assertRaisesRegex(ValueError, "changed"):
                    STUDY.verify_frozen(report, run)

    def test_validate_rejects_wrong_status_attempt_policy_and_run_before_any_work(self):
        for mutation in ("status", "attempt", "policy", "run"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                args, state, run, report, catalog, _ = self.validation_fixture(temporary)
                if mutation == "status":
                    report["status"] = "complete"
                elif mutation == "attempt":
                    catalog["attempts_started"] += 1
                elif mutation == "policy":
                    report["policy"] = "changed"
                else:
                    catalog["runs"].append("runs/000006-other")
                DATA.checkpoint_json(run / "cycle.json", report)
                before = (run / "cycle.json").read_bytes()
                with patch.object(STUDY, "verify_frozen") as freeze, patch("endgame_matches.run_matches") as matches, patch("learning_arena.run_arena") as arena, patch("learning_registry.promote") as promote:
                    with self.assertRaisesRegex(ValueError, "current untouched prepared"):
                        STUDY.validate(args, state, catalog)
                    freeze.assert_not_called()
                    matches.assert_not_called()
                    arena.assert_not_called()
                    promote.assert_not_called()
                self.assertEqual((run / "cycle.json").read_bytes(), before)

    def test_validation_wrong_engine_identity_blocks_matches_and_promotion(self):
        with tempfile.TemporaryDirectory() as temporary:
            args, state, run, _, catalog, _ = self.validation_fixture(temporary)
            wrong = Path(temporary) / "different-engine"
            wrong.write_bytes(b"different")
            args.binary = wrong
            with patch.object(STUDY, "verify_frozen"), patch("endgame_matches.run_matches") as matches, patch("learning_arena.run_arena") as arena:
                with self.assertRaisesRegex(ValueError, "differs from prepared"):
                    STUDY.validate(args, state, catalog)
                matches.assert_not_called()
                arena.assert_not_called()
            self.assertEqual(json.loads((run / "cycle.json").read_text())["status"], "failed")

    def test_validation_missing_or_forged_candidate_blocks_any_match(self):
        for missing in (False, True):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as temporary:
                args, state, run, report, catalog, _ = self.validation_fixture(temporary)
                if missing:
                    report.pop("candidate")
                else:
                    report["candidate"]["model_sha256"] = "0" * 64
                DATA.checkpoint_json(run / "cycle.json", report)
                with patch.object(STUDY, "verify_frozen"), patch("learning_registry.load_champion", return_value=None), patch("endgame_matches.run_matches") as matches, patch("learning_arena.run_arena") as arena:
                    with self.assertRaises((ValueError, KeyError)):
                        STUDY.validate(args, state, catalog)
                    matches.assert_not_called()
                    arena.assert_not_called()

    def test_development_scores_never_choose_another_model_or_authorize_adoption(self):
        with tempfile.TemporaryDirectory() as temporary:
            args, state, run, report, catalog, curve = self.validation_fixture(temporary)
            good_dev = {"completed": True, "valid_for_comparison": True, "summary": {"candidate_score": 1.0}}
            rejected_arena = {"status": "complete", "decision": {"reason": "below lower bound"}, "promotion_passed": False}
            with patch.object(STUDY, "verify_frozen"), patch("learning_registry.load_champion", return_value=None), patch("endgame_matches.run_matches", return_value=good_dev) as matches, patch("learning_arena.run_arena", return_value=rejected_arena) as arena, patch("learning_registry.promote") as promote, patch.object(STUDY.CYCLE, "catalog_read", return_value=catalog):
                result = STUDY.validate(args, state, catalog)
            self.assertEqual(matches.call_count, len(curve["arms"]))
            self.assertEqual(arena.call_args.args[1], run / report["candidate"]["model_path"])
            self.assertEqual(arena.call_args.kwargs["attempt_index"], 5)
            self.assertFalse(result["adopted"])
            self.assertIsNone(result["selected_model_sha256"])
            promote.assert_not_called()
            self.assertEqual(catalog["attempts_started"], 5)

    def test_dirty_development_comparison_blocks_reserved_arena(self):
        with tempfile.TemporaryDirectory() as temporary:
            args, state, _, _, catalog, _ = self.validation_fixture(temporary)
            with patch.object(STUDY, "verify_frozen"), patch("learning_registry.load_champion", return_value=None), patch("endgame_matches.run_matches", return_value={"completed": True, "valid_for_comparison": False}), patch("learning_arena.run_arena") as arena:
                with self.assertRaisesRegex(ValueError, "contaminated"):
                    STUDY.validate(args, state, catalog)
                arena.assert_not_called()

    def test_prepare_failure_consumes_attempt_and_persists_failure_without_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            state.mkdir()
            baseline = Path(temporary) / "baseline.json"
            baseline.write_bytes(b"fixture source")
            bootstrap = state / "bootstrap/manifest.json"
            bootstrap.parent.mkdir()
            bootstrap.write_bytes(b"fixture bootstrap")
            binary = Path(temporary) / "regicide"
            binary.write_bytes(b"fixture engine")
            catalog = {"attempts_started": 4, "runs": [], "bootstrap": {"manifest": "bootstrap/manifest.json"}, "datasets": []}
            args = SimpleNamespace(baseline=baseline, binary=binary, seed=123, train_repeats=4)
            with patch.object(STUDY, "_source"), patch.object(STUDY, "frozen_sources", return_value={}), patch("learning_registry.load_champion", return_value=None), patch("diagnose_endgames.run_diagnosis", side_effect=RuntimeError("synthetic diagnostic failure")), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, "diagnostic failure"):
                    STUDY.prepare(args, state, catalog)
            persisted = json.loads((state / "catalog.json").read_text())
            self.assertEqual(persisted["attempts_started"], 5)
            self.assertEqual(len(persisted["runs"]), 1)
            report = json.loads((state / persisted["runs"][0] / "cycle.json").read_text())
            self.assertEqual(report["attempt_index"], 5)
            self.assertEqual(report["status"], "failed")
            self.assertEqual(list(state.rglob("*.nn")), [])
            self.assertFalse((state / "champion.json").exists())

    def catalog_fixture(self, temporary):
        state = Path(temporary) / "state"
        state.mkdir()
        original = Path(temporary) / "original"
        original.mkdir()
        _, games, rows = fixture(original)
        bootstrap = self.dataset(state / "bootstrap", games, rows)
        heldout = self.dataset(state / "new-heldout", games[1:], rows[1:])
        document = json.loads(heldout.read_text())
        document["endgame"] = {"schema": "regicide.endgame.collection.v1", "selected_split": "heldout"}
        DATA.checkpoint_json(heldout, document)
        catalog = {"schema": STUDY.CYCLE.SCHEMA, "attempts_started": 0, "runs": [], "datasets": [],
                   "bootstrap": STUDY.CYCLE.dataset_reference(state, bootstrap),
                   "endgame_holdouts": [STUDY.CYCLE.dataset_reference(state, heldout)]}
        DATA.checkpoint_json(state / "catalog.json", catalog)
        return state, heldout, catalog, rows

    def test_catalog_keeps_new_holdout_hashes_and_builds_membership_only_exclusion(self):
        with tempfile.TemporaryDirectory() as temporary:
            state, _, expected, rows = self.catalog_fixture(temporary)
            catalog = STUDY.CYCLE.catalog_read(state)
            self.assertEqual(catalog, expected)
            inventory = STUDY.CYCLE.endgame_holdout_exclusion(state, catalog)
            board = DATA.replay_position(rows[1]["initial_fen"], rows[1]["history"])
            self.assertEqual(inventory.reason(board), "reserved_canonical_input")
            # Every prefix is protected, not only sampled teacher positions.
            prefix = DATA.replay_position(rows[1]["initial_fen"], rows[1]["history"][:1])
            self.assertIsNotNone(inventory.reason(prefix))
            self.assertIsNone(STUDY.CYCLE.endgame_holdout_exclusion(state, {"endgame_holdouts": []}))

    def test_catalog_refuses_mutated_duplicate_or_wrong_role_holdout(self):
        for mutation in ("bytes", "duplicate", "role", "malformed"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                state, heldout, catalog, _ = self.catalog_fixture(temporary)
                if mutation == "bytes":
                    with (heldout.parent / "positions.jsonl").open("ab") as handle:
                        handle.write(b" ")
                elif mutation == "duplicate":
                    catalog["endgame_holdouts"].append(copy.deepcopy(catalog["endgame_holdouts"][0]))
                elif mutation == "malformed":
                    catalog["endgame_holdouts"] = "not a list"
                else:
                    document = json.loads(heldout.read_text())
                    document["endgame"]["selected_split"] = "train"
                    DATA.checkpoint_json(heldout, document)
                    catalog["endgame_holdouts"] = [STUDY.CYCLE.dataset_reference(state, heldout)]
                DATA.checkpoint_json(state / "catalog.json", catalog)
                with self.assertRaisesRegex(ValueError, "holdout"):
                    STUDY.CYCLE.catalog_read(state)


if __name__ == "__main__":
    unittest.main()
