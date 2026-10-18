"""Nested-data controls, leakage rejection and fixed-policy training integration."""

import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import endgame_learning_curve as CURVE
import train_neural as TRAIN
import chess
import chess.pgn
import numpy as np
from neural_model import feature_key, material_cp_stm
from neural_training_tests import fixture, write_fixture


def write_source(directory, manifest, games, positions):
    write_fixture(directory, manifest, games, positions)
    # Odd spacing and CRLF make byte-preservation checks meaningful.
    (directory / "positions.jsonl").write_bytes(b"".join(
        (json.dumps(row, sort_keys=True, separators=(", ", ": ")) + "\r\n").encode()
        for row in positions))
    pgns = []
    for game in games:
        board = TRAIN.replay(game["initial_fen"], game["history"])
        parsed = chess.pgn.Game.from_board(board)
        parsed.headers["Result"] = game["result"]
        pgns.append(str(parsed))
    (directory / "games.pgn").write_bytes(("\r\n\r\n".join(pgns) + "\r\n").encode())
    manifest["pgn_path"] = "games.pgn"
    manifest["files_sha256"] = {name: TRAIN.sha256(directory / name)
                                for name in ("positions.jsonl", "games.jsonl", "games.pgn")}
    (directory / "datasetmanifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return directory / "datasetmanifest.json"


def endgame_fixture(directory):
    games, positions = [], []
    for index, (king, black, queen, move, split, family) in enumerate([
            *(('c6', 'a8', 'b' + str(rank), 'b' + str(rank) + 'b7', 'train', 'endgame-' + str(rank % 3))
              for rank in range(1, 7)),
            ('f6', 'h8', 'g6', 'g6g7', 'validation', 'endgame-validation'),
            ('c3', 'a1', 'b3', 'b3b2', 'test', 'endgame-test')]):
        board = chess.Board(None)
        for square, piece in ((king, 'K'), (black, 'k'), (queen, 'Q')):
            board.set_piece_at(chess.parse_square(square), chess.Piece.from_symbol(piece))
        initial = board.fen(en_passant="fen")
        assert board.is_valid() and not board.is_check()
        board.push_uci(move)
        assert board.is_checkmate()
        game_id = 'extra-' + str(index)
        games.append({"game_id": game_id, "family": family, "split": split,
                      "initial_fen": initial, "history": [move], "completed": True,
                      "result": "1-0"})
        start = chess.Board(initial)
        positions.append({"game_id": game_id, "family": family, "split": split,
                          "initial_fen": initial, "history": [], "fen": initial, "ply": 0,
                          "turn": "w", "teacher_cp_stm": 20,
                          "material_cp_stm": material_cp_stm(start), "outcome_stm": 1,
                          "teacher_depth": 2, "teacher_nodes": 100, "teacher_pv": [move]})
    manifest = {"schema": TRAIN.SCHEMA, "status": "complete", "games_path": "games.jsonl",
                "positions_path": "positions.jsonl"}
    return manifest, games, positions


class LearningCurveTests(unittest.TestCase):
    def setup_sources(self, temporary):
        base, extra = Path(temporary) / "bootstrap", Path(temporary) / "extra"
        base.mkdir()
        extra.mkdir()
        manifest, games, positions = fixture(base)
        baseline = write_source(base, manifest, games, positions)
        manifest, games, positions = endgame_fixture(extra)
        additions = write_source(extra, manifest, games, positions)
        return baseline, additions, Path(temporary) / "curve"

    def run_small(self, baseline, additions, output, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return CURVE.run_curve(baseline, additions, output, counts=[0, 2, 4, 8],
                                   seeds=[61, 62], epochs=2, batch_size=2, **kwargs)

    def test_real_trainer_nested_arms_fixed_initials_and_candidate_policy(self):
        with tempfile.TemporaryDirectory() as temporary:
            baseline, additions, output = self.setup_sources(temporary)
            report = self.run_small(baseline, additions, output)
            self.assertEqual(report["status"], "complete")
            self.assertEqual(report["feasible_added_train"], [0, 2, 4])
            self.assertEqual(report["skipped_infeasible_added_train"], [8])
            self.assertEqual(report["candidate"]["added_train"], 4)
            self.assertEqual(report["candidate"]["seed"], 61)
            initials = {seed: set() for seed in (61, 62)}
            added_sets = []
            for arm in report["arms"]:
                count = arm["added_train"]
                self.assertEqual(arm["dataset"]["counts"]["train"]["samples"], 1 + count)
                rows = list(TRAIN.json_lines(output / f"added-{count:06d}" / "dataset" / "positions.jsonl"))
                added_sets.append({(row["game_id"], row["ply"]) for row in rows if row["game_id"].startswith("extra-")})
                for run in arm["runs"]:
                    initials[run["seed"]].add(run["initial_model_sha256"])
                    self.assertEqual(set(run["independent_endgame"]), {"validation", "test"})
                    self.assertEqual(run["independent_endgame"]["test"]["samples"], 1)
                    self.assertEqual(run["loss_components"]["validation_selection"], "MSE only; L2 is a training penalty")
            self.assertTrue(added_sets[0] < added_sets[1] < added_sets[2])
            self.assertTrue(all(len(hashes) == 1 for hashes in initials.values()))
            self.assertNotEqual(next(iter(initials[61])), next(iter(initials[62])))
            self.assertTrue((output / report["candidate"]["model_path"]).is_file())

    def test_raw_heldout_games_pgn_and_all_source_bytes_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            baseline, additions, output = self.setup_sources(temporary)
            paths = [path for directory in (baseline.parent, additions.parent) for path in directory.iterdir()]
            before = {path: path.read_bytes() for path in paths}
            report = self.run_small(baseline, additions, output)
            for path, raw in before.items():
                self.assertEqual(path.read_bytes(), raw)
            for label, manifest in (("bootstrap", baseline), ("endgame", additions)):
                for path in manifest.parent.iterdir():
                    self.assertEqual((output / "sources" / label / path.name).read_bytes(), path.read_bytes())
            base_rows = baseline.parent.joinpath("positions.jsonl").read_bytes().splitlines(keepends=True)
            for arm in report["arms"]:
                dataset = output / f"added-{arm['added_train']:06d}" / "dataset"
                self.assertTrue((dataset / "positions.jsonl").read_bytes().startswith(b"".join(base_rows)))
                self.assertEqual((dataset / "games.jsonl").read_bytes(),
                                 baseline.parent.joinpath("games.jsonl").read_bytes() +
                                 additions.parent.joinpath("games.jsonl").read_bytes())
                pgn = (dataset / "games.pgn").read_bytes()
                self.assertTrue(pgn.startswith(baseline.parent.joinpath("games.pgn").read_bytes()))
                self.assertTrue(pgn.endswith(additions.parent.joinpath("games.pgn").read_bytes()))

    def test_schedule_is_label_independent_repeatable_and_family_balanced(self):
        with tempfile.TemporaryDirectory() as temporary:
            baseline, additions, _ = self.setup_sources(temporary)
            base, extra = CURVE._raw_source(baseline), CURVE._raw_source(additions)
            schedule, report = CURVE.prepare_schedule(base, extra, 17)
            changed = copy.deepcopy(extra)
            for row in changed["positions"]:
                row["teacher_cp_stm"] += 999
            other, _ = CURVE.prepare_schedule(base, changed, 17)
            self.assertEqual(schedule, other)
            self.assertEqual(len(set(schedule)), 6)
            self.assertEqual(len({extra["positions"][index]["family"] for index in schedule[:3]}), 3)
            self.assertEqual([row["family"] for row in report["ordered_samples"][:3]],
                             [row["family"] for row in report["ordered_samples"][3:6]])

    def test_same_game_samples_are_round_robin_within_family(self):
        with tempfile.TemporaryDirectory() as temporary:
            baseline, additions, _ = self.setup_sources(temporary)
            base, extra = CURVE._raw_source(baseline), CURVE._raw_source(additions)
            # Scheduling receives validated rows in production. This focused
            # test assigns already-distinct valid input rows to two game queues.
            for index, row in enumerate(extra["positions"][:6]):
                row["family"] = "single-family"
                row["game_id"] = "schedule-game-" + str(index % 2)
                row["ply"] = index
            schedule, _ = CURVE.prepare_schedule(base, extra, 17)
            ids = [extra["positions"][index]["game_id"] for index in schedule]
            self.assertTrue(all(ids[index] != ids[index + 1] for index in range(len(ids) - 1)))

    def test_same_neural_input_with_different_counters_is_not_new_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            baseline, additions, _ = self.setup_sources(temporary)
            base, extra = CURVE._raw_source(baseline), CURVE._raw_source(additions)
            duplicate = copy.deepcopy(extra["positions"][0])
            duplicate["game_id"] = "duplicate-game"
            fields = duplicate["initial_fen"].split()
            fields[4:6] = ["2", "5"]
            duplicate["initial_fen"] = duplicate["fen"] = " ".join(fields)
            extra["positions"].append(duplicate)
            schedule, report = CURVE.prepare_schedule(base, extra)
            self.assertEqual(len(schedule), 6)
            self.assertEqual(report["exclusions"], {"added_training_input_duplicate": 1})

    def test_color_rank_symmetric_neural_input_deduplicated(self):
        with tempfile.TemporaryDirectory() as temporary:
            baseline, additions, _ = self.setup_sources(temporary)
            base, extra = CURVE._raw_source(baseline), CURVE._raw_source(additions)
            duplicate = copy.deepcopy(extra["positions"][0])
            board = chess.Board(duplicate["fen"]).mirror()
            duplicate.update(game_id="symmetric-game", initial_fen=board.fen(en_passant="fen"),
                             fen=board.fen(en_passant="fen"), history=[], turn="b")
            self.assertEqual(feature_key(board), feature_key(chess.Board(extra["positions"][0]["fen"])))
            extra["positions"].append(duplicate)
            schedule, report = CURVE.prepare_schedule(base, extra)
            self.assertEqual(len(schedule), 6)
            self.assertEqual(report["exclusions"]["added_training_input_duplicate"], 1)

    def test_bootstrap_input_preserved_while_duplicate_addition_excluded(self):
        with tempfile.TemporaryDirectory() as temporary:
            baseline, additions, _ = self.setup_sources(temporary)
            base, extra = CURVE._raw_source(baseline), CURVE._raw_source(additions)
            row = copy.deepcopy(base["positions"][0])
            row["game_id"] = "extra-collision"
            extra["positions"].append(row)
            schedule, report = CURVE.prepare_schedule(base, extra)
            self.assertEqual(len(schedule), 6)
            self.assertEqual(report["exclusions"]["bootstrap_input_duplicate"], 1)

    def test_independent_holdout_must_not_overlap_any_bootstrap_split(self):
        with tempfile.TemporaryDirectory() as temporary:
            baseline, additions, _ = self.setup_sources(temporary)
            base, extra = CURVE._raw_source(baseline), CURVE._raw_source(additions)
            row = copy.deepcopy(base["positions"][0])
            row.update(game_id="extra-heldout", split="test", family="new-test")
            extra["positions"].append(row)
            with self.assertRaisesRegex(ValueError, "holdout overlaps"):
                CURVE.prepare_schedule(base, extra)

    def test_family_split_changes_and_game_identity_collisions_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            baseline, additions, _ = self.setup_sources(temporary)
            base, extra = CURVE._raw_source(baseline), CURVE._raw_source(additions)
            changed = copy.deepcopy(extra)
            changed["metadata"]["family_splits"]["family-1"] = "train"
            with self.assertRaisesRegex(ValueError, "family changes split"):
                CURVE.prepare_schedule(base, changed)
            changed = copy.deepcopy(extra)
            changed["games"][0]["game_id"] = base["games"][0]["game_id"]
            with self.assertRaisesRegex(ValueError, "reuse a game id"):
                CURVE.prepare_schedule(base, changed)
            changed = copy.deepcopy(extra)
            changed["games"][0]["initial_fen"] = base["games"][0]["initial_fen"]
            changed["games"][0]["history"] = base["games"][0]["history"]
            with self.assertRaisesRegex(ValueError, "reuses a bootstrap game history"):
                CURVE.prepare_schedule(base, changed)

    def test_failure_has_no_candidate_or_completed_partial_arm(self):
        with tempfile.TemporaryDirectory() as temporary:
            baseline, additions, output = self.setup_sources(temporary)
            with patch.object(TRAIN, "train", side_effect=RuntimeError("synthetic failure")):
                with self.assertRaisesRegex(RuntimeError, "synthetic failure"):
                    self.run_small(baseline, additions, output)
            report = json.loads((output / "report.json").read_text())
            self.assertEqual(report["status"], "failed")
            self.assertNotIn("candidate", report)
            self.assertFalse((output / "added-000000").exists())
            self.assertTrue((output / "added-000000.partial").is_dir())

    def test_mutation_during_training_detected_without_publishing_candidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            baseline, additions, output = self.setup_sources(temporary)
            original = TRAIN.train
            def mutate(*args, **kwargs):
                result = original(*args, **kwargs)
                with additions.parent.joinpath("positions.jsonl").open("ab") as handle:
                    handle.write(b" ")
                return result
            with patch.object(TRAIN, "train", side_effect=mutate):
                with self.assertRaisesRegex(ValueError, "frozen input changed"):
                    self.run_small(baseline, additions, output)
            report = json.loads((output / "report.json").read_text())
            self.assertEqual(report["status"], "failed")
            self.assertNotIn("candidate", report)
            self.assertFalse((output / "added-000000").exists())

    def test_existing_output_and_infeasible_minimum_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            baseline, additions, output = self.setup_sources(temporary)
            with self.assertRaisesRegex(ValueError, "insufficient unique"):
                CURVE.run_curve(baseline, additions, output, counts=[0, 10], seeds=[1, 2])
            self.assertFalse(output.exists())
            output.mkdir()
            marker = output / "keep"
            marker.write_bytes(b"user data")
            with self.assertRaises(FileExistsError):
                self.run_small(baseline, additions, output)
            self.assertEqual(marker.read_bytes(), b"user data")

    def test_configuration_requires_nested_counts_and_replicates(self):
        for counts, seeds in (([1, 2], [1, 2]), ([0, 1, 1], [1, 2]),
                              ([0, 2, 1], [1, 2]), ([0, 1], [1]),
                              ([0, 1], [1, 1]), ([0, True], [1, 2]),
                              ([0, 1], [1, -2])):
            with self.subTest(counts=counts, seeds=seeds), self.assertRaises(ValueError):
                CURVE._validate_configuration(counts, seeds, 1, 2, .003, .0001)

    def test_train_and_new_holdout_collision_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            baseline, additions, _ = self.setup_sources(temporary)
            base, extra = CURVE._raw_source(baseline), CURVE._raw_source(additions)
            row = copy.deepcopy(extra["positions"][0])
            row.update(game_id="leaking-holdout", split="test", family="leaking-test")
            extra["positions"].append(row)
            with self.assertRaisesRegex(ValueError, "training input overlaps"):
                CURVE.prepare_schedule(base, extra)

    def test_repeated_real_training_exactly_reproduces_models_and_metrics(self):
        with tempfile.TemporaryDirectory() as temporary:
            baseline, additions, output = self.setup_sources(temporary)
            first = self.run_small(baseline, additions, output)
            second = self.run_small(baseline, additions, output.with_name("repeat"))
            self.assertEqual(first["candidate"], second["candidate"])
            self.assertEqual(first["arms"], second["arms"])

    def test_test_and_new_holdout_labels_cannot_select_checkpoint_or_candidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            baseline, additions, output = self.setup_sources(temporary)
            first = self.run_small(baseline, additions, output)
            for path, splits in ((baseline, {"test"}), (additions, {"validation", "test"})):
                manifest = json.loads(path.read_text())
                rows = list(TRAIN.json_lines(path.parent / "positions.jsonl"))
                for row in rows:
                    if row["split"] in splits:
                        row["teacher_cp_stm"] = -700
                (path.parent / "positions.jsonl").write_bytes(b"".join(
                    (json.dumps(row) + "\n").encode() for row in rows))
                manifest["files_sha256"]["positions.jsonl"] = TRAIN.sha256(path.parent / "positions.jsonl")
                path.write_text(json.dumps(manifest), encoding="utf-8")
            second = self.run_small(baseline, additions, output.with_name("changed-tests"))
            self.assertEqual(first["candidate"], second["candidate"])
            for before_arm, after_arm in zip(first["arms"], second["arms"]):
                for before, after in zip(before_arm["runs"], after_arm["runs"]):
                    self.assertEqual(before["selected_epoch"], after["selected_epoch"])
                    self.assertEqual(before["model_sha256"], after["model_sha256"])
                    self.assertEqual(before["history"], after["history"])
                    self.assertNotEqual(before["metrics"]["test"], after["metrics"]["test"])
                    self.assertNotEqual(before["independent_endgame"], after["independent_endgame"])

    def test_complete_game_identity_cannot_cross_splits_under_new_ids(self):
        with tempfile.TemporaryDirectory() as temporary:
            baseline, additions, _ = self.setup_sources(temporary)
            base, extra = CURVE._raw_source(baseline), CURVE._raw_source(additions)
            duplicate = copy.deepcopy(extra["games"][0])
            duplicate.update(game_id="relabelled-game", split="test", family="independent-fake")
            extra["games"].append(duplicate)
            with self.assertRaisesRegex(ValueError, "complete game history appears across splits"):
                CURVE.prepare_schedule(base, extra)

    def test_unused_incomplete_game_preserved_without_becoming_a_sample_or_draw(self):
        with tempfile.TemporaryDirectory() as temporary:
            baseline, additions, _ = self.setup_sources(temporary)
            manifest = json.loads(additions.read_text())
            games = list(TRAIN.json_lines(additions.parent / "games.jsonl"))
            rows = list(TRAIN.json_lines(additions.parent / "positions.jsonl"))
            unfinished = copy.deepcopy(games[0])
            unfinished.update(game_id="unused-ply-cap", family="unused-family", history=[],
                              completed=False, result="*")
            games.append(unfinished)
            write_source(additions.parent, manifest, games, rows)
            source = CURVE._raw_source(additions)
            self.assertEqual(source["unused_incomplete_games"], 1)
            self.assertEqual(source["games"][-1]["result"], "*")
            self.assertEqual(len(source["positions"]), 8)
            row = copy.deepcopy(rows[0])
            row.update(game_id="unused-ply-cap", family="unused-family")
            write_source(additions.parent, manifest, games, rows + [row])
            with self.assertRaisesRegex(ValueError, "rows may not refer to an unfinished"):
                CURVE._raw_source(additions)

    def test_actual_turn_alternates_within_family_when_both_available(self):
        with tempfile.TemporaryDirectory() as temporary:
            baseline, additions, _ = self.setup_sources(temporary)
            base, extra = CURVE._raw_source(baseline), CURVE._raw_source(additions)
            for row in extra["positions"][:6]:
                row["family"] = "one-family"
            for index in range(4):
                row = copy.deepcopy(extra["positions"][index])
                board = chess.Board(row["fen"]).transform(chess.flip_horizontal).mirror()
                row.update(game_id="black-turn-" + str(index), initial_fen=board.fen(en_passant="fen"),
                           fen=board.fen(en_passant="fen"), turn="b")
                extra["positions"].append(row)
            schedule, report = CURVE.prepare_schedule(base, extra)
            turns = [extra["positions"][index]["turn"] for index in schedule]
            self.assertEqual(len(turns), 10)
            self.assertTrue(all(turns[index] != turns[index + 1] for index in range(7)))
            self.assertEqual([row["turn"] for row in report["ordered_samples"]], turns)


if __name__ == "__main__":
    unittest.main()
