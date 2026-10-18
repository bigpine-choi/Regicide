"""Teacher-ladder safety, frozen holdouts and optional native UCI integration."""

import argparse
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

import chess
import chess.engine
import chess.pgn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import teacher_quality as QUALITY
import collect_learning_data as DATA
from neural_training_tests import fixture, write_fixture
from train_neural import load_dataset, sha256


BINARY = None
BUDGETS = (1000, 2000, 4000)


class Teacher:
    def __init__(self, stages=None, callback=None):
        self.id = {"name": "Regicide teacher-quality fixture"}
        self.options = {key: object() for key in (*DATA.OPTIONS, "UseNeuralEvaluation", "Clear Hash")}
        self.protocol = SimpleNamespace(last_score_event=None)
        self.calls, self.configurations = [], []
        self.stages = stages or [{"cp": 10}, {"cp": 20}, {"cp": 30}]
        self.callback = callback

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def configure(self, options):
        self.configurations.append(dict(options))

    def play(self, board, limit, **kwargs):
        stage = self.stages[len(self.calls) % 3]
        self.calls.append({"board": board.copy(stack=True), "limit": limit, **kwargs})
        if self.callback is not None:
            self.callback()
        if stage.get("fail"):
            raise RuntimeError("synthetic teacher transport error")
        move = chess.Move.from_uci(stage.get("move", "e2e4"))
        cp, depth, nodes = stage.get("cp", 20), stage.get("depth", 4), stage.get("nodes", limit.nodes)
        kind, value = ("mate", stage["mate"]) if "mate" in stage else ("cp", cp)
        raw = f"info depth {depth} score {kind} {value}"
        raw += " lowerbound" if stage.get("bound") else ""
        raw += f" nodes {nodes} pv {move.uci()}"
        if stage.get("raw_mismatch"):
            raw = raw.replace(f"depth {depth}", f"depth {depth + 1}")
        self.protocol.last_score_event = DATA.parse_score_event(raw) if not stage.get("missing_score") else None
        score = chess.engine.Mate(value) if kind == "mate" else chess.engine.Cp(value)
        info = {"score": chess.engine.PovScore(score, board.turn), "depth": depth,
                "nodes": nodes, "pv": [move]}
        if stage.get("missing_score"):
            info.pop("score")
        if stage.get("missing_pv"):
            info.pop("pv")
        if stage.get("illegal_pv"):
            info["pv"] = [move, chess.Move.from_uci("e7e5")]
        return SimpleNamespace(move=move, info=info)


class TeacherQualityTests(unittest.TestCase):
    def test_train_only_mode_requires_explicit_endgame_provenance_and_train_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, _, _, manifest, games, positions = self.setup_source(temporary)
            with self.assertRaisesRegex(ValueError, "train-only"):
                QUALITY._source_state(source, allow_train_only_endgame=True)
            manifest["endgame"] = {"selected_split": "train"}
            DATA.checkpoint_json(source, manifest)
            with self.assertRaisesRegex(ValueError, "train-only"):
                QUALITY._source_state(source, allow_train_only_endgame=True)
            selected_games = [row for row in games if row["split"] == "train"]
            selected_rows = [row for row in positions if row["split"] == "train"]
            DATA.write_jsonl(source.parent / "games.jsonl", selected_games)
            DATA.write_jsonl(source.parent / "positions.jsonl", selected_rows)
            pgn = chess.pgn.Game.from_board(DATA.replay_position(
                selected_games[0]["initial_fen"], selected_games[0]["history"]))
            (source.parent / "games.pgn").write_text(str(pgn) + "\n", encoding="utf-8")
            manifest["files_sha256"] = {name: sha256(source.parent / name)
                                       for name in ("games.jsonl", "positions.jsonl", "games.pgn")}
            DATA.checkpoint_json(source, manifest)
            self.assertTrue(QUALITY._source_state(source, allow_train_only_endgame=True)["positions"])
            with self.assertRaises(ValueError):
                QUALITY._source_state(source)

    def setup_source(self, temporary, *, pgn=True, crlf=True):
        root = Path(temporary).resolve()
        source = root / "source"
        source.mkdir()
        manifest, games, positions = fixture(source)
        # An actual repetition-bearing prefix must survive every teacher query.
        # The standard fixture already has a real two-ply history; a separate
        # direct search test below covers a full repetition stack.
        if crlf:
            (source / "positions.jsonl").write_bytes(b"".join(
                (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\r\n").encode("utf-8")
                for row in positions))
        if pgn:
            pgns = []
            for game in games:
                board = DATA.replay_position(game["initial_fen"], game["history"])
                parsed = chess.pgn.Game.from_board(board)
                parsed.headers["Result"] = game["result"]
                pgns.append(str(parsed))
            (source / "games.pgn").write_bytes(("\r\n\r\n".join(pgns) + "\r\n").encode("utf-8"))
            manifest["pgn_path"] = "games.pgn"
        names = ["games.jsonl", "positions.jsonl"] + (["games.pgn"] if pgn else [])
        manifest["files_sha256"] = {name: sha256(source / name) for name in names}
        manifest_path = source / "datasetmanifest.json"
        DATA.checkpoint_json(manifest_path, manifest)
        binary = root / "fixture-engine"
        binary.write_bytes(b"mocked native binary")
        return manifest_path, binary, root / "quality", manifest, games, positions

    def run_fixture(self, temporary, teacher=None, **kwargs):
        source, binary, output, manifest, games, positions = self.setup_source(temporary)
        teacher = teacher or Teacher()
        with patch.object(DATA.LearningEngine, "popen_uci", return_value=teacher), contextlib.redirect_stdout(io.StringIO()):
            result = QUALITY.refine_dataset(source, binary, output, node_budgets=BUDGETS, **kwargs)
        report = json.loads((output / "quality-report.json").read_text(encoding="utf-8"))
        return result, report, teacher, source, output

    def test_defaults_are_prespecified_and_strict(self):
        policy = QUALITY._configuration(QUALITY.DEFAULT_BUDGETS, 30, 3)
        self.assertEqual(policy["node_budgets"], [10000, 50000, 200000])
        self.assertEqual(policy["max_last_two_cp_delta"], 30)
        self.assertEqual(policy["minimum_completed_depth"], 3)

    def test_exact_stable_final_label_preserves_holdout_and_game_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            result, report, teacher, source, output = self.run_fixture(temporary)
            self.assertTrue(result["trainable"])
            self.assertEqual(result["quality_counts"], {"requested": 1, "accepted": 1, "excluded": 0})
            self.assertEqual(report["status"], "complete")
            self.assertEqual(report["queries_completed"], 3)
            self.assertEqual(report["source_holdout"], report["output_holdout"])
            self.assertEqual(result["quality_report_sha256"], sha256(output / "quality-report.json"))
            self.assertEqual((source.parent / "games.jsonl").read_bytes(), (output / "games.jsonl").read_bytes())
            self.assertEqual((source.parent / "games.pgn").read_bytes(), (output / "games.pgn").read_bytes())
            original_lines = (source.parent / "positions.jsonl").read_bytes().splitlines(keepends=True)
            derived_lines = (output / "positions.jsonl").read_bytes().splitlines(keepends=True)
            self.assertEqual(original_lines[1:], derived_lines[1:])
            row = json.loads(derived_lines[0])
            self.assertEqual(row["teacher_cp_stm"], 30)
            self.assertEqual(row["teacher_nodes"], 4000)
            self.assertEqual(row["teacher_quality"]["original_teacher"]["teacher_cp_stm"], 20)
            self.assertEqual(len(teacher.calls), 3)
            self.assertEqual([call["limit"].nodes for call in teacher.calls], list(BUDGETS))
            self.assertTrue(all(call["limit"].time is None for call in teacher.calls))
            self.assertTrue(all(len(call["board"].move_stack) == 2 for call in teacher.calls))
            self.assertEqual(len({id(call["game"]) for call in teacher.calls}), 3)
            self.assertEqual(teacher.configurations.count({"Clear Hash": None}), 3)
            self.assertIn({"UseNeuralEvaluation": False}, teacher.configurations)
            _, metadata = load_dataset(output / "manifest.json")
            self.assertEqual(metadata["counts"]["train"]["samples"], 1)

    def test_last_two_threshold_is_inclusive_and_first_cp_can_differ(self):
        for delta, accepted in ((30, True), (31, False)):
            with self.subTest(delta=delta), tempfile.TemporaryDirectory() as temporary:
                result, report, *_ = self.run_fixture(temporary, Teacher([
                    {"cp": 900}, {"cp": 10}, {"cp": 10 + delta}]))
                self.assertEqual(result["trainable"], accepted)
                self.assertEqual(report["records"][0]["last_two_cp_delta"], delta)
                if not accepted:
                    self.assertEqual(report["records"][0]["exclusion"], "last_two_cp_unstable")

    def test_different_final_best_moves_are_filtered(self):
        with tempfile.TemporaryDirectory() as temporary:
            result, report, *_ = self.run_fixture(temporary, Teacher([
                {"move": "e2e4"}, {"move": "e2e4"}, {"move": "d2d4"}]))
            self.assertFalse(result["trainable"])
            self.assertEqual(report["records"][0]["exclusion"], "last_two_best_moves_differ")

    def test_mate_bound_missing_or_mate_range_at_any_stage_never_labels(self):
        cases = [({"mate": 2}, "mate_score"), ({"bound": True}, "bound_score"),
                 ({"missing_score": True}, "missing_score"), ({"missing_pv": True}, "missing_pv"),
                 ({"cp": QUALITY.MATE_CP_THRESHOLD}, "mate_range_cp"),
                 ({"depth": 0}, "no_completed_depth"), ({"nodes": 999}, "budget_not_exhausted")]
        for mutation, reason in cases:
            for index in range(3):
                with self.subTest(mutation=mutation, index=index), tempfile.TemporaryDirectory() as temporary:
                    stages = [{}, {}, {}]
                    stages[index] = mutation
                    result, report, _, _, output = self.run_fixture(temporary, Teacher(stages))
                    self.assertFalse(result["trainable"])
                    self.assertEqual(report["records"][0]["exclusion"], f"stage_{index + 1}_{reason}")
                    self.assertEqual(len([json.loads(line) for line in (output / "positions.jsonl").read_text().splitlines()]), 2)

    def test_low_completed_final_depth_is_filtered(self):
        for index in (1, 2):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as temporary:
                stages = [{}, {}, {}]
                stages[index]["depth"] = 2
                result, report, *_ = self.run_fixture(temporary, Teacher(stages))
                self.assertFalse(result["trainable"])
                self.assertEqual(report["records"][0]["exclusion"], "insufficient_completed_depth")

    def test_final_budget_must_exceed_original_teacher_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, binary, output, manifest, games, positions = self.setup_source(temporary)
            positions[0]["teacher_nodes"] = 4000
            write_fixture(source.parent, manifest, games, positions)
            manifest["files_sha256"]["positions.jsonl"] = sha256(source.parent / "positions.jsonl")
            DATA.checkpoint_json(source, manifest)
            with patch.object(DATA.LearningEngine, "popen_uci", return_value=Teacher()), contextlib.redirect_stdout(io.StringIO()):
                result = QUALITY.refine_dataset(source, binary, output, node_budgets=BUDGETS)
            self.assertFalse(result["trainable"])
            report = json.loads((output / "quality-report.json").read_text())
            self.assertEqual(report["records"][0]["exclusion"], "final_budget_not_greater_than_original")

    def test_invalid_search_transport_pv_or_raw_fields_fail_closed(self):
        for mutation in ({"fail": True}, {"move": "e2e5"}, {"illegal_pv": True}, {"raw_mismatch": True}):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                source, binary, output, *_ = self.setup_source(temporary)
                with patch.object(DATA.LearningEngine, "popen_uci", return_value=Teacher([mutation, {}, {}])), contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises((ValueError, RuntimeError)):
                        QUALITY.refine_dataset(source, binary, output, node_budgets=BUDGETS)
                self.assertEqual(json.loads((output / "manifest.json").read_text())["status"], "failed")
                self.assertEqual(json.loads((output / "quality-report.json").read_text())["status"], "failed")

    def test_source_hash_and_false_completed_result_are_rejected_before_output(self):
        for mutation in ("hash", "result"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                source, binary, output, manifest, games, positions = self.setup_source(temporary)
                if mutation == "hash":
                    (source.parent / "positions.jsonl").write_text("{}\n")
                else:
                    games[0]["result"] = "1-0"
                    write_fixture(source.parent, manifest, games, positions)
                    manifest["files_sha256"] = {name: sha256(source.parent / name) for name in manifest["files_sha256"]}
                    DATA.checkpoint_json(source, manifest)
                with self.assertRaises(ValueError):
                    QUALITY.refine_dataset(source, binary, output)
                self.assertFalse(output.exists())

    def test_source_raw_bound_or_mate_cannot_masquerade_as_cp(self):
        for raw in ("info score cp 20 lowerbound", "info score mate 1", "info score cp 21"):
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as temporary:
                source, binary, output, manifest, games, positions = self.setup_source(temporary)
                positions[0]["teacher_score_raw_info"] = raw
                write_fixture(source.parent, manifest, games, positions)
                manifest["files_sha256"]["positions.jsonl"] = sha256(source.parent / "positions.jsonl")
                DATA.checkpoint_json(source, manifest)
                with self.assertRaisesRegex(ValueError, "source label"):
                    QUALITY.refine_dataset(source, binary, output)
                self.assertFalse(output.exists())

    def test_holdout_canonical_hash_tampering_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, binary, output, manifest, *_ = self.setup_source(temporary)
            manifest["holdout_rows_sha256"] = {"validation": "0" * 64}
            DATA.checkpoint_json(source, manifest)
            with self.assertRaisesRegex(ValueError, "canonical holdout"):
                QUALITY.refine_dataset(source, binary, output)
            self.assertFalse(output.exists())

    def test_incomplete_game_sample_cannot_be_promoted_into_quality_training(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, binary, output, manifest, games, positions = self.setup_source(temporary, pgn=False)
            unfinished = copy.deepcopy(games[0])
            unfinished.update(game_id="unfinished", family="unfinished", completed=False,
                              result="*", history=unfinished["history"][:2])
            sample = copy.deepcopy(positions[0])
            sample.update(game_id="unfinished", family="unfinished")
            games.append(unfinished)
            positions.append(sample)
            write_fixture(source.parent, manifest, games, positions)
            manifest["files_sha256"] = {name: sha256(source.parent / name) for name in manifest["files_sha256"]}
            DATA.checkpoint_json(source, manifest)
            with self.assertRaisesRegex(ValueError, "completed source games"):
                QUALITY.refine_dataset(source, binary, output)
            self.assertFalse(output.exists())

    def test_reserved_arena_history_cannot_be_used_for_training(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, binary, output, manifest, games, positions = self.setup_source(temporary)
            # Swap the valid f4 mate game into train, and the f3 game into test.
            games[0]["split"], games[2]["split"] = "test", "train"
            positions[0]["split"], positions[2]["split"] = "test", "train"
            write_fixture(source.parent, manifest, games, positions)
            manifest["files_sha256"] = {name: sha256(source.parent / name) for name in manifest["files_sha256"]}
            DATA.checkpoint_json(source, manifest)
            with self.assertRaisesRegex(ValueError, "reserved arena prefix"):
                QUALITY.refine_dataset(source, binary, output)
            self.assertFalse(output.exists())

    def test_frozen_source_or_binary_mutation_fails_closed(self):
        for target in ("source", "binary"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as temporary:
                source, binary, output, *_ = self.setup_source(temporary)
                path = binary if target == "binary" else source
                def mutate():
                    with path.open("ab") as handle:
                        handle.write(b" ")
                with patch.object(DATA.LearningEngine, "popen_uci", return_value=Teacher(callback=mutate)), contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaisesRegex(ValueError, "frozen input changed"):
                        QUALITY.refine_dataset(source, binary, output, node_budgets=BUDGETS)
                self.assertEqual(json.loads((output / "manifest.json").read_text())["status"], "failed")

    def test_replay_preserves_repetition_stack_on_each_fresh_query(self):
        board = DATA.replay_position(chess.STARTING_FEN, ["g1f3", "g8f6", "f3g1", "f6g8"])
        teacher = Teacher()
        for nodes in BUDGETS:
            QUALITY.search_teacher(teacher, board, nodes)
        self.assertTrue(all(call["board"].move_stack == board.move_stack for call in teacher.calls))
        self.assertEqual(len({id(call["game"]) for call in teacher.calls}), 3)

    def test_original_history_and_fen_mismatch_rejected_before_search(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, binary, output, manifest, games, positions = self.setup_source(temporary)
            positions[0]["fen"] = chess.STARTING_FEN
            write_fixture(source.parent, manifest, games, positions)
            manifest["files_sha256"]["positions.jsonl"] = sha256(source.parent / "positions.jsonl")
            DATA.checkpoint_json(source, manifest)
            with self.assertRaisesRegex(ValueError, "FEN disagrees"):
                QUALITY.refine_dataset(source, binary, output)
            self.assertFalse(output.exists())

    def test_missing_required_options_or_foreign_engine_fail_closed(self):
        for mutation in ("foreign", "neural", "hash"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                source, binary, output, *_ = self.setup_source(temporary)
                teacher = Teacher()
                if mutation == "foreign":
                    teacher.id["name"] = "external fixture"
                else:
                    teacher.options.pop("UseNeuralEvaluation" if mutation == "neural" else "Clear Hash")
                with patch.object(DATA.LearningEngine, "popen_uci", return_value=teacher):
                    with self.assertRaises(ValueError):
                        QUALITY.refine_dataset(source, binary, output)
                self.assertEqual(json.loads((output / "manifest.json").read_text())["status"], "failed")

    def test_existing_output_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, binary, output, *_ = self.setup_source(temporary)
            output.mkdir()
            sentinel = output / "keep"
            sentinel.write_text("precious")
            with self.assertRaises(FileExistsError):
                QUALITY.refine_dataset(source, binary, output)
            self.assertEqual(sentinel.read_text(), "precious")

    def test_invalid_configuration_rejected_without_source_access(self):
        cases = [dict(node_budgets=(1, 2, 3)), dict(node_budgets=(1000, 1000, 2000)),
                 dict(node_budgets=(1000, 2000)), dict(node_budgets=(1000, True, 3000)),
                 dict(max_cp_delta=-1), dict(max_cp_delta=True), dict(min_depth=0), dict(min_depth=True)]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                QUALITY.refine_dataset("missing", "missing", "missing", **kwargs)

    def test_hce_difference_is_diagnostic_not_a_training_filter(self):
        with tempfile.TemporaryDirectory() as temporary:
            result, report, *_ = self.run_fixture(temporary, Teacher([
                {"cp": 2000}, {"cp": 2000}, {"cp": 2000}]))
            self.assertTrue(result["trainable"])
            record = report["records"][0]
            self.assertFalse(record["hce_is_search_label"])
            self.assertEqual(record["stages"][-1]["teacher_minus_hce_cp"], 2000 - record["hce_cp_stm"])

    def test_no_pgn_source_remains_schema_compatible(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, binary, output, *_ = self.setup_source(temporary, pgn=False)
            with patch.object(DATA.LearningEngine, "popen_uci", return_value=Teacher()), contextlib.redirect_stdout(io.StringIO()):
                result = QUALITY.refine_dataset(source, binary, output, node_budgets=BUDGETS)
            self.assertNotIn("pgn_path", result)
            load_dataset(output / "manifest.json")

    def test_native_full_history_ladder_exports_only_declared_stable_labels(self):
        if BINARY is None:
            self.skipTest("optional --binary native integration")
        with tempfile.TemporaryDirectory() as temporary:
            source, _, output, *_ = self.setup_source(temporary)
            with contextlib.redirect_stdout(io.StringIO()):
                result = QUALITY.refine_dataset(source, BINARY, output, node_budgets=BUDGETS,
                                                max_cp_delta=30, min_depth=3)
            report = json.loads((output / "quality-report.json").read_text())
            self.assertEqual(report["status"], "complete")
            self.assertEqual(report["queries_completed"], 3)
            self.assertEqual(report["source_holdout"], report["output_holdout"])
            record = report["records"][0]
            board = DATA.replay_position(record["initial_fen"], record["history"])
            for stage in record["stages"]:
                self.assertIn(chess.Move.from_uci(stage["best_move"]), board.legal_moves)
                self.assertTrue(stage["raw_score_event"]["raw_info"].startswith("info depth "))
                self.assertIsNone(stage["mate_stm"])
            load_dataset(output / "manifest.json", allow_empty_splits=True)
            self.assertEqual(result["trainable"], record["accepted"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--binary", type=Path)
    arguments, remainder = parser.parse_known_args()
    BINARY = arguments.binary.resolve(strict=True) if arguments.binary else None
    unittest.main(argv=[sys.argv[0], *remainder])
