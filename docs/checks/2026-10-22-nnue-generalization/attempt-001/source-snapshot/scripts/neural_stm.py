"""MSE QAT for one relative side-to-move input, with legacy model parity.

The 768 piece inputs and their packing stay unchanged.  A separate binary
input is one only in the perspective that currently has the move.  Its sixteen
weights start at zero; format v3 appends them after the existing piece rows.
Integer inference, STE, Adam and final-epoch training follow neural_precision.
"""
from __future__ import annotations

import math
from pathlib import Path

import chess
import numpy as np

import neural_training as base
import neural_qat as qat
import neural_precision as precision

FEATURES, HIDDEN, CLIP, OUTPUT_SCALE = base.FEATURES, base.HIDDEN, base.CLIP, base.OUTPUT_SCALE
PARAMETER_KEYS = precision.PARAMETER_KEYS + ("turn_weights",)


def with_turn_parameters(initial: dict) -> dict:
    """Copy the original initializer exactly, adding only zero turn weights."""
    precision._validate_parameters(initial)
    return {**{key: value.copy() for key, value in initial.items()},
            "turn_weights": np.zeros(HIDDEN, dtype=np.float64)}


def _validate_parameters(parameters: dict) -> None:
    if set(parameters) != set(PARAMETER_KEYS):
        raise ValueError("Expected piece, bias, output and turn float parameters")
    precision._validate_parameters({key: parameters[key] for key in precision.PARAMETER_KEYS})
    values = np.asarray(parameters["turn_weights"])
    if (values.shape != (HIDDEN,) or not np.issubdtype(values.dtype, np.floating)
            or not np.all(np.isfinite(values))):
        raise ValueError("Invalid float turn weights")


def _turn_mask(turns, count: int, enable_turn: bool = True) -> np.ndarray:
    values = np.asarray(turns)
    if (values.shape != (count,) or not np.issubdtype(values.dtype, np.integer)
            or np.any((values != 0) & (values != 1))):
        raise ValueError("Turns must contain one integer 0=white or 1=black per row")
    return ((np.arange(2)[None, :] == values[:, None]) & enable_turn)[:, :, None]


def pack_records(rows: list[dict]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    indices, targets = base.pack_records(rows)
    turns = np.array([0 if chess.Board(row["fen"]).turn else 1 for row in rows], dtype=np.int64)
    return indices, targets, turns


def _legacy_view(model: dict) -> dict:
    result = {key: model[key] for key in precision.PARAMETER_KEYS}
    version = model.get("format_version", 1)
    result.update(format_version=2 if version == 3 else version,
                  input_scale=model.get("input_scale", 1), clip=model.get("clip", CLIP),
                  output_scale=model["output_scale"])
    return result


def validate_model(model: dict) -> None:
    version = model.get("format_version", 1)
    if isinstance(version, (bool, np.bool_)) or not isinstance(version, (int, np.integer)) or version not in (1, 2, 3):
        raise ValueError("Unsupported model version")
    precision.validate_model(_legacy_view(model))
    turn = np.asarray(model["turn_weights"])
    bound = precision.INTEGER_BOUNDS["input_weights"] if version == 3 else 0
    if (turn.shape != (HIDDEN,) or not np.issubdtype(turn.dtype, np.integer)
            or np.any(turn < -bound) or np.any(turn > bound)):
        raise ValueError("Invalid or out-of-range turn weights")


def model_checksum(model: dict) -> int:
    validate_model(model)
    if model.get("format_version", 1) != 3:
        return precision.model_checksum(_legacy_view(model))
    values = [3, FEATURES + 1, HIDDEN, model["clip"], model["output_scale"], model["input_scale"]]
    for key in ("hidden_bias", "output_weights", "input_weights", "turn_weights"):
        values.extend(int(value) for value in model[key].flat)
    checksum = 14695981039346656037
    for value in values:
        for byte in (int(value) & 0xFFFFFFFF).to_bytes(4, "little"):
            checksum = ((checksum ^ byte) * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return checksum


def save_model(model: dict, path: Path | str) -> None:
    validate_model(model)
    if model.get("format_version", 1) != 3:
        precision.save_model(_legacy_view(model), path)
        return
    header = f"REGICIDE_NNUE 3 {FEATURES + 1} {HIDDEN} {model['clip']} {model['output_scale']} {model['input_scale']}"
    lines = [header]
    for key in ("hidden_bias", "output_weights"):
        lines.append(" ".join(str(int(value)) for value in model[key]))
    lines.extend(" ".join(str(int(value)) for value in row) for row in model["input_weights"])
    lines.append(" ".join(str(int(value)) for value in model["turn_weights"]))
    lines.append(f"checksum {model_checksum(model)}")
    text = "\n".join(lines) + "\n"
    if len(text.encode("ascii")) > base.MODEL_LIMIT:
        raise ValueError("Model exceeds maximum file size")
    with Path(path).open("x", encoding="ascii", newline="\n") as handle:
        handle.write(text)


def load_model(path: Path | str) -> dict:
    path = Path(path)
    if path.stat().st_size > base.MODEL_LIMIT:
        raise ValueError("Model exceeds maximum file size")
    tokens = path.read_text(encoding="ascii").split()
    if len(tokens) < 3 or tokens[0] != "REGICIDE_NNUE":
        raise ValueError("Invalid model header")
    if tokens[1] in ("1", "2"):
        return {**precision.load_model(path), "turn_weights": np.zeros(HIDDEN, dtype=np.int64)}
    expected = 7 + HIDDEN * 3 + FEATURES * HIDDEN + 2
    if len(tokens) != expected or tokens[1] != "3" or tokens[-2] != "checksum":
        raise ValueError("Unsupported model version or token count")
    integers = tokens[1:-2] + tokens[-1:]
    if any(len(token) > 20 or not base.INTEGER.fullmatch(token) for token in integers):
        raise ValueError("Invalid model integer")
    values = [int(token) for token in integers]
    if values[:3] != [3, FEATURES + 1, HIDDEN] or not 0 <= values[-1] <= 0xFFFFFFFFFFFFFFFF:
        raise ValueError("Invalid model architecture or checksum range")
    model = {"format_version": 3, "input_scale": values[5], "clip": values[3], "output_scale": values[4]}
    offset = 6
    for key, shape in (("hidden_bias", (HIDDEN,)), ("output_weights", (HIDDEN,)),
                       ("input_weights", (FEATURES, HIDDEN)), ("turn_weights", (HIDDEN,))):
        count = math.prod(shape)
        part = values[offset:offset + count]
        bound = precision.INTEGER_BOUNDS["input_weights" if key == "turn_weights" else key]
        if any(value < -bound or value > bound for value in part):
            raise ValueError(f"Out-of-range {key}")
        model[key] = np.array(part, dtype=np.int64).reshape(shape)
        offset += count
    validate_model(model)
    if model_checksum(model) != values[-1]:
        raise ValueError("Model checksum mismatch")
    return model


load_integer_model = load_model


def _bounds(input_scale: int) -> dict:
    bounds = precision._bounds(input_scale)
    return {**bounds, "turn_weights": bounds["input_weights"]}


def quantize(parameters: dict, input_scale: int = 32) -> dict:
    _validate_parameters(parameters)
    model = precision.quantize({key: parameters[key] for key in precision.PARAMETER_KEYS}, input_scale)
    bound = _bounds(input_scale)["turn_weights"]
    model.update(format_version=3, turn_weights=np.rint(
        np.clip(parameters["turn_weights"], -bound, bound) * input_scale).astype(np.int64))
    validate_model(model)
    return model


def save_integer_model(path: Path | str, parameters: dict, input_scale: int = 32) -> None:
    save_model(quantize(parameters, input_scale), path)


def _integer_forward(model: dict, indices: np.ndarray, turns, *, enable_turn: bool = True) -> tuple[np.ndarray, dict]:
    validate_model(model)
    qat._validate_batch(indices)
    mask = _turn_mask(turns, len(indices), enable_turn)
    weights = np.vstack((model["input_weights"], np.zeros((1, HIDDEN), dtype=np.int64)))
    sums = weights[indices].sum(axis=2) + model["hidden_bias"]
    sums += mask * model["turn_weights"]
    activations = np.clip(sums, 0, model["clip"])
    differences = activations[:, 0] - activations[:, 1]
    numerators = differences @ model["output_weights"]
    scores = (np.abs(numerators) // model["output_scale"]) * np.where(numerators >= 0, 1, -1)
    return np.clip(scores, -28000, 28000).astype(np.float64), {
        "quantized_model": model, "sums": sums, "differences": differences,
        "raw_scores": numerators.astype(np.float64) / model["output_scale"], "turn_mask": mask}


def predict(model: dict, indices: np.ndarray, turns) -> np.ndarray:
    return _integer_forward(model, indices, turns)[0]


def predict_integer(parameters: dict, indices: np.ndarray, input_scale: int = 32, *, turns) -> np.ndarray:
    return predict(quantize(parameters, input_scale), indices, turns)


def quantized_white(board: chess.Board, model: dict) -> int:
    indices, _, turns = pack_records([{"fen": board.fen(), "target_white_cp": 0}])
    return int(predict(model, indices, turns)[0])


def qat_forward(parameters: dict, indices: np.ndarray, input_scale: int = 32, *, turns,
                enable_turn: bool = True) -> tuple[np.ndarray, dict]:
    return _integer_forward(quantize(parameters, input_scale), indices, turns, enable_turn=enable_turn)


def _float_predictions(parameters: dict, indices: np.ndarray, turns, enable_turn: bool) -> np.ndarray:
    weights = np.vstack((parameters["input_weights"], np.zeros((1, HIDDEN))))
    sums = weights[indices].sum(axis=2) + parameters["hidden_bias"]
    sums += _turn_mask(turns, len(indices), enable_turn) * parameters["turn_weights"]
    activations = np.clip(sums, 0, CLIP)
    return (activations[:, 0] - activations[:, 1]) @ parameters["output_weights"]


def qat_gradients(parameters: dict, indices: np.ndarray, targets: np.ndarray,
                  input_scale: int = 32, *, turns, enable_turn: bool = True) -> dict:
    """Same MSE STE as the 768-input trainer, plus the active turn row."""
    qat._validate_batch(indices, targets)
    predictions, cache = qat_forward(parameters, indices, input_scale, turns=turns, enable_turn=enable_turn)
    derivative = precision.loss_derivative(predictions, targets, "mse")
    derivative *= (cache["raw_scores"] > -28000) & (cache["raw_scores"] < 28000)
    outputs = cache["quantized_model"]["output_weights"] / OUTPUT_SCALE
    hidden_gradient = derivative[:, None, None] * outputs[None, None, :]
    hidden_gradient = np.broadcast_to(hidden_gradient, cache["sums"].shape).copy()
    hidden_gradient[:, 1] *= -1
    hidden_gradient *= (cache["sums"] > 0) & (cache["sums"] < CLIP * input_scale)
    repeated = np.repeat(hidden_gradient.reshape(-1, HIDDEN), indices.shape[2], axis=0)
    inputs = np.column_stack([
        np.bincount(indices.ravel(), weights=repeated[:, h], minlength=FEATURES + 1)
        for h in range(HIDDEN)])
    gradients = {"input_weights": inputs[:-1], "hidden_bias": hidden_gradient.sum(axis=(0, 1)),
                 "output_weights": (cache["differences"] / input_scale).T @ derivative,
                 "turn_weights": (hidden_gradient * cache["turn_mask"]).sum(axis=(0, 1))}
    for key, bound in _bounds(input_scale).items():
        gradients[key] *= (parameters[key] >= -bound) & (parameters[key] <= bound)
    return gradients


def train_candidate(initial_parameters: dict, indices: np.ndarray, targets: np.ndarray,
                    config: dict, *, turns, progress=None, enable_turn: bool = True) -> tuple[dict, list[dict]]:
    """Train exactly the requested final epoch; validation never enters Adam."""
    _validate_parameters(initial_parameters)
    qat._validate_batch(indices, targets)
    _turn_mask(turns, len(indices), enable_turn)
    turns = np.asarray(turns)
    precision._validate_config(config)
    if config.get("loss", "mse") != "mse":
        raise ValueError("The side-to-move experiment keeps MSE fixed")
    if progress is not None and not callable(progress):
        raise ValueError("progress must be callable or None")
    if type(enable_turn) is not bool:
        raise ValueError("enable_turn must be bool")
    bounds = _bounds(config["input_scale"])
    parameters = {key: value.astype(np.float64, copy=True) for key, value in initial_parameters.items()}
    if any(np.any(np.abs(values) > bounds[key]) for key, values in parameters.items()):
        raise ValueError("Initial shadow parameter outside model bounds")
    first = {key: np.zeros_like(values) for key, values in parameters.items()}
    second = {key: np.zeros_like(values) for key, values in parameters.items()}
    rates = {key: config["output_lr"] if key == "output_weights" else config["input_bias_lr"] for key in PARAMETER_KEYS}
    rng, history, step = np.random.default_rng(config["seed"]), [], 0
    progress_window = []
    for epoch in range(1, config["epochs"] + 1):
        order = rng.permutation(len(targets))
        maximum_norm, clipped_batches = 0.0, 0
        batch_norms = []
        for offset in range(0, len(order), config["batch_size"]):
            chosen = order[offset:offset + config["batch_size"]]
            gradients = qat_gradients(parameters, indices[chosen], targets[chosen], config["input_scale"],
                                      turns=turns[chosen], enable_turn=enable_turn)
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
        predictions, _ = qat_forward(parameters, indices, config["input_scale"], turns=turns, enable_turn=enable_turn)
        history.append({"epoch": epoch, "optimizer_steps": step,
                        "fake_quantized_train": base.error_metrics(predictions, targets),
                        "shadow_float_train": base.error_metrics(_float_predictions(parameters, indices, turns, enable_turn), targets),
                        "max_batch_gradient_global_norm": maximum_norm,
                        "gradient_clipped_batches": clipped_batches})
        if progress is not None:
            progress_window.append({"epoch": epoch, "batch_gradient_global_norms": batch_norms})
            if epoch % 10 == 0:
                progress(epoch, {key: value.copy() for key, value in parameters.items()},
                         dict(history[-1], batch_gradient_observations=progress_window))
                progress_window = []
    return parameters, history
