"""Train a tiny QAT export, freeze it, then test fresh holdout and C++ loading.

Usage: python tests/neural_qat_integration.py --engine BUILD/regicide[.exe]
All generated data, artifacts, and adversarial fixtures live in a temporary
directory. This uses a small test grid; the recorded research run preserves
both the initial fixed grid and the later explicit training-only plan.
"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from unittest import mock

import chess
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import neural_training as training
import neural_qat as qat
import neural_qat_experiment as experiment
import neural_training_integration as prior


def assert_rejects(action, pattern: str) -> None:
    try:
        action()
    except ValueError as error:
        assert pattern in str(error).lower(), (pattern, str(error))
    else:
        raise AssertionError("adversarial edit was accepted: " + pattern)


def replace_holdout(directory: Path, rows: list[dict]) -> None:
    path = directory / "holdout.jsonl"
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
    manifest_path = directory / "holdout_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update({"rows": len(rows), "holdout_sha256": training.sha256(path)})
    training.write_json(manifest_path, manifest)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", required=True, type=Path)
    parser.add_argument("--model-tests", type=Path)
    args = parser.parse_args()
    engine = args.engine.resolve()
    model_tests = (args.model_tests or engine.with_name("regicide_neural_model_tests" + engine.suffix)).resolve()
    assert engine.is_file() and model_tests.is_file(), "build engine and neural model tests first"
    grid = [{"candidate_id": "integration-a", "epochs": 4, "input_bias_lr": .05,
             "output_lr": .02, "seed": 20261023, "batch_size": 16},
            {"candidate_id": "integration-b", "epochs": 6, "input_bias_lr": .10,
             "output_lr": .02, "seed": 20261023, "batch_size": 16}]
    with tempfile.TemporaryDirectory(prefix="regicide-qat-integration-") as temporary:
        root = Path(temporary)
        data = root / "data"
        training.generate_dataset(engine, data, games=8, max_plies=24, sample_every=2, seed=51027)
        _, partitions = training.read_dataset(data)
        initial = training.bootstrap_float()
        np.savez(root / "initial.npz", **initial)
        baseline_path = root / "baseline.nnue"
        training.save_model(training.quantize(initial), baseline_path)
        selected = root / "selected"
        # Every attempt to open original validation targets during selection is
        # an immediate failure, including hash reads through Path.read_bytes.
        original_open = Path.open
        def guarded_open(file, *args, **kwargs):
            if file.name == "validation.jsonl":
                raise AssertionError("QAT selection accessed validation")
            return original_open(file, *args, **kwargs)
        with mock.patch.object(qat, "FIXED_GRID", grid), mock.patch.object(Path, "open", guarded_open):
            selection = qat.select_model(data, baseline_path, root / "initial.npz", selected)
        freeze_path = selected / "freeze.json"
        before = {file.name: file.read_bytes() for file in selected.iterdir() if file.is_file()}
        freeze = qat.verify_freeze(freeze_path)
        model_path = selected / "model.nnue"
        model, baseline = training.load_model(model_path), training.load_model(baseline_path)
        packed, _ = training.pack_records(partitions["train"])
        floats = qat.load_float_parameters(selected / "float_weights.npz")
        expected, _ = qat.qat_forward(floats, packed)
        np.testing.assert_array_equal(expected,
            [training.quantized_white(chess.Board(row["fen"]), model) for row in partitions["train"]])
        # Save/load preserves every integer, current architecture header, and
        # checksum, with existing-file protection and malformed load failures.
        roundtrip = root / "roundtrip.nnue"
        training.save_model(model, roundtrip)
        assert roundtrip.read_bytes() == model_path.read_bytes()
        assert model_path.read_text(encoding="ascii").splitlines()[0] == "REGICIDE_NNUE 1 768 16 127 8"
        assert_rejects(lambda: training.save_model(model, model_path), "overwrite")
        tokens = model_path.read_text(encoding="ascii").split()
        tokens[-1] = str(int(tokens[-1]) ^ 1)
        corrupt = root / "corrupt.nnue"
        corrupt.write_text(" ".join(tokens) + "\n", encoding="ascii")
        assert_rejects(lambda: training.load_model(corrupt), "checksum")
        rejected = subprocess.run([str(engine), "eval-batch", "--nnue-model", str(corrupt)],
            input=chess.STARTING_FEN + "\n", capture_output=True, text=True, timeout=10)
        assert rejected.returncode != 0, "C++ accepted corrupt model"
        fens = [row["fen"] for rows in partitions.values() for row in rows] + prior.special_positions()
        scores = training.evaluate_engine(engine, fens, model_path)
        for fen, triple in zip(fens, scores, strict=True):
            board = chess.Board(fen)
            score = training.quantized_white(board, model) * (1 if board.turn else -1)
            assert triple[1] == triple[2] == score, (fen, triple, score)
        cpp_output = prior.run([str(model_tests), str(model_path)])
        assert "0 failures" in cpp_output, cpp_output
        uci_pairs = prior.test_uci(engine, model_path, corrupt,
            [chess.STARTING_FEN, "7k/8/8/3pP3/8/8/8/6K1 w - d6 0 1",
             "1r5k/P7/8/8/8/8/8/6K1 w - - 0 1"])
        holdout = root / "holdout"
        metadata = experiment.generate_holdout(selected, data, holdout, engine,
            games=4, max_plies=24, sample_every=2, seed=71029)
        held_rows = prior.read_rows(holdout / "holdout.jsonl")
        old_ids = {training.feature_identity(chess.Board(row["fen"]))
                   for rows in partitions.values() for row in rows}
        assert all(row["feature_hash"] not in old_ids for row in held_rows)
        assert len({row["feature_hash"] for row in held_rows}) == len(held_rows)
        assert metadata["freeze_sha256"] == training.sha256(freeze_path)
        # Replaying source games ties held-out rows to their recorded legal
        # moves; this also catches an unrelated generated position or wrong ply.
        replayed = {}
        for game in prior.read_rows(holdout / "raw_games.jsonl"):
            board = chess.Board()
            for ply, text in enumerate(game["moves_uci"], 1):
                assert chess.Move.from_uci(text) in board.legal_moves
                board.push_uci(text)
                replayed[game["group_id"], ply] = board.fen(en_passant="fen")
        assert all(replayed[row["group_id"], row["ply"]] == row["fen"] for row in held_rows)
        report = experiment.evaluate_holdout(selected, holdout, root / "evaluation.json", engine)
        assert report["python_cpp_mismatches"] == 0
        assert report["positions"] == len(held_rows)
        # Changing unseen labels changes errors, while fixed models predict
        # identically. This pure comparison cannot train or select a model.
        altered = copy.deepcopy(held_rows)
        for row in altered:
            row["target_white_cp"] += 137
            row["classical_stm_cp"] = row["target_white_cp"] * (1 if chess.Board(row["fen"]).turn else -1)
        changed_report = experiment.compare_records(altered, baseline, model)
        assert report["predictions"] == changed_report["predictions"]
        assert report["metrics"] != changed_report["metrics"]
        assert before == {file.name: file.read_bytes() for file in selected.iterdir() if file.is_file()}
        # Even if a caller re-hashes forged labels, the real engine teacher
        # must reject them; a frozen result never silently accepts new targets.
        bad_labels = root / "bad-labels"
        shutil.copytree(holdout, bad_labels)
        replace_holdout(bad_labels, altered)
        assert_rejects(lambda: experiment.evaluate_holdout(selected, bad_labels, root / "bad-eval.json", engine), "teacher")
        assert not (root / "bad-eval.json").exists()
        # Input/mirror leakage is checked independently of the manifest hash.
        leaked = root / "leaked"
        shutil.copytree(holdout, leaked)
        rows = copy.deepcopy(held_rows)
        duplicate = dict(partitions["train"][0])
        board = chess.Board(duplicate["fen"]).mirror()
        duplicate.update({"fen": board.fen(en_passant="fen"), "group_id": rows[0]["group_id"],
                          "target_white_cp": -duplicate["target_white_cp"]})
        rows[0] = duplicate
        replace_holdout(leaked, rows)
        assert_rejects(lambda: experiment.evaluate_holdout(selected, leaked, root / "leak-eval.json", engine), "overlap")
        # A changed selected model remains a syntactically valid format-v1
        # file, but the frozen hash blocks it before any held-out evaluation.
        model_path.write_bytes(before["model.nnue"] + b"\n")
        assert_rejects(lambda: experiment.evaluate_holdout(selected, holdout, root / "mutated-eval.json", engine), "checksum")
        model_path.write_bytes(before["model.nnue"])
        qat.verify_freeze(freeze_path)
        assert before == {file.name: file.read_bytes() for file in selected.iterdir() if file.is_file()}
        print(json.dumps({"training_rows": len(partitions["train"]),
            "old_validation_rows": len(partitions["validation"]), "new_holdout_rows": len(held_rows),
            "selected_candidate_id": selection["selected_candidate_id"],
            "validation_never_opened_during_selection": True,
            "holdout_label_perturbation_changes_only_metrics": True,
            "frozen_artifacts_unchanged": True, "adversarial_rejections": 3,
            "python_full_incremental_positions": len(fens), "uci_parity_pairs": uci_pairs,
            "trained_model_cpp_tests": cpp_output.splitlines(),
            "model_checksum_fnv1a64": freeze["model_checksum_fnv1a64"]}, indent=2))


if __name__ == "__main__":
    main()
