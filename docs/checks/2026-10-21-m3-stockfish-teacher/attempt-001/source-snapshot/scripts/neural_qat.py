"""Deterministic quantization-aware calibration for Regicide format-v1 models.

Candidate selection reads only the original training partition.  Its ranking is
in-sample calibration, not an unseen validation estimate.  A separate experiment
must create and evaluate fresh held-out games after the immutable freeze file.
The forward pass is exactly the deployed integer network; the backward pass uses
an explicitly approximate straight-through estimator (STE), not the mathematical
derivative of integer rounding or truncation.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import platform
import subprocess
import sys

import chess
import numpy as np

import neural_training as base

REPOSITORY = Path(__file__).resolve().parents[1]
PARAMETER_KEYS = ("input_weights", "hidden_bias", "output_weights")
BOUNDS = {"input_weights": 127.0, "hidden_bias": 4096.0,
          "output_weights": 8192.0 / base.OUTPUT_SCALE}
SHAPES = {"input_weights": (base.FEATURES, base.HIDDEN),
          "hidden_bias": (base.HIDDEN,), "output_weights": (base.HIDDEN,)}
FIXED_SEED = 20261021
# This grid is part of the source, and grid_plan.json is written before training.
FIXED_GRID = [
    {"candidate_id": "lr005-e060", "epochs": 60, "input_bias_lr": 0.005,
     "output_lr": 0.01, "seed": FIXED_SEED, "batch_size": 128},
    {"candidate_id": "lr005-e120", "epochs": 120, "input_bias_lr": 0.005,
     "output_lr": 0.01, "seed": FIXED_SEED, "batch_size": 128},
    {"candidate_id": "lr020-e060", "epochs": 60, "input_bias_lr": 0.02,
     "output_lr": 0.01, "seed": FIXED_SEED, "batch_size": 128},
    {"candidate_id": "lr020-e120", "epochs": 120, "input_bias_lr": 0.02,
     "output_lr": 0.01, "seed": FIXED_SEED, "batch_size": 128},
]


def _write_exclusive_json(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(value, indent=2, sort_keys=True) + "\n")


def _validate_parameters(parameters: dict) -> None:
    if set(parameters) != set(PARAMETER_KEYS):
        raise ValueError("Unexpected float parameter keys")
    for key in PARAMETER_KEYS:
        values = np.asarray(parameters[key])
        if values.shape != SHAPES[key] or not np.issubdtype(values.dtype, np.floating):
            raise ValueError(f"Invalid float parameter shape or type: {key}")
        if not np.all(np.isfinite(values)):
            raise ValueError(f"Nonfinite float parameter: {key}")


def _validate_batch(indices: np.ndarray, targets: np.ndarray | None = None) -> None:
    if not isinstance(indices, np.ndarray) or indices.ndim != 3 or indices.shape[1:] != (2, 32):
        raise ValueError("Expected nonempty N x 2 x 32 packed feature indices")
    if len(indices) == 0 or not np.issubdtype(indices.dtype, np.integer):
        raise ValueError("Empty or noninteger packed features")
    if np.any(indices < 0) or np.any(indices > base.FEATURES):
        raise ValueError("Packed feature index out of range")
    if targets is not None and (np.asarray(targets).shape != (len(indices),)
                                or not np.all(np.isfinite(targets))):
        raise ValueError("Invalid training targets")


def qat_forward(parameters: dict, indices: np.ndarray) -> tuple[np.ndarray, dict]:
    """Exact runtime forward pass from float shadows, with a gradient cache.

    Input weights and bias round to integer (ties to even); output weights round
    after multiplying by 8. Accumulators and the final numerator use int64, with
    one truncation toward zero and a final +/-28000 clamp, as in C++ format v1.
    """
    _validate_parameters(parameters)
    _validate_batch(indices)
    model = base.quantize(parameters)
    weights = np.vstack((model["input_weights"], np.zeros((1, base.HIDDEN), dtype=np.int64)))
    sums = weights[indices].sum(axis=2) + model["hidden_bias"]
    activated = np.clip(sums, 0, base.CLIP)
    differences = activated[:, 0] - activated[:, 1]
    numerator = differences @ model["output_weights"]
    raw_scores = numerator.astype(np.float64) / base.OUTPUT_SCALE
    # Every bounded format-v1 numerator is exactly representable in float64.
    predictions = np.clip(np.trunc(raw_scores), -28000, 28000)
    return predictions, {"quantized_model": model, "sums": sums,
                         "differences": differences, "raw_scores": raw_scores}


def qat_gradients(parameters: dict, indices: np.ndarray, targets: np.ndarray) -> dict:
    """MSE gradient surrogate through integer rounding/truncation.

    STE replaces each rounding/truncation derivative by one. Clipped ReLU and
    score clamp have zero derivative at and outside their endpoints. Parameter
    clip derivative is one inside and at the bounds, zero outside; Adam clips
    shadow parameters after each update. This is a surrogate, not a finite-
    difference gradient of the piecewise-constant deployed integer function.
    """
    _validate_batch(indices, targets)
    predictions, cache = qat_forward(parameters, indices)
    derivative = 2 * (predictions - targets) / (len(targets) * 10000)
    derivative *= (cache["raw_scores"] > -28000) & (cache["raw_scores"] < 28000)
    quantized_outputs = cache["quantized_model"]["output_weights"] / base.OUTPUT_SCALE
    hidden_gradient = derivative[:, None, None] * quantized_outputs[None, None, :]
    hidden_gradient = np.broadcast_to(hidden_gradient, cache["sums"].shape).copy()
    hidden_gradient[:, 1] *= -1
    hidden_gradient *= (cache["sums"] > 0) & (cache["sums"] < base.CLIP)
    # bincount performs the same repeated-feature accumulation as np.add.at,
    # without materializing a B x 2 x 32 x H sparse-gradient tensor.
    flat_indices = indices.ravel()
    repeated = np.repeat(hidden_gradient.reshape(-1, base.HIDDEN), indices.shape[2], axis=0)
    input_gradient = np.column_stack([
        np.bincount(flat_indices, weights=repeated[:, h], minlength=base.FEATURES + 1)
        for h in range(base.HIDDEN)])
    gradients = {"input_weights": input_gradient[:-1],
                 "hidden_bias": hidden_gradient.sum(axis=(0, 1)),
                 "output_weights": cache["differences"].T @ derivative}
    for key, values in gradients.items():
        values *= (parameters[key] >= -BOUNDS[key]) & (parameters[key] <= BOUNDS[key])
    return gradients


def load_training_partition(data_dir: Path | str) -> tuple[dict, list[dict]]:
    """Check and read train.jsonl only; do not open validation labels or data."""
    data_dir = Path(data_dir)
    manifest = json.loads((data_dir / "split_manifest.json").read_text(encoding="utf-8"))
    metadata = manifest["partitions"]["train"]
    if metadata["file"] != "train.jsonl":
        raise ValueError("Unexpected training partition filename")
    path = data_dir / "train.jsonl"
    if base.sha256(path) != metadata["sha256"]:
        raise ValueError("Training partition checksum mismatch")
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    if not rows or len(rows) != metadata["count"]:
        raise ValueError("Empty or inconsistent training partition")
    identities = set()
    for row in rows:
        board = chess.Board(row["fen"])
        identity = base.feature_identity(board)
        if not board.is_valid() or board.outcome(claim_draw=True) is not None:
            raise ValueError("Training includes invalid or terminal position")
        if row["feature_hash"] != identity or identity in identities:
            raise ValueError("Invalid or duplicate training feature identity")
        if manifest["groups"].get(row["group_id"]) != "train":
            raise ValueError("Training game is outside assigned training partition")
        if type(row["target_white_cp"]) is not int or type(row["classical_stm_cp"]) is not int:
            raise ValueError("Training targets must be integer centipawns")
        if row["target_white_cp"] != row["classical_stm_cp"] * (1 if board.turn else -1):
            raise ValueError("Inconsistent target score perspective")
        identities.add(identity)
    return manifest, rows


def load_float_parameters(path: Path | str) -> dict:
    with np.load(path, allow_pickle=False) as source:
        parameters = {key: source[key].astype(np.float64, copy=True) for key in source.files}
    _validate_parameters(parameters)
    if any(np.any(np.abs(value) > BOUNDS[key]) for key, value in parameters.items()):
        raise ValueError("Initial shadow parameter outside model bounds")
    return parameters


def _validate_config(config: dict) -> None:
    expected = {"candidate_id", "epochs", "input_bias_lr", "output_lr", "seed", "batch_size"}
    if set(config) != expected:
        raise ValueError("Unexpected candidate configuration keys")
    if not isinstance(config["candidate_id"], str) or not config["candidate_id"]:
        raise ValueError("Candidate ID must be a nonempty string")
    for key, lower, upper in (("epochs", 1, 10000), ("batch_size", 1, 65536), ("seed", 0, 2**32 - 1)):
        if type(config[key]) is not int or not lower <= config[key] <= upper:
            raise ValueError(f"Invalid candidate {key}")
    for key in ("input_bias_lr", "output_lr"):
        if isinstance(config[key], bool) or not isinstance(config[key], (float, int)):
            raise ValueError(f"Invalid candidate {key}")
        if not math.isfinite(config[key]) or not 0 < config[key] <= 1:
            raise ValueError(f"Invalid candidate {key}")


def train_candidate(initial_parameters: dict, indices: np.ndarray,
                    targets: np.ndarray, config: dict) -> tuple[dict, list[dict]]:
    """Train a fixed final epoch without early stopping or held-out access."""
    _validate_parameters(initial_parameters)
    _validate_batch(indices, targets)
    _validate_config(config)
    parameters = {key: value.astype(np.float64, copy=True) for key, value in initial_parameters.items()}
    if any(np.any(np.abs(value) > BOUNDS[key]) for key, value in parameters.items()):
        raise ValueError("Initial shadow parameter outside model bounds")
    first = {key: np.zeros_like(values) for key, values in parameters.items()}
    second = {key: np.zeros_like(values) for key, values in parameters.items()}
    rates = {"input_weights": config["input_bias_lr"], "hidden_bias": config["input_bias_lr"],
             "output_weights": config["output_lr"]}
    rng = np.random.default_rng(config["seed"])
    history = []
    step = 0
    for epoch in range(1, config["epochs"] + 1):
        order = rng.permutation(len(targets))
        max_gradient_norm = 0.0
        clipped_batches = 0
        for offset in range(0, len(order), config["batch_size"]):
            chosen = order[offset:offset + config["batch_size"]]
            gradients = qat_gradients(parameters, indices[chosen], targets[chosen])
            norm = math.sqrt(sum(float(np.sum(value * value)) for value in gradients.values()))
            max_gradient_norm = max(max_gradient_norm, norm)
            clipped_batches += int(norm > 10.0)
            factor = min(1.0, 10.0 / max(norm, 1e-30))
            step += 1
            for key, parameter in parameters.items():
                gradient = gradients[key] * factor
                first[key] *= 0.9
                first[key] += 0.1 * gradient
                second[key] *= 0.999
                second[key] += 0.001 * gradient * gradient
                estimate = ((first[key] / (1 - 0.9 ** step))
                            / (np.sqrt(second[key] / (1 - 0.999 ** step)) + 1e-8))
                parameter -= rates[key] * estimate
                np.clip(parameter, -BOUNDS[key], BOUNDS[key], out=parameter)
                if not np.all(np.isfinite(parameter)):
                    raise ValueError("Nonfinite QAT shadow parameter")
        predictions, _ = qat_forward(parameters, indices)
        history.append({"epoch": epoch, "optimizer_steps": step,
                        "fake_quantized_train": base.error_metrics(predictions, targets),
                        "shadow_float_train": base.error_metrics(base.forward(parameters, indices)[0], targets),
                        "max_batch_gradient_global_norm": max_gradient_norm,
                        "gradient_clipped_batches": clipped_batches})
    return parameters, history


def _file_entry(path: Path | str) -> dict:
    path = Path(path).resolve()
    try:
        text_path = path.relative_to(REPOSITORY).as_posix()
    except ValueError:
        text_path = str(path)
    return {"path": text_path, "sha256": base.sha256(path)}


def resolve_frozen_file(entry: dict) -> Path:
    path = Path(entry["path"])
    return path if path.is_absolute() else REPOSITORY / path


def verify_freeze(freeze_path: Path | str) -> dict:
    """Refuse evaluation if selected artifacts or training dependencies changed."""
    freeze = json.loads(Path(freeze_path).read_text(encoding="utf-8"))
    expected = {"qat_source", "base_training_source", "dataset_manifest", "training_data",
                "baseline_model", "initial_floats", "grid_plan", "selection", "selected_model", "selected_floats"}
    if freeze.get("schema_version") != 1 or set(freeze.get("files", {})) != expected:
        raise ValueError("Unsupported or incomplete QAT freeze")
    for name, entry in freeze["files"].items():
        path = resolve_frozen_file(entry)
        if base.sha256(path) != entry["sha256"]:
            raise ValueError(f"Frozen dependency checksum mismatch: {name}")
    selection = json.loads(resolve_frozen_file(freeze["files"]["selection"]).read_text(encoding="utf-8"))
    if selection["selected_candidate_id"] != freeze["selected_candidate_id"]:
        raise ValueError("Frozen selection ID mismatch")
    if base.model_checksum(base.load_model(resolve_frozen_file(freeze["files"]["selected_model"]))) != freeze["model_checksum_fnv1a64"]:
        raise ValueError("Frozen model checksum mismatch")
    return freeze


def select_model(data_dir: Path | str, baseline_model_path: Path | str,
                 initial_float_path: Path | str, output_dir: Path | str) -> dict:
    """Write all candidate evidence and freeze a train-only selected final model."""
    data_dir, output_dir = Path(data_dir), Path(output_dir)
    baseline_model_path, initial_float_path = Path(baseline_model_path), Path(initial_float_path)
    manifest, rows = load_training_partition(data_dir)
    initial = load_float_parameters(initial_float_path)
    baseline = base.load_model(baseline_model_path)
    initial_integer = base.quantize(initial)
    if base.model_checksum(initial_integer) != base.model_checksum(baseline):
        raise ValueError("Initial float checkpoint does not quantize to the supplied baseline")
    indices, targets = base.pack_records(rows)
    base.fresh_directory(output_dir)
    files = {"qat_source": _file_entry(Path(__file__)),
             "base_training_source": _file_entry(Path(base.__file__)),
             "dataset_manifest": _file_entry(data_dir / "split_manifest.json"),
             "training_data": _file_entry(data_dir / "train.jsonl"),
             "baseline_model": _file_entry(baseline_model_path),
             "initial_floats": _file_entry(initial_float_path)}
    plan = {"schema_version": 1, "candidate_grid": FIXED_GRID,
            "ranking": "Final serialized integer train MAE, then train RMSE, then grid index, ascending",
            "selection_data": "Original training partition only; no validation or held-out rows read",
            "initialization": "Prior float checkpoint trained on the complete original training partition",
            "calibration_limitation": "Ranking is in-sample training calibration, not independent validation",
            "model_format": "REGICIDE_NNUE 1 768 16 127 8", "files": files}
    _write_exclusive_json(output_dir / "grid_plan.json", plan)
    candidates = []
    baseline_predictions, _ = qat_forward(initial, indices)
    for index, config in enumerate(FIXED_GRID):
        candidate_dir = output_dir / config["candidate_id"]
        candidate_dir.mkdir()
        parameters, history = train_candidate(initial, indices, targets, config)
        model = base.quantize(parameters)
        base.save_model(model, candidate_dir / "model.nnue")
        loaded = base.load_model(candidate_dir / "model.nnue")
        predictions, _ = qat_forward(parameters, indices)
        exact = np.array([base.quantized_white(chess.Board(row["fen"]), loaded) for row in rows])
        if not np.array_equal(predictions, exact):
            raise ValueError("QAT fake forward differs from serialized model")
        if base.model_checksum(loaded) != base.model_checksum(model):
            raise ValueError("QAT model save/load checksum mismatch")
        np.savez(candidate_dir / "float_weights.npz", **parameters)
        record = {"grid_index": index, "config": config,
                  "train": base.error_metrics(exact, targets),
                  "fake_forward_serialized_exact_rows": len(rows),
                  "changed_integer_parameters": {key: int(np.count_nonzero(model[key] != baseline[key])) for key in PARAMETER_KEYS},
                  "changed_float_parameters": {key: int(np.count_nonzero(parameters[key] != initial[key])) for key in PARAMETER_KEYS},
                  "model_sha256": base.sha256(candidate_dir / "model.nnue"),
                  "model_checksum_fnv1a64": base.model_checksum(model),
                  "float_weights_sha256": base.sha256(candidate_dir / "float_weights.npz"),
                  "training_history": history}
        _write_exclusive_json(candidate_dir / "metrics.json", record)
        candidates.append(record)
    ranking = sorted(candidates, key=lambda item: (item["train"]["mae_cp"], item["train"]["rmse_cp"], item["grid_index"]))
    winner = ranking[0]
    selected_dir = output_dir / winner["config"]["candidate_id"]
    selected_model = base.load_model(selected_dir / "model.nnue")
    base.save_model(selected_model, output_dir / "model.nnue")
    # Preserve exact checkpoint bytes instead of reserializing an NPZ archive.
    with (output_dir / "float_weights.npz").open("xb") as handle:
        handle.write((selected_dir / "float_weights.npz").read_bytes())
    selection = {"schema_version": 1, "selected_candidate_id": winner["config"]["candidate_id"],
                 "selected_config": winner["config"], "selection_policy": plan["ranking"],
                 "selection_data": plan["selection_data"], "calibration_limitation": plan["calibration_limitation"],
                 "training_rows": len(rows), "training_game_count": len({row["group_id"] for row in rows}),
                 "baseline_train": base.error_metrics(baseline_predictions, targets),
                 "ranking": [{key: item[key] for key in ("grid_index", "config", "train", "model_sha256", "changed_integer_parameters")}
                             for item in ranking],
                 "optimizer": "Adam beta1=0.9 beta2=0.999 epsilon=1e-8; float64 shadows and moments",
                 "loss": "Mean squared error in pawn units, with exact integer deployed forward",
                 "gradient_global_norm_limit": 10.0,
                 "ste": "Rounding and final truncation derivative=1 surrogate; strict clipped-ReLU and score-clamp interior; parameter-clip derivative=1 at/in bounds",
                 "quantization": "Nearest ties-to-even; input/bias integer, output times8; int64 accumulators; one white-score truncation toward zero; clamp +/-28000",
                 "python": platform.python_version(), "numpy": np.__version__, "python_chess": chess.__version__,
                 "source_dataset_engine_sha256": manifest["engine_sha256"],
                 "no_playing_strength_claim": True}
    _write_exclusive_json(output_dir / "selection.json", selection)
    files.update({"grid_plan": _file_entry(output_dir / "grid_plan.json"),
                  "selection": _file_entry(output_dir / "selection.json"),
                  "selected_model": _file_entry(output_dir / "model.nnue"),
                  "selected_floats": _file_entry(output_dir / "float_weights.npz")})
    freeze = {"schema_version": 1, "selected_candidate_id": selection["selected_candidate_id"],
              "selected_config": winner["config"], "files": files,
              "model_checksum_fnv1a64": base.model_checksum(selected_model),
              "policy": "Written last before creating fresh held-out games; selected model/config never change after held-out evaluation",
              "selection_data": plan["selection_data"], "calibration_limitation": plan["calibration_limitation"]}
    _write_exclusive_json(output_dir / "freeze.json", freeze)
    verify_freeze(output_dir / "freeze.json")
    return selection


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    select = sub.add_parser("select")
    select.add_argument("--data", type=Path, required=True)
    select.add_argument("--baseline-model", type=Path, required=True)
    select.add_argument("--initial-floats", type=Path, required=True)
    select.add_argument("--output", type=Path, required=True)
    check = sub.add_parser("check-freeze")
    check.add_argument("freeze", type=Path)
    args = parser.parse_args()
    if args.command == "select":
        result = select_model(args.data, args.baseline_model, args.initial_floats, args.output)
    else:
        result = verify_freeze(args.freeze)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)
