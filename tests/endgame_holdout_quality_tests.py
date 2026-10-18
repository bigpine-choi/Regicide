"""One-time quality definition of fresh endgame holdouts, never old holdouts."""

import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import chess
import chess.pgn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import collect_endgame_data as ENDATA
import collect_learning_data as DATA
import refine_endgame_holdout as HOLD
from teacher_quality_tests import Teacher

BUDGETS = (1000, 2000, 4000)


class EndgameHoldoutQualityTests(unittest.TestCase):
    def fixture(self, temporary):
        root = Path(temporary)
        source = root / "data"
        source.mkdir()
        fen = "7k/8/5KQ1/8/8/8/8/8 w - - 0 1"
        seeds = {"schema": "regicide.endgame.seeds.v1", "families": [
            {"id": "fixture", "split": "validation", "theme": "unit fixture",
             "starts": [fen, "8/8/8/8/8/5kq1/8/7K b - - 0 1"]}]}
        seed_path = root / "seeds.json"
        DATA.checkpoint_json(seed_path, seeds)
        game = {"game_id": "fixture", "family": "endgame-v1-fixture", "split": "validation",
                "initial_fen": fen, "history": ["g6g7"], "result": "1-0", "completed": True}
        row = {"game_id": "fixture", "family": game["family"], "split": "validation", "initial_fen": fen,
               "history": [], "ply": 0, "fen": fen, "turn": "w", "material_cp_stm": 900,
               "outcome_stm": 1, "teacher_cp_stm": 10, "teacher_nodes": 500, "teacher_depth": 2,
               "teacher_pv": ["g6g7"], "teacher_score_raw_info": "info depth 2 score cp 10 nodes 500 pv g6g7"}
        DATA.write_jsonl(source / "games.jsonl", [game])
        DATA.write_jsonl(source / "positions.jsonl", [row])
        board = DATA.replay_position(fen, game["history"])
        pgn = chess.pgn.Game.from_board(board)
        pgn.headers["Result"] = game["result"]
        (source / "games.pgn").write_bytes((str(pgn).replace("\n", "\r\n") + "\r\n").encode())
        manifest = {"schema": DATA.SCHEMA, "status": "complete", "games_path": "games.jsonl",
                    "positions_path": "positions.jsonl", "pgn_path": "games.pgn",
                    "endgame": {"schema": "regicide.endgame.collection.v1", "selected_split": "heldout",
                                "seed_definition": seeds, "seed_file_sha256": DATA.sha256_file(seed_path)},
                    "files_sha256": {name: DATA.sha256_file(source / name) for name in ("games.jsonl", "positions.jsonl", "games.pgn")}}
        DATA.checkpoint_json(source / "manifest.json", manifest)
        binary = root / "regicide"
        binary.write_bytes(b"mocked native binary")
        return source, binary, root / "quality", seed_path, manifest, row

    def teacher(self, stages=None, callback=None):
        stages = stages or [{"cp": 10}, {"cp": 20}, {"cp": 30}]
        return Teacher([{**item, "move": "g6g7"} for item in stages], callback=callback)

    def run_fixture(self, temporary, teacher=None, **kwargs):
        source, binary, output, seeds, manifest, row = self.fixture(temporary)
        teacher = teacher or self.teacher()
        with patch.object(ENDATA, "DEFAULT_SEEDS", seeds), patch.object(DATA.LearningEngine, "popen_uci", return_value=teacher), contextlib.redirect_stdout(io.StringIO()):
            result = HOLD.refine_holdout(source, binary, output, node_budgets=BUDGETS, **kwargs)
        report = json.loads((output / "holdout-quality-report.json").read_text())
        return result, report, source, output, seeds, teacher, row

    def test_three_stage_policy_preserves_history_games_and_pgn_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            result, report, source, output, _, teacher, original = self.run_fixture(temporary)
            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["quality_counts"], {"requested": 1, "accepted": 1, "excluded": 0})
            self.assertEqual(report["queries_completed"], 3)
            for name in ("games.jsonl", "games.pgn"):
                self.assertEqual((source / name).read_bytes(), (output / name).read_bytes())
            row = json.loads((output / "positions.jsonl").read_text())
            self.assertEqual(row["teacher_cp_stm"], 30)
            self.assertEqual(row["teacher_nodes"], 4000)
            self.assertEqual(row["split"], "validation")
            self.assertEqual(row["initial_holdout_quality"]["original_teacher"]["teacher_cp_stm"], original["teacher_cp_stm"])
            self.assertEqual(len({id(call["game"]) for call in teacher.calls}), 3)
            self.assertIn({"UseNeuralEvaluation": False}, teacher.configurations)
            self.assertEqual(report["policy"]["node_budgets"], list(BUDGETS))

    def test_no_bootstrap_or_train_dataset_can_be_refined(self):
        for change in ("no_endgame", "train", "all", "old_refined", "old_teacher"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
                source, binary, output, seeds, manifest, _ = self.fixture(temporary)
                if change == "no_endgame":
                    manifest.pop("endgame")
                elif change in ("train", "all"):
                    manifest["endgame"]["selected_split"] = change
                elif change == "old_refined":
                    manifest["endgame_holdout_refinement"] = {"schema": HOLD.SCHEMA}
                else:
                    manifest["teacher_quality_schema"] = "old"
                DATA.checkpoint_json(source / "manifest.json", manifest)
                with patch.object(ENDATA, "DEFAULT_SEEDS", seeds), self.assertRaisesRegex(ValueError, "NEW unrefined"):
                    HOLD.refine_holdout(source, binary, output, node_budgets=BUDGETS)
                self.assertFalse(output.exists())

    def test_mismatched_seed_definition_or_hash_rejected(self):
        for change in ("definition", "sha"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
                source, binary, output, seeds, manifest, _ = self.fixture(temporary)
                if change == "definition":
                    manifest["endgame"]["seed_definition"]["scope"] = "changed"
                else:
                    manifest["endgame"]["seed_file_sha256"] = "0" * 64
                DATA.checkpoint_json(source / "manifest.json", manifest)
                with patch.object(ENDATA, "DEFAULT_SEEDS", seeds), self.assertRaisesRegex(ValueError, "seed definition"):
                    HOLD.refine_holdout(source, binary, output, node_budgets=BUDGETS)

    def test_existing_output_and_descendant_source_output_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, binary, output, seeds, _, _ = self.fixture(temporary)
            with patch.object(ENDATA, "DEFAULT_SEEDS", seeds):
                with self.assertRaisesRegex(ValueError, "outside"):
                    HOLD.refine_holdout(source, binary, source / "changed", node_budgets=BUDGETS)
                output.mkdir()
                with self.assertRaises(FileExistsError):
                    HOLD.refine_holdout(source, binary, output, node_budgets=BUDGETS)

    def test_refined_output_cannot_be_relabelled_again(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, _, _, output, seeds, _, _ = self.run_fixture(temporary)
            binary = Path(temporary) / "regicide"
            with patch.object(ENDATA, "DEFAULT_SEEDS", seeds), self.assertRaisesRegex(ValueError, "NEW unrefined"):
                HOLD.refine_holdout(output, binary, Path(temporary) / "second", node_budgets=BUDGETS)

    def test_mate_bound_unstable_depth_and_nonexhausted_budget_excluded(self):
        stage_sets = ([{}, {}, {"mate": 1}], [{}, {"bound": True}, {}],
                      [{}, {}, {"cp": 100}], [{}, {}, {"depth": 2}], [{}, {}, {"nodes": 3000}])
        for stages in stage_sets:
            with self.subTest(stages=stages), tempfile.TemporaryDirectory() as temporary:
                result, report, _, output, _, _, _ = self.run_fixture(temporary, self.teacher(stages))
                self.assertEqual(result["quality_counts"]["accepted"], 0)
                self.assertEqual((output / "positions.jsonl").read_bytes(), b"")
                self.assertFalse(report["records"][0]["accepted"])
                self.assertTrue(report["records"][0]["exclusion"])

    def test_source_mutation_during_teacher_query_fails_freeze(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, binary, output, seeds, _, _ = self.fixture(temporary)
            def mutate():
                with (source / "games.pgn").open("ab") as handle:
                    handle.write(b" ")
            with patch.object(ENDATA, "DEFAULT_SEEDS", seeds), patch.object(DATA.LearningEngine, "popen_uci", return_value=self.teacher(callback=mutate)), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(ValueError, "frozen input changed"):
                    HOLD.refine_holdout(source, binary, output, node_budgets=BUDGETS)
            report = json.loads((output / "holdout-quality-report.json").read_text())
            self.assertEqual(report["status"], "failed")

    def test_transport_failure_keeps_raw_partial_records_without_complete_dataset(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, binary, output, seeds, _, _ = self.fixture(temporary)
            with patch.object(ENDATA, "DEFAULT_SEEDS", seeds), patch.object(DATA.LearningEngine, "popen_uci", return_value=self.teacher([{}, {"fail": True}, {}])), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, "transport"):
                    HOLD.refine_holdout(source, binary, output, node_budgets=BUDGETS)
            report = json.loads((output / "holdout-quality-report.json").read_text())
            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["queries_completed"], 1)
            self.assertEqual(len(report["records"][0]["stages"]), 1)
            self.assertEqual(json.loads((output / "manifest.json").read_text())["status"], "failed")

    def test_old_row_provenance_cannot_be_disguised_as_fresh(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, binary, output, seeds, manifest, row = self.fixture(temporary)
            row["teacher_quality"] = {"schema": "previous"}
            DATA.write_jsonl(source / "positions.jsonl", [row])
            manifest["files_sha256"]["positions.jsonl"] = DATA.sha256_file(source / "positions.jsonl")
            DATA.checkpoint_json(source / "manifest.json", manifest)
            with patch.object(ENDATA, "DEFAULT_SEEDS", seeds), self.assertRaisesRegex(ValueError, "previously refined"):
                HOLD.refine_holdout(source, binary, output, node_budgets=BUDGETS)

    def test_original_mismatched_or_bound_raw_label_rejected(self):
        for raw in ("info score cp 99", "info score cp 10 lowerbound", "info score mate 2"):
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as temporary:
                source, binary, output, seeds, manifest, row = self.fixture(temporary)
                row["teacher_score_raw_info"] = raw
                DATA.write_jsonl(source / "positions.jsonl", [row])
                manifest["files_sha256"]["positions.jsonl"] = DATA.sha256_file(source / "positions.jsonl")
                DATA.checkpoint_json(source / "manifest.json", manifest)
                with patch.object(ENDATA, "DEFAULT_SEEDS", seeds), self.assertRaisesRegex(ValueError, "exact finite"):
                    HOLD.refine_holdout(source, binary, output, node_budgets=BUDGETS)


if __name__ == "__main__":
    unittest.main()
