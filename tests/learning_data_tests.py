"""Regress self-play provenance, adjudication and leakage without long matches."""

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
import collect_learning_data as DATA


def sample(board, split="train", family="family", initial=chess.STARTING_FEN):
    return {"game_id": family, "family": family, "split": split, "initial_fen": initial,
            "history": [move.uci() for move in board.move_stack],
            "fen": board.fen(en_passant="fen")}


def info(board=None, cp=30, depth=2, nodes=100, pv=None):
    board = board or chess.Board()
    return {"score": chess.engine.PovScore(chess.engine.Cp(cp), board.turn),
            "depth": depth, "nodes": nodes,
            "pv": pv or [next(iter(board.legal_moves))]}


class LearningDataPureTests(unittest.TestCase):
    def test_actual_sampling_offsets_cover_both_turns_without_synthetic_positions(self):
        history = "e2e4 e7e5 g1f3 b8c6 f1b5 a7a6 b5a4 g8f6 e1g1 f8e7 f1e1 b7b5 a4b3 d7d6 c2c3 e8g8 h2h3".split()
        expected = {1: list(range(18)), 2: [0, 3, 4, 7, 8, 11, 12, 15, 16],
                    3: [0, 3, 6, 9, 12, 15], 4: [0, 5, 8, 13, 16]}
        for interval, actual_plies in expected.items():
            with self.subTest(interval=interval):
                selected = [ply for ply in range(18) if DATA.should_sample(ply, interval)]
                self.assertEqual(selected, actual_plies)
                self.assertEqual(len({ply // interval for ply in selected}), len(selected))
                boards = [DATA.replay_position(chess.STARTING_FEN, history[:ply]) for ply in selected]
                self.assertEqual({board.turn for board in boards}, {chess.WHITE, chess.BLACK})
                for ply, board in zip(selected, boards):
                    self.assertEqual([move.uci() for move in board.move_stack], history[:ply])
                    self.assertEqual(board.turn, ply % 2 == 0)
                self.assertEqual(selected, [ply for ply in range(18) if DATA.should_sample(ply, interval)])

    def test_sampling_rejects_invalid_intervals_and_relative_plies(self):
        for ply, interval in ((-1, 4), (True, 4), (0, 0), (0, -1), (0, True), (0, 2.5)):
            with self.subTest(ply=ply, interval=interval), self.assertRaises(ValueError):
                DATA.should_sample(ply, interval)

    def test_fixture_legal_unique_families_and_predefined_splits(self):
        data = DATA.load_openings(DATA.DEFAULT_OPENINGS)
        self.assertEqual(len(data["openings"]), 16)
        self.assertEqual(len({row["family"] for row in data["openings"]}), 16)
        self.assertEqual({split: sum(row["split"] == split for row in data["openings"])
                          for split in DATA.SPLITS}, {"train": 10, "validation": 3, "test": 3})
        for opening in data["openings"]:
            self.assertEqual(len(opening["moves"]), 8)

    def test_opening_family_cannot_leak_into_a_second_split(self):
        data = {"schema": "regicide.learning.openings.v1", "openings": [
            {"id": "a", "name": "a", "family": "same", "split": "train", "moves": []},
            {"id": "b", "name": "b", "family": "same", "split": "test", "moves": []}]}
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "openings.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "family"):
                DATA.load_openings(path)
            data["openings"][1]["family"] = "another"
            data["openings"][1]["moves"] = ["e2e5"]
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "illegal"):
                DATA.load_openings(path)

    def test_full_history_retains_claim_context_and_disallows_post_claim_moves(self):
        cycle = ["g1f3", "g8f6", "f3g1", "f6g8"] * 2
        board = DATA.replay_position(chess.STARTING_FEN, cycle[:-1])
        self.assertTrue(board.can_claim_threefold_repetition())
        self.assertFalse(chess.Board(board.fen()).can_claim_threefold_repetition())
        with self.assertRaisesRegex(ValueError, "terminal/claimable"):
            DATA.replay_position(chess.STARTING_FEN, cycle)
        with self.assertRaises(ValueError):
            DATA.replay_position("bad fen", [])
        with self.assertRaisesRegex(ValueError, "illegal"):
            DATA.replay_position(chess.STARTING_FEN, ["e2e5"])

    def test_seed_controls_job_order_random_augmentation_and_saved_history(self):
        openings = DATA.load_openings(DATA.DEFAULT_OPENINGS)["openings"]
        jobs = DATA.seeded_jobs(openings, 2, 8)
        self.assertEqual(jobs, DATA.seeded_jobs(openings, 2, 8))
        self.assertNotEqual(jobs, DATA.seeded_jobs(openings, 2, 9))
        opening = openings[0]
        first = DATA.diversify_opening(opening, 123)
        second = DATA.diversify_opening(opening, 123)
        self.assertEqual(first[1:], second[1:])
        self.assertEqual(len(first[2]), 2)
        self.assertEqual(first[1], opening["moves"] + first[2])
        replayed = DATA.replay_position(chess.STARTING_FEN, first[1])
        self.assertEqual(replayed.fen(), first[0].fen())

    def test_canonical_key_ignores_clocks_and_ineffective_ep_but_keeps_turn_castling(self):
        board = chess.Board()
        board.push_uci("e2e4")
        no_ep = chess.Board(board.fen().replace("0 1", "19 60"))
        self.assertEqual(DATA.canonical_position_key(board), DATA.canonical_position_key(no_ep))
        other_turn = chess.Board(board.fen())
        other_turn.turn = not board.turn
        self.assertNotEqual(DATA.canonical_position_key(board), DATA.canonical_position_key(other_turn))
        no_castling = chess.Board(board.fen())
        no_castling.castling_rights = 0
        self.assertNotEqual(DATA.canonical_position_key(board), DATA.canonical_position_key(no_castling))

    def test_transposed_positions_are_removed_from_all_overlapping_splits(self):
        one = DATA.replay_position(chess.STARTING_FEN, ["g1f3", "g8f6", "g2g3", "g7g6"])
        two = DATA.replay_position(chess.STARTING_FEN, ["g2g3", "g7g6", "g1f3", "g8f6"])
        self.assertEqual(DATA.canonical_position_key(one), DATA.canonical_position_key(two))
        rows = [sample(one), sample(two, "test", "testfamily"), sample(chess.Board(), "train", "third")]
        filtered, exclusions = DATA.remove_cross_split_positions(rows)
        self.assertEqual(len(filtered), 1)
        self.assertEqual(exclusions["canonical_groups"], 1)
        self.assertEqual(exclusions["excluded_positions"], 2)
        rows[0]["fen"] = chess.STARTING_FEN
        with self.assertRaisesRegex(ValueError, "history and FEN"):
            DATA.remove_cross_split_positions(rows)

    def test_identical_neural_inputs_cannot_cross_splits_despite_different_fens(self):
        white = chess.Board("4k3/8/8/8/8/8/P7/4K3 w - - 0 1")
        mirrored = chess.Board("4k3/p7/8/8/8/8/8/4K3 b - - 0 1")
        rights = chess.Board()
        no_rights = chess.Board()
        no_rights.castling_rights = 0
        for first, second in ((white, mirrored), (rights, no_rights)):
            self.assertNotEqual(DATA.canonical_position_key(first), DATA.canonical_position_key(second))
            self.assertEqual(DATA.neural_feature_key(first), DATA.neural_feature_key(second))
        rows = [sample(white, "train", "one", white.fen()),
                sample(mirrored, "test", "two", mirrored.fen()),
                sample(rights, "train", "three", rights.fen()),
                sample(no_rights, "validation", "four", no_rights.fen())]
        filtered, exclusions = DATA.remove_cross_split_positions(rows)
        self.assertEqual(filtered, [])
        self.assertEqual(exclusions["canonical_groups"], 0)
        self.assertEqual(exclusions["neural_feature_groups"], 2)
        self.assertEqual(exclusions["neural_feature_excluded_positions"], 4)
        self.assertEqual(exclusions["excluded_positions"], 4)

    def test_material_and_outcome_labels_are_side_to_move_relative(self):
        board = chess.Board("4k3/8/8/8/8/8/P7/4K3 b - - 0 1")
        self.assertEqual(DATA.material_cp_stm(board), -100)
        board.turn = chess.WHITE
        self.assertEqual(DATA.material_cp_stm(board), 100)
        self.assertEqual(DATA.outcome_stm("1-0", "w"), 1)
        self.assertEqual(DATA.outcome_stm("1-0", "b"), -1)
        self.assertEqual(DATA.outcome_stm("0-1", "b"), 1)
        self.assertEqual(DATA.outcome_stm("1/2-1/2", "w"), 0)
        with self.assertRaisesRegex(ValueError, "unfinished"):
            DATA.outcome_stm("*", "w")

    def test_terminal_claimable_and_check_roots_are_excluded(self):
        mate = DATA.replay_position(chess.STARTING_FEN, ["f2f3", "e7e5", "g2g4", "d8h4"])
        self.assertEqual(DATA.sample_exclusion(mate), "terminal")
        draw = DATA.replay_position(chess.STARTING_FEN,
                                    ["g1f3", "g8f6", "f3g1", "f6g8", "g1f3", "g8f6", "f3g1"])
        self.assertEqual(DATA.sample_exclusion(draw), "claimable_draw")
        check = chess.Board("4k3/8/8/8/8/8/4r3/4K3 w - - 0 1")
        self.assertEqual(DATA.sample_exclusion(check), "root_in_check")
        self.assertIsNone(DATA.sample_exclusion(chess.Board()))

    def test_raw_exact_score_replaces_stale_accumulated_bounds(self):
        board = chess.Board()
        parsed = info(board)
        parsed["upperbound"] = True
        event = DATA.parse_score_event("info depth 2 score cp 30 nodes 100 pv g1h3")
        label, reason = DATA.exact_cp_info(board, parsed, event)
        self.assertIsNone(reason)
        self.assertEqual(label["teacher_cp_stm"], 30)
        bound = DATA.parse_score_event("info depth 2 score cp 30 upperbound nodes 100")
        self.assertEqual(DATA.exact_cp_info(board, parsed, bound)[1], "bound_score")
        self.assertIsNone(DATA.parse_score_event("info string score cp 999"))
        self.assertIsNone(DATA.parse_score_event("info multipv 2 score cp 99"))
        with self.assertRaisesRegex(ValueError, "conflicting"):
            DATA.parse_score_event("info score cp 30 upperbound lowerbound")

    def test_mate_incomplete_depth_illegal_pv_and_score_mismatch_not_training_labels(self):
        board = chess.Board()
        event = DATA.parse_score_event("info score cp 30")
        self.assertEqual(DATA.exact_cp_info(board, info(depth=0), event)[1], "no_completed_depth")
        mate = info()
        mate["score"] = chess.engine.PovScore(chess.engine.Mate(3), chess.WHITE)
        self.assertEqual(DATA.exact_cp_info(board, mate, DATA.parse_score_event("info score mate 3"))[1],
                         "mate_score")
        with self.assertRaisesRegex(ValueError, "PV"):
            DATA.exact_cp_info(board, info(pv=[chess.Move.from_uci("e2e5")]), event)
        with self.assertRaisesRegex(ValueError, "disagree"):
            DATA.exact_cp_info(board, info(cp=31), event)

    def test_existing_output_directory_is_never_reused(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "dataset"
            DATA.reserve_output(output)
            sentinel = output / "precious.txt"
            sentinel.write_text("preserve", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                DATA.reserve_output(output)
            self.assertEqual(sentinel.read_text(), "preserve")


class FakeEngine:
    def __init__(self, move=None, failing=False):
        self.move = move
        self.failing = failing
        self.id = {"name": "Regicide test fixture"}
        self.options = {name: SimpleNamespace(type="check", default=True, min=None, max=None, var=[])
                        for name in DATA.OPTIONS}
        self.protocol = SimpleNamespace(last_score_event=None)
        self.calls = []
        self.configuration_calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def configure(self, options):
        self.configured = options
        self.configuration_calls.append(dict(options))

    def ping(self):
        pass

    def play(self, board, limit, **kwargs):
        self.calls.append({"history": list(board.move_stack), "limit": limit, **kwargs})
        if self.failing:
            raise RuntimeError("synthetic communication failure")
        move = chess.Move.from_uci(self.move) if self.move else next(iter(board.legal_moves))
        self.protocol.last_score_event = DATA.parse_score_event("info depth 2 score cp 30 nodes 100")
        return SimpleNamespace(move=move, info=info(board, pv=[move]))


class CollectorContractTests(unittest.TestCase):
    def setup_args(self, root, moves, max_plies):
        root = Path(root)
        binary = root / "regicide-dummy"
        binary.write_bytes(b"not an executable; mocked UCI transport")
        opening_file = root / "openings.json"
        opening_file.write_text(json.dumps({"schema": "regicide.learning.openings.v1", "openings": [
            {"id": "fixture", "family": "fixture", "name": "fixture", "split": "train", "moves": moves}]}),
            encoding="utf-8")
        return argparse.Namespace(binary=binary, output=root / "new-dataset", seed=10,
                                  games_per_opening=1, nodes=100, max_plies=max_plies,
                                  sample_every=1, teacher_nodes=200, openings=opening_file,
                                  source_commit=None)

    def run_mocked(self, args, player, teacher):
        original = DATA.diversify_opening
        with patch.object(DATA.LearningEngine, "popen_uci", side_effect=[player, teacher]), \
                patch.object(DATA, "diversify_opening", side_effect=lambda opening, seed:
                             original(opening, seed, random_plies=0)), \
                patch.object(DATA, "checkout_provenance", return_value={"scope": "mock"}):
            return DATA.collect(args)

    def test_ply_cap_is_unfinished_not_draw_and_teacher_is_not_called(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = self.setup_args(temporary, [], 1)
            player, teacher = FakeEngine("e2e4"), FakeEngine()
            report = self.run_mocked(args, player, teacher)
            self.assertEqual(report["status"], "complete")
            self.assertEqual(report["completed_games"], 0)
            self.assertEqual(report["unfinished_games"], 1)
            self.assertEqual(report["positions"], 0)
            self.assertEqual(report["exclusions"]["unfinished_game_positions"], 1)
            self.assertEqual(teacher.calls, [])
            game = json.loads((args.output / "games.jsonl").read_text())
            self.assertEqual(game["result"], "*")
            self.assertEqual(game["termination"], "PLY_CAP_UNFINISHED")
            self.assertEqual(game["history"], ["e2e4"])
            self.assertEqual((args.output / "positions.jsonl").read_text(), "")

    def test_completed_game_supplies_perspective_correct_outcome_and_full_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = self.setup_args(temporary, ["f2f3", "e7e5", "g2g4"], 8)
            player, teacher = FakeEngine("d8h4"), FakeEngine("d8h4")
            report = self.run_mocked(args, player, teacher)
            self.assertEqual(report["completed_games"], 1)
            self.assertEqual(report["positions"], 1)
            position = json.loads((args.output / "positions.jsonl").read_text())
            self.assertEqual(position["turn"], "b")
            self.assertEqual(position["outcome_stm"], 1)
            self.assertEqual(position["history"], ["f2f3", "e7e5", "g2g4"])
            self.assertEqual(report["counts_by_turn"]["train"], {"w": 0, "b": 1})
            self.assertEqual(report["positions_by_turn"], {"b": 1})
            self.assertEqual(len(teacher.calls[0]["history"]), 3)
            self.assertEqual(teacher.calls[0]["limit"].nodes, 200)
            self.assertIsNot(teacher.calls[0]["game"], player.calls[0]["game"])
            for name, digest in report["files_sha256"].items():
                self.assertEqual(DATA.sha256_file(args.output / name), digest)

    def test_even_interval_collects_real_white_and_black_queries_in_a_completed_game(self):
        class SequencePlayer(FakeEngine):
            def play(self, board, limit, **kwargs):
                self.move = "e2e4 e7e5 d1h5 b8c6 f1c4 g8f6 h5f7".split()[len(board.move_stack)]
                return super().play(board, limit, **kwargs)

        with tempfile.TemporaryDirectory() as temporary:
            args = self.setup_args(temporary, [], 12)
            args.sample_every = 2
            player, teacher = SequencePlayer(), FakeEngine()
            report = self.run_mocked(args, player, teacher)
            samples = [json.loads(line) for line in (args.output / "positions.jsonl").read_text().splitlines()]
            self.assertEqual(report["completed_games"], 1)
            self.assertEqual([sample["ply"] for sample in samples], [0, 3, 4])
            self.assertEqual([sample["turn"] for sample in samples], ["w", "b", "w"])
            self.assertEqual(report["counts_by_turn"]["train"], {"w": 2, "b": 1})
            self.assertEqual([len(query["history"]) for query in teacher.calls], [0, 3, 4])
            for sample in samples:
                board = DATA.replay_position(sample["initial_fen"], sample["history"])
                self.assertEqual(board.fen(en_passant="fen"), sample["fen"])
                self.assertEqual(sample["outcome_stm"], 1 if board.turn else -1)
            self.assertIn("no synthetic turn flips", report["label_policy"]["sampling_turns"])

    def test_engine_failure_preserves_partial_report_as_failed(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = self.setup_args(temporary, [], 4)
            with self.assertRaisesRegex(RuntimeError, "synthetic"):
                self.run_mocked(args, FakeEngine(failing=True), FakeEngine())
            report = json.loads((args.output / "manifest.json").read_text())
            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["games_finished"], 0)
            self.assertEqual(report["errors"][0]["type"], "RuntimeError")
            self.assertEqual((args.output / "positions.jsonl").read_text(), "")

    def test_champion_snapshot_plays_both_colors_while_teacher_stays_classical(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = self.setup_args(temporary, ["f2f3", "e7e5", "g2g4"], 8)
            args.model = ROOT / "models/regicide-small-v1.nn"
            player, teacher = FakeEngine("d8h4"), FakeEngine("d8h4")
            for engine in (player, teacher):
                engine.options.update({name: SimpleNamespace(type="check", default=False, min=None,
                                                            max=None, var=[])
                                       for name in ("EvalFile", "UseNeuralEvaluation")})
            report = self.run_mocked(args, player, teacher)
            snapshot = args.output / "selfplay-model.nn"
            digest = DATA.sha256_file(args.model)
            self.assertEqual(DATA.sha256_file(snapshot), digest)
            self.assertEqual(report["engine"]["selfplay_model"]["sha256"], digest)
            self.assertTrue(report["engine"]["optional_options"]["UseNeuralEvaluation"])
            self.assertIn({"EvalFile": str(snapshot), "UseNeuralEvaluation": True}, player.configuration_calls)
            self.assertIn({"UseNeuralEvaluation": False}, teacher.configuration_calls)
            self.assertNotIn({"EvalFile": str(snapshot), "UseNeuralEvaluation": True}, teacher.configuration_calls)
            game = json.loads((args.output / "games.jsonl").read_text())
            self.assertEqual(game["selfplay_model_sha256"], digest)
            self.assertIn(digest, (args.output / "games.pgn").read_text())

    def test_invalid_champion_is_rejected_before_reserving_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = self.setup_args(temporary, [], 1)
            args.model = Path(temporary) / "bad.nn"
            args.model.write_text("invalid", encoding="ascii")
            with self.assertRaisesRegex(ValueError, "header"):
                self.run_mocked(args, FakeEngine(), FakeEngine())
            self.assertFalse(args.output.exists())


if __name__ == "__main__":
    unittest.main()
