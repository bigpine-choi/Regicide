"""Bounded loss diagnosis: same-root contexts, safe labels and held-out isolation."""

import copy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import chess
import chess.engine

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import analyze_learning_losses as LOSS
import collect_learning_data as DATA


def loss_game(split="train"):
    history = ["f2f3", "e7e5", "g2g4", "d8h4"]
    return {"game_id": "fools-mate", "opening_id": "fools", "family": "fools",
            "split": split, "initial_fen": chess.STARTING_FEN, "history": history,
            "result": "0-1", "completed": True,
            "selfplay_searches": [
                {"ply": ply, "move": move, "cp_stm": cp, "depth": 1, "nodes": 100,
                 "raw_score_event": DATA.parse_score_event(event)}
                for ply, (move, cp, event) in enumerate(zip(
                    history, [10, -10, 10, None],
                    ["info score cp 10", "info score cp -10", "info score cp 10", "info score mate 1"]))]}


class Teacher:
    def __init__(self, *, forced_cp=-300, mate=False, bound=False, depth=3, failing=False,
                 ignore_forced=False, best_move="e2e4"):
        self.id = {"name": "Regicide test fixture"}
        self.options = {name: object() for name in (*DATA.OPTIONS, "UseNeuralEvaluation", "Clear Hash")}
        self.protocol = SimpleNamespace(last_score_event=None)
        self.calls, self.configurations = [], []
        self.forced_cp, self.mate, self.bound, self.depth = forced_cp, mate, bound, depth
        self.failing, self.ignore_forced = failing, ignore_forced
        self.best_move = best_move

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def configure(self, options):
        self.configurations.append(dict(options))

    def play(self, board, limit, **kwargs):
        self.calls.append({"board": board.copy(stack=True), "limit": limit, **kwargs})
        if self.failing:
            raise RuntimeError("synthetic search transport failure")
        forced = kwargs.get("root_moves")
        move = forced[0] if forced and not self.ignore_forced else chess.Move.from_uci(self.best_move)
        cp = self.forced_cp if forced else 100
        score = chess.engine.Mate(-1) if forced and self.mate else chess.engine.Cp(cp)
        raw = "info depth 3 score " + ("mate -1" if forced and self.mate else f"cp {cp}")
        raw += " lowerbound" if self.bound else ""
        raw += " nodes 1000"
        self.protocol.last_score_event = DATA.parse_score_event(raw)
        return SimpleNamespace(move=move, info={"score": chess.engine.PovScore(score, board.turn),
                                               "depth": self.depth, "nodes": 1000, "pv": [move]})


class LossAnalysisTests(unittest.TestCase):
    def fixture(self, directory, *, split="train", selfplay_nodes=100):
        root = Path(directory)
        source = root / "data"
        source.mkdir()
        game = loss_game(split)
        DATA.write_jsonl(source / "games.jsonl", [game])
        DATA.write_jsonl(source / "positions.jsonl", [])
        DATA.checkpoint_json(source / "manifest.json", {
            "schema": DATA.SCHEMA, "status": "complete", "games_path": "games.jsonl",
            "positions_path": "positions.jsonl", "limits": {"selfplay_nodes": selfplay_nodes},
            "files_sha256": {name: DATA.sha256_file(source / name)
                             for name in ("games.jsonl", "positions.jsonl")}})
        binary = root / "regicide"
        binary.write_bytes(b"mocked binary")
        return source / "manifest.json", binary, root / "analysis"

    def run_fixture(self, directory, teacher=None, *, split="train", nodes=1000, selfplay_nodes=100):
        manifest, binary, output = self.fixture(directory, split=split, selfplay_nodes=selfplay_nodes)
        teacher = teacher or Teacher()
        with patch.object(DATA.LearningEngine, "popen_uci", return_value=teacher):
            report = LOSS.run_analysis(manifest, binary, output, nodes, 1, 100)
        return report, teacher, output

    def test_only_complete_decisive_games_and_loser_decisions_are_selected(self):
        game = loss_game()
        self.assertEqual([row["ply"] for row in LOSS.select_positions(game, 8)], [2, 0])
        self.assertEqual([row["ply"] for row in LOSS.select_positions(game, 1)], [2])
        for completed, result in ((False, "*"), (True, "1/2-1/2")):
            another = {**game, "completed": completed, "result": result}
            self.assertEqual(LOSS.select_positions(another, 8), [])

    def test_finite_deterioration_gets_a_slot_before_the_terminal_mate_sequence(self):
        game = loss_game()
        game["selfplay_searches"][1]["cp_stm"] = 200
        self.assertEqual([row["ply"] for row in LOSS.select_positions(game, 1)], [0])
        self.assertEqual([row["ply"] for row in LOSS.select_positions(game, 2)], [0, 2])

    def test_same_full_history_root_forced_move_and_classical_teacher(self):
        with tempfile.TemporaryDirectory() as temporary:
            report, teacher, output = self.run_fixture(temporary)
            self.assertEqual(report["status"], "complete")
            self.assertEqual(report["queries_completed"], 2)
            self.assertEqual(report["positions"], 1)
            self.assertEqual(len(teacher.calls), 2)
            self.assertEqual(teacher.calls[0]["board"].move_stack, teacher.calls[1]["board"].move_stack)
            self.assertEqual(len(teacher.calls[0]["board"].move_stack), 2)
            self.assertEqual(teacher.calls[1]["root_moves"], [chess.Move.from_uci("g2g4")])
            self.assertIsNot(teacher.calls[0]["game"], teacher.calls[1]["game"])
            self.assertEqual(teacher.calls[0]["limit"].nodes, 1000)
            self.assertIn({"UseNeuralEvaluation": False}, teacher.configurations)
            row = json.loads((output / "positions.jsonl").read_text())
            self.assertEqual(row["outcome_stm"], -1)
            self.assertEqual(row["teacher_cp_stm"], 100)
            self.assertEqual(row["loss_analysis"]["cp_loss"], 400)
            self.assertTrue(row["loss_analysis"]["classification_is_inference"])
            self.assertEqual(report["source_dataset_manifest_sha256"],
                             DATA.sha256_file(Path(temporary) / "data/manifest.json"))
            self.assertEqual(report["positions_sha256"], DATA.sha256_file(output / "positions.jsonl"))

    def test_held_out_diagnosis_never_exports_train_corrections(self):
        for split in ("validation", "test"):
            with self.subTest(split=split), tempfile.TemporaryDirectory() as temporary:
                report, _, output = self.run_fixture(temporary, split=split)
                self.assertEqual(report["positions"], 0)
                self.assertEqual(report["records"][0]["training_exclusion"], "held_out_family")
                self.assertEqual((output / "positions.jsonl").read_text(), "")

    def test_tiny_or_not_deeper_budget_cannot_create_training_labels(self):
        for nodes, selfplay in ((999, 100), (1000, 1000), (1000, 2000)):
            with self.subTest(nodes=nodes, selfplay=selfplay), tempfile.TemporaryDirectory() as temporary:
                report, _, _ = self.run_fixture(temporary, nodes=nodes, selfplay_nodes=selfplay)
                self.assertEqual(report["positions"], 0)
                self.assertEqual(report["records"][0]["training_exclusion"],
                                 "unreliable_or_nonfinite_comparison")

    def test_mate_bound_and_incomplete_depth_are_diagnostic_only(self):
        for teacher in (Teacher(mate=True), Teacher(bound=True), Teacher(depth=0), Teacher(depth=1)):
            with self.subTest(teacher=teacher), tempfile.TemporaryDirectory() as temporary:
                report, _, _ = self.run_fixture(temporary, teacher)
                self.assertEqual(report["positions"], 0)
                if teacher.mate:
                    self.assertEqual(report["records"][0]["mate_transition"]["played_mate_stm"], -1)
                    self.assertIsNone(report["records"][0]["cp_loss"])

    def test_below_threshold_or_negative_gap_is_not_a_correction(self):
        for cp in (50, 200):
            with self.subTest(cp=cp), tempfile.TemporaryDirectory() as temporary:
                report, _, _ = self.run_fixture(temporary, Teacher(forced_cp=cp))
                self.assertEqual(report["positions"], 0)
                self.assertEqual(report["records"][0]["training_exclusion"], "below_loss_threshold")

    def test_different_budget_depth_scores_do_not_turn_the_same_move_into_a_mistake(self):
        with tempfile.TemporaryDirectory() as temporary:
            report, _, _ = self.run_fixture(temporary, Teacher(best_move="g2g4"))
            self.assertEqual(report["positions"], 0)
            self.assertEqual(report["records"][0]["training_exclusion"], "no_better_alternative_found")

    def test_ignored_forced_move_fails_closed_and_keeps_failed_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            manifest, binary, output = self.fixture(temporary)
            with patch.object(DATA.LearningEngine, "popen_uci", return_value=Teacher(ignore_forced=True)):
                with self.assertRaisesRegex(ValueError, "ignored"):
                    LOSS.run_analysis(manifest, binary, output, 1000, 1, 100)
            failed = json.loads((output / "loss-report.json").read_text())
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["errors"][0]["type"], "ValueError")
            self.assertEqual(failed["queries_completed"], 1)
            self.assertEqual(failed["active_query"]["phase"], "forced_played")
            self.assertEqual(failed["active_query"]["best"]["move"], "e2e4")

    def test_transport_failure_is_recorded_without_synthetic_training_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            manifest, binary, output = self.fixture(temporary)
            with patch.object(DATA.LearningEngine, "popen_uci", return_value=Teacher(failing=True)):
                with self.assertRaisesRegex(RuntimeError, "transport"):
                    LOSS.run_analysis(manifest, binary, output, 1000, 1, 100)
            failed = json.loads((output / "loss-report.json").read_text())
            self.assertEqual(failed["positions"], 0)
            self.assertEqual(failed["status"], "failed")

    def test_hash_mismatch_rejected_before_reserving_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            manifest, binary, output = self.fixture(temporary)
            (manifest.parent / "games.jsonl").write_text("{}\n")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                LOSS.run_analysis(manifest, binary, output)
            self.assertFalse(output.exists())

    def test_completed_result_must_match_replayed_rules(self):
        with tempfile.TemporaryDirectory() as temporary:
            manifest, _, _ = self.fixture(temporary)
            source = json.loads(manifest.read_text())
            game = loss_game()
            game["result"] = "1-0"
            DATA.write_jsonl(manifest.parent / "games.jsonl", [game])
            source["files_sha256"]["games.jsonl"] = DATA.sha256_file(manifest.parent / "games.jsonl")
            DATA.checkpoint_json(manifest, source)
            with self.assertRaisesRegex(ValueError, "result disagrees"):
                LOSS.load_source(manifest)

    def test_invalid_search_ply_or_move_cannot_select_a_different_actual_move(self):
        for mutation in ({"ply": 88}, {"ply": True}, {"move": "a2a4"}):
            game = copy.deepcopy(loss_game())
            game["selfplay_searches"][0].update(mutation)
            with self.assertRaises(ValueError):
                LOSS.select_positions(game, 8)

    def test_existing_output_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            manifest, binary, output = self.fixture(temporary)
            output.mkdir()
            sentinel = output / "preserve.txt"
            sentinel.write_text("precious")
            with self.assertRaises(FileExistsError):
                LOSS.run_analysis(manifest, binary, output)
            self.assertEqual(sentinel.read_text(), "precious")

    def test_invalid_budgets_rejected(self):
        for field, value in (("analysis_nodes", 0), ("analysis_nodes", True),
                             ("max_positions_per_game", -1), ("min_loss_cp", 0)):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "positive"):
                LOSS.run_analysis("missing", "missing", "missing", **{field: value})

    def test_query_retains_repetition_stack_and_rejects_illegal_forcing(self):
        board = DATA.replay_position(chess.STARTING_FEN, ["g1f3", "g8f6", "f3g1", "f6g8"])
        teacher = Teacher()
        LOSS.search_root(teacher, board, 1000)
        self.assertEqual(len(teacher.calls[0]["board"].move_stack), 4)
        with self.assertRaisesRegex(ValueError, "illegal"):
            LOSS.search_root(teacher, board, 1000, chess.Move.from_uci("e2e5"))


if __name__ == "__main__":
    unittest.main()
