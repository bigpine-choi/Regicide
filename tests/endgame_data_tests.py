"""Endgame collection contracts: legal starts, real outcomes and input isolation."""

import argparse
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
import collect_endgame_data as ENDGAME
import collect_learning_data as DATA
import learning_arena as ARENA
from train_neural import load_dataset


class FakeEngine:
    def __init__(self):
        self.id = {"name": "Regicide endgame test transport"}
        self.options = {name: SimpleNamespace(type="check", default=True, min=None, max=None, var=[])
                        for name in DATA.OPTIONS}
        self.protocol = SimpleNamespace(last_score_event=None)
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def configure(self, options):
        pass

    def play(self, board, limit, **kwargs):
        self.calls.append((board.copy(stack=True), limit, kwargs))
        move = next(iter(board.legal_moves))
        for candidate in board.legal_moves:
            board.push(candidate)
            mate = board.is_checkmate()
            board.pop()
            if mate:
                move = candidate
                break
        self.protocol.last_score_event = DATA.parse_score_event(
            f"info depth 2 score cp 30 nodes 100 pv {move.uci()}")
        return SimpleNamespace(move=move, info={"score": chess.engine.PovScore(chess.engine.Cp(30), board.turn),
                                               "depth": 2, "nodes": 100, "pv": [move]})


class EndgameSeedTests(unittest.TestCase):
    def test_self_authored_fixture_has_independent_family_splits_and_both_turns(self):
        document, openings = ENDGAME.load_seeds(ENDGAME.DEFAULT_SEEDS)
        self.assertEqual(len(document["families"]), 24)
        self.assertEqual(len(openings["openings"]), 48)
        self.assertEqual({split: len(ENDGAME.load_seeds(ENDGAME.DEFAULT_SEEDS, split)[1]["openings"])
                          for split in DATA.SPLITS}, {"train": 32, "validation": 8, "test": 8})
        self.assertEqual(len(ENDGAME.load_seeds(ENDGAME.DEFAULT_SEEDS, "heldout")[1]["openings"]), 16)
        family_turns = {}
        for row in openings["openings"]:
            board = DATA.replay_position(row["initial_fen"], row["moves"])
            self.assertTrue(board.is_valid())
            self.assertFalse(board.is_check())
            self.assertIsNone(board.outcome(claim_draw=True))
            self.assertLessEqual(len(board.piece_map()), 12)
            family_turns.setdefault(row["family"], set()).add(board.turn)
        self.assertTrue(all(turns == {True, False} for turns in family_turns.values()))
        train = [family for family in document["families"] if family["split"] == "train"]
        self.assertEqual(sum(family["id"].startswith("rook-") or family["id"] == "loose-rook"
                             for family in train), 9)  # Includes one rook-versus-minor family.

    def test_family_assignment_rejects_identical_neural_input_in_a_different_split(self):
        document, _ = ENDGAME.load_seeds(ENDGAME.DEFAULT_SEEDS)
        document["families"][16]["starts"][0] = document["families"][0]["starts"][0]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "seeds.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "crosses splits"):
                ENDGAME.load_seeds(path)

    def test_seed_definitions_reject_invalid_terminal_and_single_turn_families(self):
        original, _ = ENDGAME.load_seeds(ENDGAME.DEFAULT_SEEDS)
        for fen, expected in (("bad", "FEN"), ("7k/8/8/8/8/8/8/K7 w - - 0 1", "nonterminal"),
                              (original["families"][0]["starts"][0], "both actual")):
            document = copy.deepcopy(original)
            document["families"][0]["starts"][1] = fen
            with tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "seeds.json"
                path.write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, expected):
                    ENDGAME.load_seeds(path)

    def test_cli_uses_existing_exact_node_collector_limits(self):
        args = ENDGAME.parse_args(["--binary", "engine", "--output", "out", "--seed", "7",
                                   "--games-per-opening", "4", "--nodes", "1000", "--teacher-nodes", "10000",
                                   "--max-plies", "240", "--sample-every", "4"])
        self.assertEqual(args.split, "train")
        self.assertEqual(args.teacher_nodes, 10000)
        self.assertEqual(args.exclude_dataset, [])
        _, openings = ENDGAME.load_seeds(args.seeds, args.split)
        first = DATA.seeded_jobs(openings["openings"], 4, 7)
        self.assertEqual(len(first), 128)
        self.assertEqual(first, DATA.seeded_jobs(openings["openings"], 4, 7))
        self.assertNotEqual(first, DATA.seeded_jobs(openings["openings"], 4, 8))


class EndgameInventoryTests(unittest.TestCase):
    def test_feature_exclusion_catches_information_absent_from_neural_inputs(self):
        inventory = ENDGAME.ExclusionInventory()
        board = chess.Board()
        inventory.add_board(board)
        self.assertEqual(inventory.reason(board), "reserved_canonical_input")
        board.castling_rights = chess.BB_EMPTY
        self.assertEqual(inventory.reason(board), "reserved_neural_input")

    def test_arena_inventory_includes_every_drawn_full_opening_prefix(self):
        inventory = ENDGAME.ExclusionInventory()
        inventory.add_openings(ARENA.DEFAULT_OPENINGS)
        sampled = ARENA.load_openings(ARENA.DEFAULT_OPENINGS, 100, 49301)
        for row in sampled["openings"]:
            board = chess.Board()
            for move in row["moves"]:
                board.push_uci(move)
                self.assertIsNotNone(inventory.reason(board))
        inventory.verify_sources()

    def test_dataset_heldout_exclusion_uses_all_plies_not_only_sampled_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            games = [{"initial_fen": chess.STARTING_FEN, "history": ["e2e4", "e7e5"], "split": "test"},
                     {"initial_fen": chess.STARTING_FEN, "history": ["d2d4"], "split": "train"}]
            DATA.write_jsonl(directory / "games.jsonl", games)
            DATA.checkpoint_json(directory / "manifest.json", {
                "schema": DATA.SCHEMA, "games_path": "games.jsonl", "files_sha256": {
                    "games.jsonl": DATA.sha256_file(directory / "games.jsonl")}})
            inventory = ENDGAME.ExclusionInventory()
            inventory.add_dataset(directory, heldout_only=True)
            self.assertIsNotNone(inventory.reason(DATA.replay_position(chess.STARTING_FEN, ["e2e4"])))
            self.assertIsNone(inventory.reason(DATA.replay_position(chess.STARTING_FEN, ["d2d4"])))
            self.assertEqual(len(inventory.sources), 2)

    def test_actual_arena_json_uci_moves_and_jsonl_both_supported(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            row = {"initial_fen": chess.STARTING_FEN, "uci_moves": ["g1f3", "b8c6"],
                   "result": "ignored by inventory", "candidate_points": "ignored"}
            path = directory / "arena.json"
            path.write_text(json.dumps({"games": [row]}), encoding="utf-8")
            inventory = ENDGAME.ExclusionInventory()
            inventory.add_games(path)
            board = DATA.replay_position(chess.STARTING_FEN, row["uci_moves"])
            self.assertIsNotNone(inventory.reason(board))
            path2 = directory / "games.jsonl"
            DATA.write_jsonl(path2, [{"initial_fen": chess.STARTING_FEN, "history": row["uci_moves"]}])
            other = ENDGAME.ExclusionInventory()
            other.add_games(path2)
            self.assertEqual(inventory.features, other.features)

    def test_reserved_source_replacement_and_path_escape_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path = directory / "games.jsonl"
            DATA.write_jsonl(path, [{"initial_fen": chess.STARTING_FEN, "history": []}])
            inventory = ENDGAME.ExclusionInventory()
            inventory.add_games(path)
            path.write_text("replacement", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "changed"):
                inventory.verify_sources()
            inner = directory / "dataset"
            inner.mkdir()
            DATA.checkpoint_json(inner / "manifest.json", {"schema": DATA.SCHEMA, "games_path": "../games.jsonl"})
            with self.assertRaisesRegex(ValueError, "escapes"):
                ENDGAME.ExclusionInventory().add_dataset(inner)

    def test_whole_trajectory_cross_split_intersection_excludes_unsampled_collision(self):
        board = DATA.replay_position(chess.STARTING_FEN, ["e2e4"])
        sample = {"initial_fen": chess.STARTING_FEN, "history": ["e2e4"], "split": "train"}
        games = [{"initial_fen": chess.STARTING_FEN, "history": ["e2e4"], "split": "train"},
                 {"initial_fen": board.fen(), "history": ["e7e5"], "split": "test"}]
        accepted, report = ENDGAME.trajectory_leakage(games, [sample])
        self.assertEqual(accepted, [])
        self.assertEqual(report["excluded_samples"], {"canonical": 1})


class EndgameCollectionTests(unittest.TestCase):
    def setup_args(self, directory):
        directory = Path(directory)
        binary = directory / "mock-engine"
        binary.write_bytes(b"mocked transport; no engine run")
        seeds = directory / "seeds.json"
        document = {"schema": "regicide.endgame.seeds.v1", "families": [{
            "id": "mate-test", "split": "train", "theme": "mock transport mate outcomes",
            "starts": ["7k/8/5KQ1/8/8/8/8/8 w - - 0 1", "8/8/8/8/8/1qk5/8/K7 b - - 0 1"]}]}
        seeds.write_text(json.dumps(document), encoding="utf-8")
        return argparse.Namespace(binary=binary, output=directory / "dataset", seeds=seeds,
                                  split="train", seed=117, games_per_opening=1, nodes=100,
                                  teacher_nodes=200, max_plies=10, sample_every=1,
                                  source_commit=None, model=None, exclude_dataset=[],
                                  exclude_heldout_dataset=[], exclude_games=[], exclude_openings=[])

    def collect_mocked(self, args, player, teacher):
        original = DATA.diversify_opening
        with patch.object(DATA.LearningEngine, "popen_uci", side_effect=[player, teacher]), \
                patch.object(DATA, "diversify_opening", side_effect=lambda opening, seed:
                             original(opening, seed, random_plies=0)), \
                patch.object(DATA, "checkout_provenance", return_value={"scope": "mock transport"}):
            return ENDGAME.collect(args)

    def test_real_mate_outcomes_full_histories_original_schema_and_perspectives(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = self.setup_args(temporary)
            report = self.collect_mocked(args, FakeEngine(), FakeEngine())
            self.assertEqual(report["schema"], DATA.SCHEMA)
            self.assertEqual(report["completed_games"], 2)
            self.assertEqual(report["positions"], 2)
            _, metadata = load_dataset(args.output / "manifest.json", allow_empty_splits=True)
            self.assertEqual(metadata["counts"]["train"]["samples"], 2)
            samples = [json.loads(line) for line in (args.output / "positions.jsonl").read_text().splitlines()]
            self.assertEqual({row["turn"] for row in samples}, {"w", "b"})
            self.assertTrue(all(row["outcome_stm"] == 1 for row in samples))
            for name, expected in report["files_sha256"].items():
                self.assertEqual(DATA.sha256_file(args.output / name), expected)

    def test_reserved_sample_is_excluded_before_teacher_query_without_changing_game(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = self.setup_args(temporary)
            blocked = Path(temporary) / "reserved-games.jsonl"
            seed = json.loads(args.seeds.read_text())["families"][0]["starts"][0]
            DATA.write_jsonl(blocked, [{"initial_fen": seed, "history": []}])
            args.exclude_games = [blocked]
            teacher = FakeEngine()
            report = self.collect_mocked(args, FakeEngine(), teacher)
            self.assertEqual(report["completed_games"], 2)
            self.assertEqual(report["positions"], 1)
            self.assertEqual(len(teacher.calls), 1)
            self.assertEqual(report["exclusions"]["reserved_canonical_input"], 1)

    def test_existing_output_and_sidecar_are_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = self.setup_args(temporary)
            args.output.mkdir()
            sentinel = args.output / "sentinel"
            sentinel.write_text("preserve")
            with self.assertRaises(FileExistsError):
                ENDGAME.collect(args)
            self.assertEqual(sentinel.read_text(), "preserve")

    def test_seed_change_marks_dataset_failed_and_training_refuses_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = self.setup_args(temporary)

            class MutatingPlayer(FakeEngine):
                def play(self, *values, **kwargs):
                    args.seeds.write_text(args.seeds.read_text() + "\n", encoding="utf-8")
                    return super().play(*values, **kwargs)

            with self.assertRaisesRegex(ValueError, "changed during collection"):
                self.collect_mocked(args, MutatingPlayer(), FakeEngine())
            manifest = json.loads((args.output / "manifest.json").read_text())
            self.assertEqual(manifest["status"], "failed")
            with self.assertRaisesRegex(ValueError, "not completed"):
                load_dataset(args.output / "manifest.json", allow_empty_splits=True)


if __name__ == "__main__":
    unittest.main()
