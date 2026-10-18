"""Accumulation, frozen holdout and loss-label provenance regression tests."""

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import learning_corpus as CORPUS
import train_neural as TRAIN
import neural_model as NN
from neural_training_tests import fixture, write_fixture
import chess
import chess.pgn


def dataset(root, name):
    directory = root / name
    directory.mkdir()
    manifest, games, positions = fixture(directory)
    rewrite(directory, manifest, games, positions)
    return directory, manifest, games, positions


def rewrite(directory, manifest, games, positions, refresh_hashes=True):
    write_fixture(directory, manifest, games, positions)
    if refresh_hashes:
        manifest["files_sha256"] = {name: TRAIN.sha256(directory / name)
                                    for name in ("games.jsonl", "positions.jsonl")}
        write_fixture(directory, manifest, games, positions)
    return directory / "datasetmanifest.json"


def rows(output):
    return list(TRAIN.json_lines(output / "positions.jsonl"))


def add_game_sample(game, sample, history, ply):
    board = TRAIN.replay(chess.STARTING_FEN, history)
    game.update(history=history, result=board.outcome(claim_draw=True).result())
    board = TRAIN.replay(chess.STARTING_FEN, history[:ply])
    winner = {"1-0": 1, "0-1": -1, "1/2-1/2": 0}[game["result"]]
    sample.update(history=history[:ply], ply=ply, fen=board.fen(en_passant="fen"),
                  turn="w" if board.turn else "b", material_cp_stm=NN.material_cp_stm(board),
                  outcome_stm=winner if board.turn else -winner,
                  teacher_pv=[next(iter(board.legal_moves)).uci()])


def diagnosis(root, source, samples, name="diagnosis"):
    directory = root / name
    directory.mkdir()
    source_manifest = source / "datasetmanifest.json"
    report = {"status": "complete", "source_dataset_manifest_sha256": TRAIN.sha256(source_manifest),
              "source_files_sha256": {name: TRAIN.sha256(source / name)
                                       for name in ("games.jsonl", "positions.jsonl")},
              "positions_path": "positions.jsonl"}
    for sample in samples:
        sample["loss_analysis"] = {"reliable": True, "cp_loss": 120}
    (directory / "positions.jsonl").write_text(
        "".join(json.dumps(sample) + "\n" for sample in samples), encoding="utf-8")
    report["positions_sha256"] = TRAIN.sha256(directory / "positions.jsonl")
    TRAIN.write_json(directory / "loss-report.json", report)
    return directory


class CorpusTests(unittest.TestCase):
    def test_bootstrap_and_duplicate_run_registration_are_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, _, _, _ = dataset(root, "source")
            source = source / "datasetmanifest.json"
            first = CORPUS.build_corpus([source], root / "first")
            second = CORPUS.build_corpus([source, source], root / "second")
            self.assertEqual(first["files_sha256"], second["files_sha256"])
            self.assertEqual(second["exclusions"]["duplicate_dataset_registrations"], 1)
            self.assertEqual(second["positions_by_split"], {"train": 1, "validation": 1, "test": 1})
            TRAIN.load_dataset(root / "second/manifest.json")
            with self.assertRaises(FileExistsError):
                CORPUS.build_corpus([source], root / "second")

    def test_new_games_keep_colliding_local_ids_separate_and_holdout_fixed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            a, am, ag, ap = dataset(root, "a")
            b, bm, bg, bp = dataset(root, "b")
            add_game_sample(bg[0], bp[0], ["g2g4", "e7e5", "f2f3", "d8h4"], 2)
            bp[1]["teacher_cp_stm"] = 900
            bp[2]["teacher_cp_stm"] = 800
            bpath = rewrite(b, bm, bg, bp)
            result = CORPUS.build_corpus([a / "datasetmanifest.json", bpath], root / "out")
            samples = rows(root / "out")
            train = [sample for sample in samples if sample["split"] == "train"]
            self.assertEqual(len(train), 2)
            self.assertNotEqual(train[0]["game_id"], train[1]["game_id"])
            self.assertEqual([sample["teacher_cp_stm"] for sample in samples if sample["split"] != "train"], [20, 20])
            self.assertEqual(result["exclusions"]["later_holdout_games_ignored"], 2)

    def test_later_train_only_run_is_valid_without_new_holdout_samples(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            a, _, _, _ = dataset(root, "a")
            b, bm, bg, bp = dataset(root, "b")
            add_game_sample(bg[0], bp[0], ["g2g4", "e7e5", "f2f3", "d8h4"], 2)
            rewrite(b, bm, bg[:1], bp[:1])
            result = CORPUS.build_corpus([a / "datasetmanifest.json", b / "datasetmanifest.json"], root / "out")
            self.assertEqual(result["positions_by_split"], {"train": 2, "validation": 1, "test": 1})
            with self.assertRaisesRegex(ValueError, "empty validation"):
                CORPUS.build_corpus([b / "datasetmanifest.json"], root / "bad-bootstrap")

    def test_all_unfinished_later_run_preserves_previous_corpus_but_is_replayed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            a, _, _, _ = dataset(root, "a")
            b, bm, bg, _ = dataset(root, "b")
            for game in bg:
                game.update(completed=False, result="*", history=game["history"][:2])
            rewrite(b, bm, bg, [])
            result = CORPUS.build_corpus([a / "datasetmanifest.json", b / "datasetmanifest.json"], root / "out")
            self.assertEqual(result["positions_by_split"], {"train": 1, "validation": 1, "test": 1})
            self.assertEqual(result["exclusions"]["incomplete_games_excluded"], 3)
            bg[0]["history"][0] = "a2a5"
            rewrite(b, bm, bg, [])
            with self.assertRaisesRegex(ValueError, "replay"):
                CORPUS.build_corpus([a / "datasetmanifest.json", b / "datasetmanifest.json"], root / "bad")

    def test_same_game_ply_uses_latest_teacher_label(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            a, _, _, _ = dataset(root, "a")
            b, bm, bg, bp = dataset(root, "b")
            bp[0]["teacher_cp_stm"] = 143
            rewrite(b, bm, bg, bp)
            result = CORPUS.build_corpus([a / "datasetmanifest.json", b / "datasetmanifest.json"], root / "out")
            self.assertEqual([sample["teacher_cp_stm"] for sample in rows(root / "out") if sample["split"] == "train"], [143])
            self.assertEqual(result["exclusions"]["same_game_ply_labels_replaced"], 1)
            self.assertEqual(result["exclusions"]["duplicate_games_coalesced"], 1)

    def test_identical_manifest_with_different_unhashed_data_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            a, am, ag, ap = dataset(root, "a")
            b, bm, bg, bp = dataset(root, "b")
            am.pop("files_sha256")
            bm.pop("files_sha256")
            bp[0]["teacher_cp_stm"] += 30
            rewrite(a, am, ag, ap, refresh_hashes=False)
            rewrite(b, bm, bg, bp, refresh_hashes=False)
            with self.assertRaisesRegex(ValueError, "immutable run identity"):
                CORPUS.build_corpus([a / "datasetmanifest.json", b / "datasetmanifest.json"], root / "out")

    def test_loss_diagnosis_replaces_label_adds_position_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            a, _, _, ap = dataset(root, "a")
            updated = copy.deepcopy(ap[0])
            updated["teacher_cp_stm"] = -500
            earlier = copy.deepcopy(updated)
            earlier.update(history=[], ply=0, fen=chess.STARTING_FEN,
                           teacher_pv=["e2e4"])
            d = diagnosis(root, a, [updated, earlier])
            result = CORPUS.build_corpus([a / "datasetmanifest.json"], root / "out", [d, d])
            train = [sample for sample in rows(root / "out") if sample["split"] == "train"]
            self.assertEqual(len(train), 2)
            self.assertEqual({sample["teacher_cp_stm"] for sample in train}, {-500})
            self.assertEqual(result["exclusions"]["diagnosed_labels_replaced"], 1)
            self.assertEqual(result["exclusions"]["diagnosed_positions_added"], 1)
            self.assertEqual(result["exclusions"]["duplicate_diagnosis_registrations"], 1)

    def test_diagnosis_precedes_original_duplicate_input_and_holdout_hashes_stay_fixed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            a, _, _, ap = dataset(root, "a")
            b, bm, bg, bp = dataset(root, "b")
            add_game_sample(bg[0], bp[0], ["f2f3", "e7e5", "g2g4", "b8c6", "a2a3", "d8h4"], 2)
            bp[0]["teacher_cp_stm"] = 500
            rewrite(b, bm, bg, bp)
            update = copy.deepcopy(ap[0])
            update["teacher_cp_stm"] = -500
            d = diagnosis(root, a, [update])
            baseline = CORPUS.build_corpus([a / "datasetmanifest.json"], root / "base")
            result = CORPUS.build_corpus([a / "datasetmanifest.json", b / "datasetmanifest.json"], root / "out", [d])
            self.assertEqual([sample["teacher_cp_stm"] for sample in rows(root / "out") if sample["split"] == "train"], [-500])
            self.assertEqual(result["holdout_rows_sha256"], baseline["holdout_rows_sha256"])

    def test_diagnosis_unknown_source_hash_and_changed_source_hash_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            a, _, _, ap = dataset(root, "a")
            d = diagnosis(root, a, [copy.deepcopy(ap[0])])
            path = d / "loss-report.json"
            original = json.loads(path.read_text())
            for field, value in (("source_dataset_manifest_sha256", "0" * 64),
                                 ("source_files_sha256", {"games.jsonl": "0" * 64})):
                payload = {**original, field: value}
                TRAIN.write_json(path, payload)
                with self.subTest(field=field), self.assertRaisesRegex(ValueError, "hash"):
                    CORPUS.build_corpus([a / "datasetmanifest.json"], root / "out", [d])

    def test_invalid_diagnosis_history_pv_result_or_holdout_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            a, _, _, ap = dataset(root, "a")
            cases = [{**ap[0], "fen": chess.STARTING_FEN},
                     {**ap[0], "teacher_pv": ["a1a8"]},
                     {**ap[0], "outcome_stm": 1},
                     copy.deepcopy(ap[1])]
            for index, sample in enumerate(cases):
                d = diagnosis(root, a, [sample], f"diagnosis-{index}")
                with self.subTest(index=index), self.assertRaises(ValueError):
                    CORPUS.build_corpus([a / "datasetmanifest.json"], root / "out", [d])

    def test_unreliable_diagnosis_and_duplicate_game_ply_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            a, _, _, ap = dataset(root, "a")
            for index, values in enumerate(([copy.deepcopy(ap[0])], [copy.deepcopy(ap[0]), copy.deepcopy(ap[0])])):
                d = diagnosis(root, a, values, f"diagnosis-{index}")
                if index == 0:
                    values[0]["loss_analysis"]["reliable"] = False
                    (d / "positions.jsonl").write_text(json.dumps(values[0]) + "\n", encoding="utf-8")
                    report = json.loads((d / "loss-report.json").read_text())
                    report["positions_sha256"] = TRAIN.sha256(d / "positions.jsonl")
                    TRAIN.write_json(d / "loss-report.json", report)
                with self.subTest(index=index), self.assertRaises(ValueError):
                    CORPUS.build_corpus([a / "datasetmanifest.json"], root / "out", [d])

    def test_conflicting_family_split_across_datasets_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            a, _, _, _ = dataset(root, "a")
            b, bm, bg, bp = dataset(root, "b")
            bg[0]["family"], bg[1]["family"] = bg[1]["family"], bg[0]["family"]
            bp[0]["family"], bp[1]["family"] = bp[1]["family"], bp[0]["family"]
            rewrite(b, bm, bg, bp)
            with self.assertRaisesRegex(ValueError, "family split"):
                CORPUS.build_corpus([a / "datasetmanifest.json", b / "datasetmanifest.json"], root / "out")

    def test_new_train_canonical_or_neural_collision_cannot_change_frozen_holdout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            a, _, ag, ap = dataset(root, "a")
            for index, remove_rights in enumerate((False, True)):
                b, bm, bg, bp = dataset(root, f"b-{index}")
                bg[0] = {**copy.deepcopy(ag[1]), "game_id": "game-0",
                         "family": "family-0", "split": "train"}
                bp[0] = {**copy.deepcopy(ap[1]), "game_id": "game-0",
                         "family": "family-0", "split": "train", "teacher_cp_stm": 999}
                # Move this run's validation sample away from the colliding
                # position so the source itself still has disjoint splits.
                add_game_sample(bg[1], bp[1], bg[1]["history"], 2)
                if remove_rights:
                    initial = chess.Board()
                    initial.castling_rights = 0
                    bg[0]["initial_fen"] = initial.fen(en_passant="fen")
                    bp[0]["initial_fen"] = bg[0]["initial_fen"]
                    board = TRAIN.replay(bg[0]["initial_fen"], bp[0]["history"])
                    bp[0]["fen"] = board.fen(en_passant="fen")
                rewrite(b, bm, bg, bp)
                output = root / f"out-{index}"
                result = CORPUS.build_corpus([a / "datasetmanifest.json", b / "datasetmanifest.json"], output)
                self.assertEqual(result["exclusions"]["train_holdout_collisions_excluded"], 1)
                self.assertEqual([sample["teacher_cp_stm"] for sample in rows(output) if sample["split"] != "train"], [20, 20])
                self.assertEqual(result["positions_by_split"], {"train": 1, "validation": 1, "test": 1})

    def test_declared_file_hash_tampering_and_illegal_game_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            a, am, ag, ap = dataset(root, "a")
            am["files_sha256"] = {"games.jsonl": "0" * 64}
            rewrite(a, am, ag, ap, refresh_hashes=False)
            with self.assertRaisesRegex(ValueError, "SHA256"):
                CORPUS.build_corpus([a / "datasetmanifest.json"], root / "out")
            am.pop("files_sha256")
            ag[0]["history"][0] = "a2a5"
            rewrite(a, am, ag, ap)
            with self.assertRaisesRegex(ValueError, "replay"):
                CORPUS.build_corpus([a / "datasetmanifest.json"], root / "out")

    def test_pgn_history_result_and_count_are_independently_checked(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            a, am, ag, ap = dataset(root, "a")
            am["pgn_path"] = "games.pgn"
            pgn = []
            for game in ag:
                parsed = chess.pgn.Game.from_board(TRAIN.replay(game["initial_fen"], game["history"]))
                parsed.headers["Result"] = game["result"]
                pgn.append(str(parsed))
            (a / "games.pgn").write_text("\n\n".join(pgn), encoding="utf-8")
            rewrite(a, am, ag, ap)
            CORPUS.build_corpus([a / "datasetmanifest.json"], root / "good")
            (a / "games.pgn").write_text("\n\n".join(pgn[:2]), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "PGN"):
                CORPUS.build_corpus([a / "datasetmanifest.json"], root / "bad")

    def test_public_bootstrap_replay_hashes_and_frozen_holdout_load(self):
        source = ROOT / "docs/checks/2026-10-18-neural-v1/data/manifest.json"
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "corpus"
            result = CORPUS.build_corpus([source], output)
            self.assertEqual(result["positions_by_split"]["validation"], 96)
            self.assertEqual(result["positions_by_split"]["test"], 125)
            self.assertGreater(result["positions_by_split"]["train"], 300)
            self.assertEqual(result["bootstrap_manifest_sha256"], TRAIN.sha256(source))
            CORPUS._verify_pgn(output / "games.pgn", list(TRAIN.json_lines(output / "games.jsonl")))

    def test_fixed_holdout_policy_cannot_be_disabled(self):
        with self.assertRaisesRegex(ValueError, "fixed_holdout"):
            CORPUS.build_corpus([], "unused", fixed_holdout=False)


if __name__ == "__main__":
    unittest.main()
