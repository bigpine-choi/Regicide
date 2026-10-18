"""Endgame definitions, train-only isolation and conservative teacher evidence."""

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
import collect_learning_data as DATA
import collect_endgame_data as ENDATA
import diagnose_endgames as END


def game(split="train", family="italian"):
    return {"game_id": "endgame-fixture", "family": family, "split": split,
            "initial_fen": "8/7k/5KQ1/8/8/8/8/8 b - - 0 1",
            "history": ["h7h8", "g6g7"], "result": "1-0", "completed": True,
            "selfplay_searches": [
                {"ply": 0, "move": "h7h8", "cp_stm": 0, "raw_score_event": {"kind": "cp", "value": 0}},
                {"ply": 1, "move": "g6g7", "cp_stm": 500, "raw_score_event": {"kind": "mate", "value": 1}}]}


class Teacher:
    def __init__(self, *, mate=False, bound=False, depth=4, nodes=1000, ignored=False):
        self.id = {"name": "Regicide test fixture"}
        self.options = {name: object() for name in (*DATA.OPTIONS, "UseNeuralEvaluation", "Clear Hash")}
        self.protocol = SimpleNamespace(last_score_event=None)
        self.calls, self.configurations = [], []
        self.mate, self.bound, self.depth, self.nodes, self.ignored = mate, bound, depth, nodes, ignored

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def configure(self, options):
        self.configurations.append(dict(options))

    def play(self, board, limit, **kwargs):
        self.calls.append({"board": board.copy(stack=True), "limit": limit, **kwargs})
        forced = kwargs.get("root_moves")
        move = (next(iter(board.legal_moves)) if not forced or self.ignored else forced[0])
        cp = -300 if forced else 100
        text = "mate -1" if self.mate else f"cp {cp}"
        text += " lowerbound" if self.bound else ""
        raw = f"info depth {self.depth} score {text} nodes {self.nodes} pv {move.uci()}"
        self.protocol.last_score_event = DATA.parse_score_event(raw)
        score = chess.engine.Mate(-1) if self.mate else chess.engine.Cp(cp)
        return SimpleNamespace(move=move, info={"score": chess.engine.PovScore(score, board.turn),
                                               "depth": self.depth, "nodes": self.nodes, "pv": [move]})


class EndgameDiagnosisTests(unittest.TestCase):
    def fixture(self, directory, games=None):
        source = Path(directory) / "data"
        source.mkdir()
        DATA.write_jsonl(source / "games.jsonl", games or [game()])
        DATA.write_jsonl(source / "positions.jsonl", [])
        DATA.checkpoint_json(source / "manifest.json", {
            "schema": DATA.SCHEMA, "status": "complete", "games_path": "games.jsonl",
            "positions_path": "positions.jsonl", "limits": {"selfplay_nodes": 100},
            "files_sha256": {name: DATA.sha256_file(source / name) for name in ("games.jsonl", "positions.jsonl")}})
        binary = Path(directory) / "regicide"
        binary.write_bytes(b"mocked binary")
        return source, binary, Path(directory) / "diagnosis"

    def comparisons(self):
        def result(move, cp, depth=4):
            return {"move": move, "cp_stm": cp, "depth": depth, "depth_completed": True}
        return [{"requested_nodes": nodes, "best": result("e2e4", 100),
                 "played": result("d2d4", -100)} for nodes in (200000, 50000, 200000)]

    def endgame_source(self, temporary):
        fen = "7k/8/5KQ1/8/8/8/8/8 w - - 0 1"
        value = {"game_id": "collected-endgame", "family": "endgame-v1-fixture", "split": "train",
                 "initial_fen": fen, "history": ["g6g7"], "result": "1-0", "completed": True,
                 "selfplay_searches": [{"ply": 0, "move": "g6g7", "cp_stm": 900}]}
        source, binary, output = self.fixture(temporary, [value])
        seeds = {"schema": "regicide.endgame.seeds.v1", "families": [
            {"id": "fixture", "split": "train", "theme": "unit fixture",
             "starts": [fen, "8/8/8/8/8/5kq1/8/7K b - - 0 1"]}]}
        seed_path = Path(temporary) / "seeds.json"
        DATA.checkpoint_json(seed_path, seeds)
        manifest = json.loads((source / "manifest.json").read_text())
        manifest["endgame"] = {"schema": "regicide.endgame.collection.v1", "selected_split": "train",
                               "seed_file_sha256": DATA.sha256_file(seed_path), "seed_definition": seeds}
        DATA.checkpoint_json(source / "manifest.json", manifest)
        return source, binary, output, seed_path, manifest, value

    def test_endgame_definition_does_not_depend_on_result(self):
        self.assertFalse(END.endgame_context(chess.Board())["is_endgame"])
        sparse = chess.Board("4k3/8/4p3/3P4/8/4K3/8/8 w - - 0 1")
        context = END.endgame_context(sparse)
        self.assertTrue(context["is_endgame"])
        self.assertEqual(context["phase"], 0)
        self.assertEqual(context["piece_count"], 4)
        self.assertIn("pawn_only", context["themes"])
        self.assertFalse(context["themes_are_causes"])
        # A promoted queen can coexist with a sparse low-phase endgame.
        self.assertTrue(END.endgame_context(chess.Board("7k/8/5KQ1/8/8/8/8/8 w - - 0 1"))["is_endgame"])

    def test_passed_pawn_requires_clear_same_and_adjacent_files_ahead(self):
        board = chess.Board("7k/8/4p3/3P4/8/4K3/8/8 w - - 0 1")
        self.assertEqual(END.endgame_context(board)["passed_pawns"]["w"], [])
        board.remove_piece_at(chess.E6)
        context = END.endgame_context(board)
        self.assertEqual(context["passed_pawns"]["w"], ["d5"])
        self.assertEqual(context["advanced_passed_pawns"]["w"], ["d5"])
        board.set_piece_at(chess.H4, chess.Piece(chess.PAWN, chess.BLACK))
        self.assertIn("pawn_race", END.endgame_context(board)["themes"])

    def test_attacked_undefended_is_geometric_context_not_capture_proof(self):
        board = chess.Board("7k/8/8/8/8/5n2/3R4/K7 w - - 0 1")
        context = END.endgame_context(board)
        self.assertEqual(context["attacked_undefended_nonpawns"]["w"], ["d2"])
        self.assertIn("attacked_undefended_nonpawn", context["themes"])
        self.assertFalse(context["themes_are_causes"])
        board.set_piece_at(chess.C1, chess.Piece(chess.BISHOP, chess.WHITE))
        self.assertEqual(END.endgame_context(board)["attacked_undefended_nonpawns"]["w"], [])

    def test_completed_train_loser_decision_only_and_context_precedes_search(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, _, _ = self.fixture(temporary)
            result = END.select_cases([source])
            self.assertEqual(len(result["selected_cases"]), 1)
            row = result["selected_cases"][0]
            self.assertEqual(row["ply"], 0)
            self.assertEqual(row["played_move"], "h7h8")
            self.assertEqual(row["selection"]["rough_loss_cp"], 500)
            self.assertTrue(row["selection"]["mate_threat_after_played"])

    def test_held_out_games_never_generate_feedback_cases(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, _, _ = self.fixture(temporary, [game("validation", "pirc")])
            report = END.select_cases([source])
            self.assertEqual(report["selected_cases"], [])
            self.assertEqual(report["excluded_games"]["held_out_game"], 1)

    def test_relabelled_heldout_or_unknown_family_fails_closed(self):
        for family in ("pirc", "arena-foo", "invented"):
            with self.subTest(family=family), tempfile.TemporaryDirectory() as temporary:
                source, _, _ = self.fixture(temporary, [game("train", family)])
                with self.assertRaisesRegex(ValueError, "whitelist"):
                    END.select_cases([source])

    def test_future_validated_endgame_training_runs_can_be_diagnosed(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, _, _, seed_path, _, _ = self.endgame_source(temporary)
            with patch.object(ENDATA, "DEFAULT_SEEDS", seed_path):
                sources, games, excluded = END.source_games([source, source])
            self.assertEqual(len(games), 1)
            self.assertEqual(games[0]["game"]["family"], "endgame-v1-fixture")
            self.assertEqual(sources[0]["validated_endgame_seed_sha256"], DATA.sha256_file(seed_path))
            self.assertEqual(excluded["duplicate_complete_game"], 1)

    def test_endgame_prefix_does_not_bypass_missing_declaration_or_seed_validation(self):
        for mutation in ("missing", "sha", "definition", "schema", "role"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                source, _, _, seed_path, manifest, _ = self.endgame_source(temporary)
                if mutation == "missing":
                    manifest.pop("endgame")
                elif mutation == "sha":
                    manifest["endgame"]["seed_file_sha256"] = "0" * 64
                elif mutation == "definition":
                    manifest["endgame"]["seed_definition"]["scope"] = "changed"
                elif mutation == "schema":
                    manifest["endgame"]["schema"] = "not-a-collection"
                else:
                    manifest["endgame"]["selected_split"] = "heldout"
                DATA.checkpoint_json(source / "manifest.json", manifest)
                with patch.object(ENDATA, "DEFAULT_SEEDS", seed_path), self.assertRaises(ValueError):
                    END.source_games([source])

    def test_endgame_game_must_match_exact_declared_train_family_and_initial_fen(self):
        for mutation in ("family", "initial_fen", "split"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                source, _, _, seed_path, manifest, value = self.endgame_source(temporary)
                if mutation == "family":
                    value["family"] = "endgame-v1-prefix-only"
                elif mutation == "initial_fen":
                    value["initial_fen"] = value["initial_fen"].replace("0 1", "1 1")
                else:
                    value["split"] = "validation"
                DATA.write_jsonl(source / "games.jsonl", [value])
                manifest["files_sha256"]["games.jsonl"] = DATA.sha256_file(source / "games.jsonl")
                DATA.checkpoint_json(source / "manifest.json", manifest)
                with patch.object(ENDATA, "DEFAULT_SEEDS", seed_path), self.assertRaisesRegex(ValueError, "not a declared training seed"):
                    END.source_games([source])

    def test_duplicate_full_games_count_once_across_cumulative_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, _, _ = self.fixture(temporary)
            report = END.select_cases([source, source])
            self.assertEqual(len(report["inventory"]), 1)
            self.assertEqual(report["excluded_games"]["duplicate_complete_game"], 1)

    def test_renamed_game_id_does_not_hide_duplicate_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            other = game()
            other["game_id"] = "renamed"
            source, _, _ = self.fixture(temporary, [game(), other])
            report = END.select_cases([source])
            self.assertEqual(len(report["inventory"]), 1)

    def test_archived_duplicate_or_wrong_move_rejected(self):
        for mutation in ("duplicate", "move", "boolply"):
            changed = game()
            if mutation == "duplicate":
                changed["selfplay_searches"].append(changed["selfplay_searches"][0])
            elif mutation == "move":
                changed["selfplay_searches"][0]["move"] = "h7h6"
            else:
                changed["selfplay_searches"][0]["ply"] = False
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                END.endgame_decisions(changed)

    def test_search_clears_hash_replays_history_and_requests_same_root(self):
        board = chess.Board()
        board.push_uci("g1f3")
        teacher = Teacher()
        result = END.search(teacher, board, 10000, 4, chess.Move.from_uci("g8f6"))
        call = teacher.calls[0]
        self.assertEqual(call["board"].move_stack, board.move_stack)
        self.assertEqual(call["limit"].depth, 4)
        self.assertEqual(call["limit"].nodes, 10000)
        self.assertEqual(call["root_moves"], [chess.Move.from_uci("g8f6")])
        self.assertEqual(teacher.configurations, [{"Clear Hash": None}])
        self.assertEqual(result["cp_stm"], -300)
        self.assertTrue(result["depth_completed"])

    def test_bound_mate_and_incomplete_search_are_not_exact_finite_evidence(self):
        for teacher in (Teacher(mate=True), Teacher(bound=True), Teacher(depth=3)):
            with self.subTest(teacher=teacher):
                result = END.search(teacher, chess.Board(), 10000, 4)
                if teacher.depth == 3:
                    self.assertFalse(result["depth_completed"])
                else:
                    self.assertIsNone(result["cp_stm"])
                    self.assertIsNotNone(result["raw_score_event"])

    def test_invalid_nodes_depth_or_missing_clear_hash_are_rejected(self):
        for teacher in (Teacher(nodes=20000), Teacher(nodes=0), Teacher(depth=5)):
            with self.subTest(teacher=teacher), self.assertRaises(ValueError):
                END.search(teacher, chess.Board(), 10000, 4)
        teacher = Teacher()
        del teacher.options["Clear Hash"]
        with self.assertRaisesRegex(ValueError, "Clear Hash"):
            END.search(teacher, chess.Board(), 10000, 4)

    def test_ignored_forced_move_rejected(self):
        with self.assertRaisesRegex(ValueError, "ignored"):
            END.search(Teacher(ignored=True), chess.Board(), 10000, 4, chess.Move.from_uci("e2e4"))

    def test_stable_finite_common_depth_and_deeper_agreement_is_conditional_support(self):
        result = END.assess(self.comparisons(), {"played_move": "d2d4", "selfplay_nodes": 1000}, 4)
        self.assertEqual(result["status"], "bounded_teacher_supported_loss")
        self.assertEqual(result["common_depth_loss_cp"], 200)
        self.assertFalse(result["contexts_are_proven_causes"])

    def test_every_support_requirement_fails_closed(self):
        for mutation in ("bound", "depth", "move", "best_cp", "played_cp", "same_move", "loss", "budget"):
            comparisons = self.comparisons()
            case = {"played_move": "d2d4", "selfplay_nodes": 1000}
            if mutation == "bound":
                comparisons[1]["best"]["cp_stm"] = None
            elif mutation == "depth":
                comparisons[0]["played"]["depth_completed"] = False
            elif mutation == "move":
                comparisons[2]["best"]["move"] = "c2c4"
            elif mutation == "best_cp":
                comparisons[2]["best"]["cp_stm"] += 31
            elif mutation == "played_cp":
                comparisons[2]["played"]["cp_stm"] += 31
            elif mutation == "same_move":
                comparisons[0]["best"]["move"] = "d2d4"
            elif mutation == "loss":
                comparisons[0]["played"]["cp_stm"] = 99
            else:
                case["selfplay_nodes"] = 50000
            with self.subTest(mutation=mutation):
                result = END.assess(comparisons, case, 4)
                self.assertEqual(result["status"], "unconfirmed")
                self.assertTrue(result["reasons"])

    def test_already_severely_unfavourable_is_metadata_not_a_selection_or_support_filter(self):
        comparisons = self.comparisons()
        for pair in comparisons:
            pair["best"]["cp_stm"], pair["played"]["cp_stm"] = -600, -800
        result = END.assess(comparisons, {"played_move": "d2d4", "selfplay_nodes": 1000}, 4)
        self.assertEqual(result["status"], "bounded_teacher_supported_loss")
        self.assertTrue(result["already_severely_unfavourable_under_teacher"])
        self.assertEqual(result["common_depth_best_cp_stm"], -600)

    def test_frequency_counts_unique_games_and_never_claims_causes(self):
        row = {"context": {"themes": ["rook_endgame"]}, "unique_game_sha256": "same",
               "family": "italian", "assessment": {"status": "bounded_teacher_supported_loss"}}
        summary = END.theme_summary([row, copy.deepcopy(row)])
        self.assertEqual(summary["rook_endgame"]["supported_positions"], 2)
        self.assertEqual(summary["rook_endgame"]["supported_unique_games"], 1)

    def test_inventory_does_not_launch_engine_or_export_labels(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, binary, output = self.fixture(temporary)
            with patch.object(DATA.LearningEngine, "popen_uci") as start:
                report = END.run_diagnosis([source], binary, output, inventory_only=True)
            start.assert_not_called()
            self.assertEqual(report["status"], "complete")
            self.assertEqual(report["queries_completed"], 0)
            self.assertEqual([path.name for path in output.iterdir()], ["endgame-report.json"])

    def test_full_diagnosis_keeps_six_queries_and_fresh_games(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, binary, output = self.fixture(temporary)
            teacher = Teacher()
            with patch.object(DATA.LearningEngine, "popen_uci", return_value=teacher):
                report = END.run_diagnosis([source], binary, output)
            self.assertEqual(report["queries_completed"], 6)
            self.assertEqual(len({id(call["game"]) for call in teacher.calls}), 6)
            self.assertIn({"UseNeuralEvaluation": False}, teacher.configurations)
            self.assertEqual(report["records"][0]["assessment"]["status"], "unconfirmed")

    def test_failure_checkpoint_preserves_raw_partial_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, binary, output = self.fixture(temporary)
            teacher = Teacher(nodes=300000)
            with patch.object(DATA.LearningEngine, "popen_uci", return_value=teacher):
                with self.assertRaises(ValueError):
                    END.run_diagnosis([source], binary, output)
            report = json.loads((output / "endgame-report.json").read_text())
            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["queries_completed"], 0)
            self.assertIn("active_query", report)

    def test_bad_inputs_rejected_before_output_reserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, binary, output = self.fixture(temporary)
            for kwargs in ({"common_depth": True}, {"common_depth": 65}, {"teacher_nodes": (2000, 1000)},
                           {"teacher_nodes": (999, 2000)}, {"max_games": 0}):
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    END.run_diagnosis([source], binary, output, **kwargs)
                self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
