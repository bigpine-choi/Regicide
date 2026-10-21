"""Train-only fixed-point QAT with unchanged 768 x 16 network topology.

Format v2 stores input weights/bias in 1/S units and clips at 127*S; its
denominator is 8*S, while output weights keep 1/8 units.  Float shadows stay
in original real units.  The exact integer forward and an approximate STE
backward pass are deliberately separate.  Fresh held-out data must be created
only after select_model writes its final immutable freeze record.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import platform
import re
import subprocess
import sys

import chess
import numpy as np

import neural_training as base
import neural_qat as qat

REPOSITORY = Path(__file__).resolve().parents[1]
FEATURES, HIDDEN, CLIP, OUTPUT_SCALE = base.FEATURES, base.HIDDEN, base.CLIP, base.OUTPUT_SCALE
PARAMETER_KEYS = ("input_weights", "hidden_bias", "output_weights")
SHAPES = {"input_weights": (FEATURES, HIDDEN), "hidden_bias": (HIDDEN,),
          "output_weights": (HIDDEN,)}
INTEGER_BOUNDS = {"input_weights": 32767, "hidden_bias": 1048576, "output_weights": 8192}
FIXED_GRID = [{"candidate_id": f"s{scale:03d}-lr{rate:06d}-e120", "input_scale": scale,
               "epochs": 120, "input_bias_lr": rate / 1000000,
               "output_lr": 0.005, "seed": 20261024, "batch_size": 128}
              for scale in (8, 16, 32) for rate in (20, 100)]


def _write_json(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(value, indent=2, sort_keys=True) + "\n")


def _validate_scale(scale: int) -> None:
    if (isinstance(scale, (bool, np.bool_)) or not isinstance(scale, (int, np.integer))
            or not 2 <= scale <= 256 or scale & (scale - 1)):
        raise ValueError("Input scale must be a power of two from 2 through 256")


def _metadata(model: dict) -> tuple[int, int, int, int]:
    version = model.get("format_version", 1)
    if isinstance(version, (bool, np.bool_)) or not isinstance(version, (int, np.integer)):
        raise ValueError("Invalid model version")
    scale, clip, denominator = model.get("input_scale", 1), model.get("clip", CLIP), model["output_scale"]
    for value in (scale, clip, denominator):
        if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, np.integer)):
            raise ValueError("Model metadata must contain integers")
    if not 1 <= denominator <= 65536:
        raise ValueError("Invalid output scale")
    if version == 1:
        if scale != 1 or clip != CLIP:
            raise ValueError("Invalid v1 input scale or activation clip")
    elif version == 2:
        _validate_scale(scale)
        if clip != CLIP * scale or denominator % scale:
            raise ValueError("Invalid v2 activation clip or output scale")
    else:
        raise ValueError("Unsupported model version")
    return int(version), int(scale), int(clip), int(denominator)


def validate_model(model: dict) -> None:
    version, _, _, _ = _metadata(model)
    bounds = INTEGER_BOUNDS if version == 2 else {
        "input_weights": 127, "hidden_bias": 4096, "output_weights": 8192}
    for key, shape in SHAPES.items():
        values = np.asarray(model[key])
        if values.shape != shape or not np.issubdtype(values.dtype, np.integer):
            raise ValueError(f"Invalid shape or type for {key}")
        if np.any(values < -bounds[key]) or np.any(values > bounds[key]):
            raise ValueError(f"Out-of-range {key}")


def model_checksum(model: dict) -> int:
    validate_model(model)
    version, scale, clip, denominator = _metadata(model)
    values = [version, FEATURES, HIDDEN, clip, denominator]
    if version == 2:
        values.append(scale)
    for key in ("hidden_bias", "output_weights", "input_weights"):
        values.extend(int(value) for value in np.asarray(model[key]).flat)
    checksum = 14695981039346656037
    for value in values:
        for byte in (value & 0xFFFFFFFF).to_bytes(4, "little"):
            checksum = ((checksum ^ byte) * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return checksum


def save_model(model: dict, path: Path | str) -> None:
    validate_model(model)
    path = Path(path)
    if path.exists():
        raise ValueError(f"Refusing to overwrite model: {path}")
    version, scale, clip, denominator = _metadata(model)
    header = f"REGICIDE_NNUE {version} {FEATURES} {HIDDEN} {clip} {denominator}"
    if version == 2:
        header += f" {scale}"
    lines = [header]
    for key in ("hidden_bias", "output_weights"):
        lines.append(" ".join(str(int(value)) for value in model[key]))
    lines.extend(" ".join(str(int(value)) for value in row) for row in model["input_weights"])
    lines.append(f"checksum {model_checksum(model)}")
    text = "\n".join(lines) + "\n"
    if len(text.encode("ascii")) > base.MODEL_LIMIT:
        raise ValueError("Model exceeds maximum file size")
    with path.open("x", encoding="ascii", newline="\n") as handle:
        handle.write(text)


def load_model(path: Path | str) -> dict:
    path = Path(path)
    if path.stat().st_size > base.MODEL_LIMIT:
        raise ValueError("Model exceeds maximum file size")
    tokens = path.read_text(encoding="ascii").split()
    if len(tokens) < 3 or tokens[0] != "REGICIDE_NNUE" or tokens[-2] != "checksum":
        raise ValueError("Invalid model header")
    integers = tokens[1:-2] + tokens[-1:]
    if any(len(token) > 20 or not base.INTEGER.fullmatch(token) for token in integers):
        raise ValueError("Invalid model integer")
    values = [int(token) for token in integers]
    version = values[0]
    header_size = 5 if version == 1 else 6 if version == 2 else 0
    if not header_size or len(tokens) != header_size + 1 + HIDDEN * 2 + FEATURES * HIDDEN + 2:
        raise ValueError("Unsupported model version or token count")
    if values[1:3] != [FEATURES, HIDDEN] or not 0 <= values[-1] <= 0xFFFFFFFFFFFFFFFF:
        raise ValueError("Invalid model architecture or checksum range")
    model = {"format_version": version, "input_scale": 1 if version == 1 else values[5],
             "clip": values[3], "output_scale": values[4]}
    _metadata(model)
    bounds = INTEGER_BOUNDS if version == 2 else {
        "input_weights": 127, "hidden_bias": 4096, "output_weights": 8192}
    offset = header_size
    for key, size in (("hidden_bias", HIDDEN), ("output_weights", HIDDEN),
                      ("input_weights", FEATURES * HIDDEN)):
        part = values[offset:offset + size]
        if any(value < -bounds[key] or value > bounds[key] for value in part):
            raise ValueError(f"Out-of-range {key}")
        model[key] = np.array(part, dtype=np.int64).reshape(SHAPES[key])
        offset += size
    validate_model(model)
    if model_checksum(model) != values[-1]:
        raise ValueError("Model checksum mismatch")
    return model


def _validate_parameters(parameters: dict) -> None:
    if set(parameters) != set(PARAMETER_KEYS):
        raise ValueError("Unexpected float parameter keys")
    for key, shape in SHAPES.items():
        values = np.asarray(parameters[key])
        if values.shape != shape or not np.issubdtype(values.dtype, np.floating):
            raise ValueError(f"Invalid float parameter shape or type: {key}")
        if not np.all(np.isfinite(values)):
            raise ValueError(f"Nonfinite float parameter: {key}")


def _bounds(input_scale: int) -> dict:
    _validate_scale(input_scale)
    return {"input_weights": INTEGER_BOUNDS["input_weights"] / input_scale,
            "hidden_bias": INTEGER_BOUNDS["hidden_bias"] / input_scale,
            "output_weights": INTEGER_BOUNDS["output_weights"] / OUTPUT_SCALE}


def load_float_parameters(path: Path | str) -> dict:
    with np.load(path, allow_pickle=False) as source:
        parameters = {key: source[key].astype(np.float64, copy=True) for key in source.files}
    _validate_parameters(parameters)
    return parameters


load_initial = load_float_parameters
load_training_partition = qat.load_training_partition


def quantize(parameters: dict, input_scale: int = 16) -> dict:
    _validate_parameters(parameters)
    bounds = _bounds(input_scale)
    model = {"format_version": 2, "input_scale": int(input_scale),
             "clip": CLIP * int(input_scale), "output_scale": OUTPUT_SCALE * int(input_scale)}
    for key in PARAMETER_KEYS:
        multiplier = OUTPUT_SCALE if key == "output_weights" else input_scale
        model[key] = np.rint(np.clip(parameters[key], -bounds[key], bounds[key]) * multiplier).astype(np.int64)
    validate_model(model)
    return model


def rescale_model(model: dict, input_scale: int) -> dict:
    """Exactly reexpress a lower-precision model without any learning."""
    validate_model(model)
    _validate_scale(input_scale)
    _, old_scale, _, denominator = _metadata(model)
    if input_scale % old_scale:
        raise ValueError("Exact rescaling requires a multiple of the old input scale")
    multiplier = input_scale // old_scale
    result = {"format_version": 2, "input_scale": input_scale,
              "clip": CLIP * input_scale, "output_scale": denominator * multiplier}
    for key in PARAMETER_KEYS:
        result[key] = model[key].astype(np.int64, copy=True) * (1 if key == "output_weights" else multiplier)
    validate_model(result)
    return result


def _integer_forward(model: dict, indices: np.ndarray) -> tuple[np.ndarray, dict]:
    validate_model(model)
    qat._validate_batch(indices)
    _, scale, clip, denominator = _metadata(model)
    weights = np.vstack((model["input_weights"], np.zeros((1, HIDDEN), dtype=np.int64)))
    sums = weights[indices].sum(axis=2) + model["hidden_bias"]
    activations = np.clip(sums, 0, clip)
    differences = activations[:, 0] - activations[:, 1]
    numerators = differences @ model["output_weights"]
    # Bounds keep abs(numerators) far below int64 overflow and 2**53.
    scores = (np.abs(numerators) // denominator) * np.where(numerators >= 0, 1, -1)
    predictions = np.clip(scores, -28000, 28000).astype(np.float64)
    return predictions, {"quantized_model": model, "sums": sums, "differences": differences,
                         "raw_scores": numerators.astype(np.float64) / denominator,
                         "input_scale": scale}


def predict(model: dict, indices: np.ndarray) -> np.ndarray:
    return _integer_forward(model, indices)[0]


def quantized_white(board: chess.Board, model: dict) -> int:
    validate_model(model)
    _, _, clip, denominator = _metadata(model)
    views = base.feature_indices(board)
    activations = [np.clip(model["hidden_bias"] + model["input_weights"][view].sum(axis=0), 0, clip)
                   for view in views]
    numerator = int((activations[0] - activations[1]) @ model["output_weights"])
    score = (abs(numerator) // denominator) * (1 if numerator >= 0 else -1)
    return max(-28000, min(28000, score))


def qat_forward(parameters: dict, indices: np.ndarray, input_scale: int = 16) -> tuple[np.ndarray, dict]:
    return _integer_forward(quantize(parameters, input_scale), indices)


def loss_terms(predictions: np.ndarray, targets: np.ndarray, loss: str = "mse") -> np.ndarray:
    """Per-row pawn-unit loss; H100 is twice standard Huber, delta=100cp.

    Inside and at +/-100cp H100 equals the historical MSE. Outside it is
    2*abs(residual_cp)/100-1. Targets are never clipped or removed.
    """
    if not isinstance(loss, str) or loss not in ("mse", "h100"):
        raise ValueError("Loss must be mse or h100 (fixed delta 100cp)")
    predicted, expected = np.asarray(predictions, dtype=np.float64), np.asarray(targets, dtype=np.float64)
    if predicted.ndim != 1 or predicted.shape != expected.shape or not len(predicted) or not (
            np.all(np.isfinite(predicted)) and np.all(np.isfinite(expected))):
        raise ValueError("Loss requires equally shaped nonempty finite score vectors")
    residual = (predicted - expected) / 100
    return residual ** 2 if loss == "mse" else np.where(np.abs(residual) <= 1, residual ** 2, 2 * np.abs(residual) - 1)


def loss_derivative(predictions: np.ndarray, targets: np.ndarray, loss: str = "mse") -> np.ndarray:
    """d(mean loss)/d(prediction_cp), before STE activation/score masks."""
    loss_terms(predictions, targets, loss)  # Validate shape, finiteness and the fixed loss name.
    # Keep the original MSE arithmetic/order exactly for historical reproduction.
    residual = predictions - targets
    if loss == "h100":
        residual = np.clip(residual, -100, 100)
    return 2 * residual / (len(targets) * 10000)


def qat_gradients(parameters: dict, indices: np.ndarray, targets: np.ndarray,
                  input_scale: int = 16, *, loss: str = "mse") -> dict:
    """MSE/H100 STE in real shadow units; S cancels along each hidden gradient."""
    qat._validate_batch(indices, targets)
    predictions, cache = qat_forward(parameters, indices, input_scale)
    derivative = loss_derivative(predictions, targets, loss)
    derivative *= (cache["raw_scores"] > -28000) & (cache["raw_scores"] < 28000)
    outputs = cache["quantized_model"]["output_weights"] / OUTPUT_SCALE
    hidden_gradient = derivative[:, None, None] * outputs[None, None, :]
    hidden_gradient = np.broadcast_to(hidden_gradient, cache["sums"].shape).copy()
    hidden_gradient[:, 1] *= -1
    hidden_gradient *= (cache["sums"] > 0) & (cache["sums"] < CLIP * input_scale)
    flat_indices = indices.ravel()
    repeated = np.repeat(hidden_gradient.reshape(-1, HIDDEN), indices.shape[2], axis=0)
    input_gradient = np.column_stack([
        np.bincount(flat_indices, weights=repeated[:, h], minlength=FEATURES + 1)
        for h in range(HIDDEN)])
    gradients = {"input_weights": input_gradient[:-1],
                 "hidden_bias": hidden_gradient.sum(axis=(0, 1)),
                 "output_weights": (cache["differences"] / input_scale).T @ derivative}
    for key, bound in _bounds(input_scale).items():
        gradients[key] *= (parameters[key] >= -bound) & (parameters[key] <= bound)
    return gradients


def _validate_config(config: dict) -> None:
    expected = {"candidate_id", "input_scale", "epochs", "input_bias_lr", "output_lr", "seed", "batch_size"}
    if not expected <= set(config) <= expected | {"loss"}:
        raise ValueError("Unexpected candidate configuration keys")
    if not isinstance(config.get("loss", "mse"), str) or config.get("loss", "mse") not in ("mse", "h100"):
        raise ValueError("Loss must be mse or h100 (fixed delta 100cp)")
    if not isinstance(config["candidate_id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]+", config["candidate_id"]):
        raise ValueError("Candidate ID must contain only letters, numbers, underscore or hyphen")
    _validate_scale(config["input_scale"])
    for key, lower, upper in (("epochs", 1, 10000), ("batch_size", 1, 65536), ("seed", 0, 2**32 - 1)):
        if type(config[key]) is not int or not lower <= config[key] <= upper:
            raise ValueError(f"Invalid candidate {key}")
    for key in ("input_bias_lr", "output_lr"):
        if (isinstance(config[key], bool) or not isinstance(config[key], (float, int))
                or not math.isfinite(config[key]) or not 0 < config[key] <= 1):
            raise ValueError(f"Invalid candidate {key}")


def train_candidate(initial_parameters: dict, indices: np.ndarray, targets: np.ndarray,
                    config: dict, *, progress=None) -> tuple[dict, list[dict]]:
    """Train final-epoch weights; optional observational copies every 10 epochs.

    progress(epoch, parameter_copies, history_entry_copy) cannot change optimizer
    arrays. Its observations never select an epoch or alter the training order.
    Historical configurations without loss keep MSE and the same arithmetic.
    """
    _validate_parameters(initial_parameters)
    qat._validate_batch(indices, targets)
    _validate_config(config)
    if progress is not None and not callable(progress):
        raise ValueError("progress must be callable or None")
    bounds = _bounds(config["input_scale"])
    parameters = {key: value.astype(np.float64, copy=True) for key, value in initial_parameters.items()}
    if any(np.any(np.abs(values) > bounds[key]) for key, values in parameters.items()):
        raise ValueError("Initial shadow parameter outside model bounds")
    first = {key: np.zeros_like(values) for key, values in parameters.items()}
    second = {key: np.zeros_like(values) for key, values in parameters.items()}
    rates = {"input_weights": config["input_bias_lr"], "hidden_bias": config["input_bias_lr"],
             "output_weights": config["output_lr"]}
    rng, history, step = np.random.default_rng(config["seed"]), [], 0
    progress_window = []
    for epoch in range(1, config["epochs"] + 1):
        order = rng.permutation(len(targets))
        maximum_norm, clipped_batches = 0.0, 0
        batch_norms = []
        for offset in range(0, len(order), config["batch_size"]):
            chosen = order[offset:offset + config["batch_size"]]
            gradients = qat_gradients(parameters, indices[chosen], targets[chosen], config["input_scale"],
                                      loss=config.get("loss", "mse"))
            norm = math.sqrt(sum(float(np.sum(value * value)) for value in gradients.values()))
            maximum_norm = max(maximum_norm, norm)
            if progress is not None:
                batch_norms.append(norm)
            clipped_batches += int(norm > 10)
            factor, step = min(1.0, 10.0 / max(norm, 1e-30)), step + 1
            for key, parameter in parameters.items():
                gradient = gradients[key] * factor
                first[key] *= 0.9
                first[key] += 0.1 * gradient
                second[key] *= 0.999
                second[key] += 0.001 * gradient * gradient
                estimate = ((first[key] / (1 - 0.9 ** step))
                            / (np.sqrt(second[key] / (1 - 0.999 ** step)) + 1e-8))
                parameter -= rates[key] * estimate
                np.clip(parameter, -bounds[key], bounds[key], out=parameter)
                if not np.all(np.isfinite(parameter)):
                    raise ValueError("Nonfinite fixed-point QAT shadow")
        predictions, _ = qat_forward(parameters, indices, config["input_scale"])
        history.append({"epoch": epoch, "optimizer_steps": step,
                        "fake_quantized_train": base.error_metrics(predictions, targets),
                        "shadow_float_train": base.error_metrics(base.forward(parameters, indices)[0], targets),
                        "max_batch_gradient_global_norm": maximum_norm,
                        "gradient_clipped_batches": clipped_batches})
        if progress is not None:
            progress_window.append({"epoch": epoch, "batch_gradient_global_norms": batch_norms})
            if epoch % 10 == 0:
                observed = dict(history[-1], batch_gradient_observations=progress_window)
                progress(epoch, {key: value.copy() for key, value in parameters.items()}, observed)
                progress_window = []
    return parameters, history


def _entry(path: Path | str) -> dict:
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
    freeze = json.loads(Path(freeze_path).read_text(encoding="utf-8"))
    required = {"precision_source", "base_training_source", "qat_source", "dataset_manifest", "training_data",
                "baseline_model", "initial_floats", "grid_plan", "selection", "selected_model", "selected_floats"}
    names = set(freeze.get("files", {}))
    if freeze.get("schema_version") != 2 or names not in (required, required | {"calibration_plan"}):
        raise ValueError("Unsupported or incomplete precision freeze")
    for name, entry in freeze["files"].items():
        if base.sha256(resolve_frozen_file(entry)) != entry["sha256"]:
            raise ValueError(f"Frozen dependency checksum mismatch: {name}")
    selection = json.loads(resolve_frozen_file(freeze["files"]["selection"]).read_text(encoding="utf-8"))
    if (selection["selected_candidate_id"] != freeze["selected_candidate_id"]
            or selection["selected_config"] != freeze["selected_config"]):
        raise ValueError("Frozen selection configuration mismatch")
    selected = load_model(resolve_frozen_file(freeze["files"]["selected_model"]))
    if model_checksum(selected) != freeze["model_checksum_fnv1a64"]:
        raise ValueError("Frozen model checksum mismatch")
    if selected["input_scale"] != freeze["selected_config"]["input_scale"]:
        raise ValueError("Frozen model precision mismatch")
    initial = load_float_parameters(resolve_frozen_file(freeze["files"]["initial_floats"]))
    reference = quantize(initial, selected["input_scale"])
    if not np.any(selected["input_weights"] != reference["input_weights"]):
        raise ValueError("Selected model has no learned integer input changes")
    return freeze


def _changed(model: dict, reference: dict) -> dict:
    return {key: int(np.count_nonzero(model[key] != reference[key])) for key in PARAMETER_KEYS}


def select_model(data_dir: Path | str, baseline_model_path: Path | str,
                 initial_float_path: Path | str, output_dir: Path | str,
                 plan_path: Path | str | None = None) -> dict:
    """Select on TRAIN only, then bind configuration and all inputs before holdout."""
    data_dir, output_dir = Path(data_dir), Path(output_dir)
    baseline_model_path, initial_float_path = Path(baseline_model_path), Path(initial_float_path)
    manifest, rows = load_training_partition(data_dir)
    initial, baseline = load_float_parameters(initial_float_path), load_model(baseline_model_path)
    grid = FIXED_GRID if plan_path is None else json.loads(Path(plan_path).read_text(encoding="utf-8"))["candidate_grid"]
    if not isinstance(grid, list) or not 1 <= len(grid) <= 16:
        raise ValueError("Candidate grid must contain 1 through 16 configurations")
    for config in grid:
        _validate_config(config)
    if len({config["candidate_id"] for config in grid}) != len(grid):
        raise ValueError("Duplicate candidate ID")
    indices, targets = base.pack_records(rows)
    base.fresh_directory(output_dir)
    files = {"precision_source": _entry(Path(__file__)), "base_training_source": _entry(Path(base.__file__)),
             "qat_source": _entry(Path(qat.__file__)), "dataset_manifest": _entry(data_dir / "split_manifest.json"),
             "training_data": _entry(data_dir / "train.jsonl"), "baseline_model": _entry(baseline_model_path),
             "initial_floats": _entry(initial_float_path)}
    if plan_path is not None:
        files["calibration_plan"] = _entry(plan_path)
    plan = {"schema_version": 2, "candidate_grid": grid,
            "ranking": "Final serialized integer train MAE, then train RMSE, then grid index, ascending",
            "selection_data": "Original training partition only; no validation or held-out labels read",
            "initialization": "Prior float checkpoint trained on the complete original training partition",
            "calibration_limitation": "In-sample training calibration, not independent validation",
            "model_format": "REGICIDE_NNUE 2 768 16 (127*S) (8*S) S", "files": files,
            "completion_requirement": "Selected input integers must differ from same-S initial-float quantization"}
    _write_json(output_dir / "grid_plan.json", plan)
    baseline_train = base.error_metrics(predict(baseline, indices), targets)
    references = {}
    for scale in sorted({config["input_scale"] for config in grid}):
        reference_dir = output_dir / f"reference-s{scale:03d}"
        reference_dir.mkdir()
        initial_reference, baseline_rescaled = quantize(initial, scale), rescale_model(baseline, scale)
        save_model(initial_reference, reference_dir / "initial_float_requantized.nnue")
        save_model(baseline_rescaled, reference_dir / "baseline_exact_rescaled.nnue")
        if not np.array_equal(predict(baseline_rescaled, indices), predict(baseline, indices)):
            raise ValueError("No-learning baseline rescaling changed predictions")
        reference_record = {"input_scale": scale,
                            "initial_float_requantized_train": base.error_metrics(predict(initial_reference, indices), targets),
                            "baseline_exact_rescaled_train": baseline_train,
                            "initial_reference_sha256": base.sha256(reference_dir / "initial_float_requantized.nnue"),
                            "rescaled_baseline_sha256": base.sha256(reference_dir / "baseline_exact_rescaled.nnue"),
                            "rescaled_baseline_exact_rows": len(rows)}
        _write_json(reference_dir / "metrics.json", reference_record)
        references[scale] = (initial_reference, baseline_rescaled, reference_record)
    candidates = []
    for index, config in enumerate(grid):
        candidate_dir = output_dir / config["candidate_id"]
        candidate_dir.mkdir()
        parameters, history = train_candidate(initial, indices, targets, config)
        model = quantize(parameters, config["input_scale"])
        save_model(model, candidate_dir / "model.nnue")
        loaded = load_model(candidate_dir / "model.nnue")
        predictions, _ = qat_forward(parameters, indices, config["input_scale"])
        exact = np.array([quantized_white(chess.Board(row["fen"]), loaded) for row in rows])
        if not np.array_equal(predictions, exact) or model_checksum(loaded) != model_checksum(model):
            raise ValueError("Fixed-point fake forward differs from serialized model")
        with (candidate_dir / "float_weights.npz").open("xb") as handle:
            np.savez(handle, **parameters)
        initial_reference, baseline_rescaled, _ = references[config["input_scale"]]
        record = {"grid_index": index, "config": config, "train": base.error_metrics(exact, targets),
                  "fake_forward_serialized_exact_rows": len(rows),
                  "changed_integer_parameters_vs_initial_reference": _changed(model, initial_reference),
                  "changed_integer_parameters_vs_baseline_rescaled": _changed(model, baseline_rescaled),
                  "changed_integer_parameters_vs_baseline_raw": _changed(model, baseline),
                  "changed_float_parameters": _changed(parameters, initial),
                  "model_sha256": base.sha256(candidate_dir / "model.nnue"),
                  "model_checksum_fnv1a64": model_checksum(model),
                  "float_weights_sha256": base.sha256(candidate_dir / "float_weights.npz"),
                  "training_history": history}
        _write_json(candidate_dir / "metrics.json", record)
        candidates.append(record)
    ranking = sorted(candidates, key=lambda item: (item["train"]["mae_cp"], item["train"]["rmse_cp"], item["grid_index"]))
    winner = ranking[0]
    selected_dir = output_dir / winner["config"]["candidate_id"]
    selected_model = load_model(selected_dir / "model.nnue")
    save_model(selected_model, output_dir / "model.nnue")
    with (output_dir / "float_weights.npz").open("xb") as handle:
        handle.write((selected_dir / "float_weights.npz").read_bytes())
    reference = references[winner["config"]["input_scale"]][0]
    input_reverted = dict(selected_model)
    input_reverted["input_weights"] = reference["input_weights"].copy()
    selected_predictions, reverted_predictions = predict(selected_model, indices), predict(input_reverted, indices)
    ranking_keys = ("grid_index", "config", "train", "model_sha256", "changed_integer_parameters_vs_initial_reference",
                    "changed_integer_parameters_vs_baseline_rescaled", "changed_integer_parameters_vs_baseline_raw")
    selection = {"schema_version": 2, "selected_candidate_id": winner["config"]["candidate_id"],
                 "selected_config": winner["config"], "selection_policy": plan["ranking"],
                 "selection_data": plan["selection_data"], "calibration_limitation": plan["calibration_limitation"],
                 "training_rows": len(rows), "training_game_count": len({row["group_id"] for row in rows}),
                 "baseline_train": baseline_train, "references": [value[2] for value in references.values()],
                 "selected_train": winner["train"],
                 "selected_changed_integer_parameters_vs_initial_reference": winner["changed_integer_parameters_vs_initial_reference"],
                 "selected_changed_integer_parameters_vs_baseline_rescaled": winner["changed_integer_parameters_vs_baseline_rescaled"],
                 "input_learning_ablation_train_only": {
                     "description": "Restore only input weights to same-S initial reference; retain final bias and outputs",
                     "changed_prediction_rows": int(np.count_nonzero(selected_predictions != reverted_predictions)),
                     "reverted_inputs_train": base.error_metrics(reverted_predictions, targets),
                     "selected_train": winner["train"]},
                 "ranking": [{key: item[key] for key in ranking_keys} for item in ranking],
                 "optimizer": "Adam beta1=0.9 beta2=0.999 epsilon=1e-8; float64 shadows/moments; all layers trained",
                 "loss": "Mean squared error in pawn units; exact integer deployed forward",
                 "gradient_global_norm_limit": 10.0,
                 "ste": "Rounding/truncation unit surrogate; strict clipped-ReLU and score-clamp interior; scale-correct real-unit gradients",
                 "quantization": "Nearest ties-to-even input/bias*S, output*8; int64; one signed truncation /8S; clamp +/-28000",
                 "python": platform.python_version(), "numpy": np.__version__, "python_chess": chess.__version__,
                 "source_dataset_engine_sha256": manifest["engine_sha256"], "no_playing_strength_claim": True}
    _write_json(output_dir / "selection.json", selection)
    if not winner["changed_integer_parameters_vs_initial_reference"]["input_weights"]:
        raise ValueError("Selected model has no learned integer input changes; do not create held-out data")
    files.update({"grid_plan": _entry(output_dir / "grid_plan.json"), "selection": _entry(output_dir / "selection.json"),
                  "selected_model": _entry(output_dir / "model.nnue"), "selected_floats": _entry(output_dir / "float_weights.npz")})
    freeze = {"schema_version": 2, "selected_candidate_id": selection["selected_candidate_id"],
              "selected_config": winner["config"], "model_checksum_fnv1a64": model_checksum(selected_model),
              "files": files, "selection_data": plan["selection_data"],
              "calibration_limitation": plan["calibration_limitation"],
              "policy": "Written last before fresh holdout generation; no model/config changes after holdout evaluation"}
    _write_json(output_dir / "freeze.json", freeze)
    verify_freeze(output_dir / "freeze.json")
    return selection


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    select = commands.add_parser("select")
    for option in ("data", "baseline-model", "initial-floats", "output"):
        select.add_argument(f"--{option}", type=Path, required=True)
    select.add_argument("--plan", type=Path)
    check = commands.add_parser("check-freeze")
    check.add_argument("freeze", type=Path)
    model = commands.add_parser("check-model")
    model.add_argument("model", type=Path)
    args = parser.parse_args()
    if args.command == "select":
        result = select_model(args.data, args.baseline_model, args.initial_floats, args.output, args.plan)
    elif args.command == "check-freeze":
        result = verify_freeze(args.freeze)
    else:
        result = {"model_checksum_fnv1a64": model_checksum(load_model(args.model)), "sha256": base.sha256(args.model)}
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)
