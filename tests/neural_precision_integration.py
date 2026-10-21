"""Tiny format-v2 training/export and independent C++ full/incremental checks.

All seeds here are test-only seeds, separate from recorded research training or
fresh evaluation runs. Files are temporary, and held-out test games are created
only after the selected model and settings have been frozen.
"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest import mock

import chess
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import neural_training as base
import neural_precision as precision
import neural_training_integration as prior


def must_reject(action) -> None:
    try:
        action()
    except ValueError:
        return
    raise AssertionError("invalid or changed model was accepted")


def error_metrics(prediction: np.ndarray, target: np.ndarray) -> dict:
    residual = prediction - target
    return {"mae_cp": float(np.mean(np.abs(residual))),
            "rmse_cp": float(np.sqrt(np.mean(residual**2)))}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", required=True, type=Path)
    parser.add_argument("--model-tests", type=Path)
    args = parser.parse_args()
    engine = args.engine.resolve()
    model_tests = (args.model_tests or engine.with_name(
        "regicide_neural_model_tests" + engine.suffix)).resolve()
    assert engine.is_file() and model_tests.is_file(), "build engine and model tests first"
    grid = [{"candidate_id": "integration-a", "input_scale": 8, "epochs": 4,
             "input_bias_lr": .03, "output_lr": .02, "seed": 20261029, "batch_size": 16},
            {"candidate_id": "integration-b", "input_scale": 16, "epochs": 6,
             "input_bias_lr": .05, "output_lr": .02, "seed": 20261029, "batch_size": 16}]
    with tempfile.TemporaryDirectory(prefix="regicide-precision-integration-") as temporary:
        root = Path(temporary)
        data = root / "data"
        base.generate_dataset(engine, data, games=8, max_plies=24, sample_every=2, seed=61137)
        _, partitions = base.read_dataset(data)
        initial = base.bootstrap_float()
        np.savez(root / "initial.npz", **initial)
        baseline_path = root / "baseline.nnue"
        base.save_model(base.quantize(initial), baseline_path)
        selected = root / "selected"
        original_open = Path.open
        def guarded_open(file, *args, **kwargs):
            if file.name in {"validation.jsonl", "holdout.jsonl", "holdout-comparison.json"}:
                raise AssertionError("selection opened held-out contents")
            return original_open(file, *args, **kwargs)
        with mock.patch.object(precision, "FIXED_GRID", grid), \
                mock.patch.object(Path, "open", guarded_open):
            selection = precision.select_model(data, baseline_path, root / "initial.npz", selected)
        precision.verify_freeze(selected / "freeze.json")
        frozen_bytes = {file.relative_to(selected).as_posix(): file.read_bytes()
                        for file in selected.rglob("*") if file.is_file()}
        model_path = selected / "model.nnue"
        model = precision.load_model(model_path)
        same_scale_initial = precision.quantize(initial, model["input_scale"])
        changed_inputs = int(np.count_nonzero(model["input_weights"] != same_scale_initial["input_weights"]))
        assert changed_inputs > 0, "reported input change was only a precision scale conversion"
        roundtrip = root / "roundtrip.nnue"
        precision.save_model(model, roundtrip)
        assert roundtrip.read_bytes() == model_path.read_bytes()
        assert model_path.read_text(encoding="ascii").splitlines()[0] == (
            f"REGICIDE_NNUE 2 768 16 {127 * model['input_scale']} "
            f"{8 * model['input_scale']} {model['input_scale']}")
        must_reject(lambda: precision.save_model(model, model_path))
        tokens = model_path.read_text(encoding="ascii").split()
        tokens[-1] = str(int(tokens[-1]) ^ 1)
        corrupt = root / "corrupt.nnue"
        corrupt.write_text(" ".join(tokens) + "\n", encoding="ascii")
        must_reject(lambda: precision.load_model(corrupt))
        rejected = subprocess.run([str(engine), "eval-batch", "--nnue-model", str(corrupt)],
            input=chess.STARTING_FEN + "\n", capture_output=True, text=True, timeout=10)
        assert rejected.returncode != 0, "C++ accepted corrupt v2 model"

        # Unseen test games use distinct RNG and group names, after freezing.
        fresh = root / "fresh"
        base.generate_dataset(engine, fresh, games=4, max_plies=24, sample_every=2, seed=81139)
        _, fresh_partitions = base.read_dataset(fresh)
        original_features = {row["feature_hash"] for rows in partitions.values() for row in rows}
        heldout = []
        for rows in fresh_partitions.values():
            for row in rows:
                if row["feature_hash"] not in original_features:
                    row["group_id"] = "precision-heldout-test-" + row["group_id"]
                    heldout.append(row)
        assert heldout
        assert not {row["group_id"] for row in heldout} & {
            row["group_id"] for rows in partitions.values() for row in rows}
        fens = [row["fen"] for rows in partitions.values() for row in rows]
        fens += [row["fen"] for row in heldout] + prior.special_positions()
        builtin = base.evaluate_engine(engine, fens)
        legacy = base.evaluate_engine(engine, fens, baseline_path)
        scores = base.evaluate_engine(engine, fens, model_path)
        loaded_shadows = precision.load_float_parameters(selected / "float_weights.npz")
        packed, _ = base.pack_records([{"fen": fen, "target_white_cp": 0} for fen in fens])
        predicted, _ = precision.qat_forward(loaded_shadows, packed, model["input_scale"])
        for index, (fen, triple) in enumerate(zip(fens, scores, strict=True)):
            board = chess.Board(fen)
            white = precision.quantized_white(board, model)
            score = white * (1 if board.turn else -1)
            assert predicted[index] == white, (fen, predicted[index], white)
            assert triple[1] == triple[2] == score, (fen, triple, score)
            assert triple[0] == legacy[index][0] == builtin[index][0], "classical evaluator changed"
        # A 4.26e9 numerator exposes accidental signed-32-bit output arithmetic.
        extreme = {"input_weights": np.zeros((768, 16), dtype=np.float64),
                   "hidden_bias": np.zeros(16), "output_weights": np.full(16, 1024.0)}
        extreme["input_weights"][5 * 64 + chess.G1] = 127
        extreme_path = root / "wide-numerator.nnue"
        precision.save_model(precision.quantize(extreme, 256), extreme_path)
        extreme_fen = "7k/8/8/8/8/8/P7/6K1 w - - 0 1"
        assert base.evaluate_engine(engine, [extreme_fen], extreme_path)[0][1:] == (28000, 28000)
        assert precision.quantized_white(chess.Board(extreme_fen), precision.load_model(extreme_path)) == 28000
        cpp_output = prior.run([str(model_tests), str(model_path)])
        assert "0 failures" in cpp_output, cpp_output
        uci_pairs = prior.test_uci(engine, model_path, corrupt,
            [chess.STARTING_FEN, "7k/8/8/3pP3/8/8/8/6K1 w - d6 0 1",
             "1r5k/P7/8/8/8/8/8/6K1 w - - 0 1"])
        packed, target = base.pack_records(heldout)
        fixed_predictions = precision.predict(model, packed)
        original_metrics = error_metrics(fixed_predictions, target)
        changed_metrics = error_metrics(fixed_predictions, target + 137)
        assert original_metrics != changed_metrics
        np.testing.assert_array_equal(fixed_predictions, precision.predict(model, packed))
        assert frozen_bytes == {file.relative_to(selected).as_posix(): file.read_bytes()
                                for file in selected.rglob("*") if file.is_file()}
        original = model_path.read_bytes()
        model_path.write_bytes(original + b"\n")
        must_reject(lambda: precision.verify_freeze(selected / "freeze.json"))
        model_path.write_bytes(original)
        precision.verify_freeze(selected / "freeze.json")
        print(json.dumps({"training_rows": len(partitions["train"]),
            "old_validation_rows": len(partitions["validation"]),
            "new_test_holdout_rows": len(heldout),
            "selected_candidate_id": selection["selected_candidate_id"],
            "selected_input_scale": int(model["input_scale"]),
            "input_integer_changes_vs_same_scale_initializer": changed_inputs,
            "selection_never_opens_validation_or_previous_holdout": True,
            "holdout_generated_after_freeze": True,
            "heldout_label_change_affects_metrics_only": True,
            "frozen_artifacts_unchanged": True,
            "v1_load_and_classical_unchanged": True,
            "python_full_incremental_positions": len(fens),
            "signed_64bit_numerator_checked": True,
            "uci_parity_pairs": uci_pairs,
            "trained_model_cpp_tests": cpp_output.splitlines()}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
