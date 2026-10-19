"""First reproducible Regicide evaluator-imitation experiment, not strength training.

Own random legal games supply positions; Regicide's existing handcrafted static
evaluator supplies targets. No external games, engines, or pretrained weights.
Validation games are assigned before positions are generated. Color/rank-mirror
equivalent board inputs are globally deduplicated, including across partitions.
Validation never participates in gradients, early stopping, or model selection.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import platform
import random
import re
import subprocess
import sys

import chess
import numpy as np

FEATURES = 768
HIDDEN = 16
CLIP = 127
OUTPUT_SCALE = 8
MODEL_LIMIT = 256 * 1024
PIECES = "PNBRQKpnbrqk"
INTEGER = re.compile(r"-?(?:0|[1-9][0-9]*)\Z")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def fresh_directory(path: Path) -> None:
    if path.exists():
        raise ValueError(f"Refusing to overwrite existing output: {path}")
    path.mkdir(parents=True)


def feature_identity(board: chess.Board) -> str:
    """Hash exactly the network input, also rejecting reversed-perspective copies.

    Turn, castling rights, ep, and clocks are excluded: this network does not read
    them. mirror() swaps colors and flips ranks, exchanging its two input views.
    Its white target changes sign but the board input contains no new information.
    """
    identity = min(board.board_fen(), board.mirror().board_fen())
    return hashlib.sha256(identity.encode("ascii")).hexdigest()


def feature_indices(board: chess.Board) -> list[list[int]]:
    views: list[list[int]] = [[], []]
    for square, piece in sorted(board.piece_map().items()):
        kind = PIECES.index(piece.symbol())
        views[0].append(kind * 64 + square)
        views[1].append(((kind + 6) % 12) * 64 + (square ^ 56))
    return views


def model_checksum(model: dict) -> int:
    values = [1, FEATURES, HIDDEN, CLIP, int(model["output_scale"])]
    for key in ("hidden_bias", "output_weights", "input_weights"):
        values.extend(int(v) for v in np.asarray(model[key]).flat)
    result = 14695981039346656037
    for value in values:
        for byte in (value & 0xFFFFFFFF).to_bytes(4, "little"):
            result = ((result ^ byte) * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return result


def validate_model(model: dict) -> None:
    scale = model["output_scale"]
    if isinstance(scale, (bool, np.bool_)) or not isinstance(scale, (int, np.integer)) or not 1 <= scale <= 65536:
        raise ValueError("Invalid output scale")
    for key, shape, bound in (("hidden_bias", (HIDDEN,), 4096),
                              ("output_weights", (HIDDEN,), 8192),
                              ("input_weights", (FEATURES, HIDDEN), 127)):
        values = np.asarray(model[key])
        if values.shape != shape or not np.issubdtype(values.dtype, np.integer):
            raise ValueError(f"Invalid shape or type for {key}")
        if np.any(values < -bound) or np.any(values > bound):
            raise ValueError(f"Out-of-range {key}")


def save_model(model: dict, path: Path | str) -> None:
    path = Path(path)
    validate_model(model)
    if path.exists():
        raise ValueError(f"Refusing to overwrite model: {path}")
    lines = [f"REGICIDE_NNUE 1 {FEATURES} {HIDDEN} {CLIP} {model['output_scale']}"]
    for key in ("hidden_bias", "output_weights"):
        lines.append(" ".join(str(int(v)) for v in model[key]))
    lines.extend(" ".join(str(int(v)) for v in row) for row in model["input_weights"])
    lines.append(f"checksum {model_checksum(model)}")
    text = "\n".join(lines) + "\n"
    if len(text.encode("ascii")) > MODEL_LIMIT:
        raise ValueError("Model exceeds maximum file size")
    # Exclusive creation also protects against accidental parallel overwrites.
    with path.open("x", encoding="ascii", newline="\n") as handle:
        handle.write(text)


def load_model(path: Path | str) -> dict:
    path = Path(path)
    if path.stat().st_size > MODEL_LIMIT:
        raise ValueError("Model exceeds maximum file size")
    tokens = path.read_text(encoding="ascii").split()
    expected = 6 + HIDDEN + HIDDEN + FEATURES * HIDDEN + 2
    if len(tokens) != expected or tokens[0] != "REGICIDE_NNUE" or tokens[-2] != "checksum":
        raise ValueError("Invalid model header or token count")
    integers = tokens[1:-2] + tokens[-1:]
    if any(not INTEGER.fullmatch(token) or len(token) > 20 for token in integers):
        raise ValueError("Invalid model integer")
    values = [int(token) for token in integers]
    if values[:4] != [1, FEATURES, HIDDEN, CLIP]:
        raise ValueError("Unsupported model version or architecture")
    if not 0 <= values[-1] <= 0xFFFFFFFFFFFFFFFF:
        raise ValueError("Invalid checksum range")
    offset = 5
    model = {"output_scale": values[4]}
    for key, size in (("hidden_bias", HIDDEN), ("output_weights", HIDDEN),
                      ("input_weights", FEATURES * HIDDEN)):
        # Validate Python integers before converting, preventing int64 overflow.
        bound = {"hidden_bias": 4096, "output_weights": 8192, "input_weights": 127}[key]
        part = values[offset:offset + size]
        if any(v < -bound or v > bound for v in part):
            raise ValueError(f"Out-of-range {key}")
        model[key] = np.array(part, dtype=np.int64)
        offset += size
    model["input_weights"] = model["input_weights"].reshape(FEATURES, HIDDEN)
    validate_model(model)
    if model_checksum(model) != values[-1]:
        raise ValueError("Model checksum mismatch")
    return model


def quantized_white(board: chess.Board, model: dict) -> int:
    views = feature_indices(board)
    activations = [np.clip(np.asarray(model["hidden_bias"], dtype=np.int64)
                           + model["input_weights"][view].sum(axis=0), 0, CLIP)
                   for view in views]
    numerator = int((activations[0] - activations[1]) @ model["output_weights"])
    # C++ integer division truncates toward zero, including negative values.
    score = (abs(numerator) // int(model["output_scale"])) * (1 if numerator >= 0 else -1)
    return max(-28000, min(28000, score))


def evaluate_engine(engine: Path | str, fens: list[str], model_path: Path | str | None = None) -> list[tuple[int, int, int]]:
    command = [str(Path(engine).resolve()), "eval-batch"]
    if model_path is not None:
        command.extend(["--neural-model", str(Path(model_path).resolve())])
    result = subprocess.run(command, input="\n".join(fens) + "\n", capture_output=True,
                            text=True, timeout=120, check=True)
    rows = [tuple(int(value) for value in line.split("\t")) for line in result.stdout.splitlines()]
    if len(rows) != len(fens) or any(len(row) != 3 for row in rows):
        raise ValueError("eval-batch must return one three-integer TSV row per input FEN")
    if any(row[1] != row[2] for row in rows):
        raise ValueError("Engine full/incremental parity failed")
    return rows


def generate_dataset(engine: Path | str, output: Path | str, *, games: int = 64,
                     max_plies: int = 64, sample_every: int = 2, seed: int = 20261019,
                     validation_fraction: float = 0.2) -> dict:
    if games < 4 or not 8 <= max_plies <= 512 or sample_every < 1:
        raise ValueError("Require >=4 games, 8..512 max plies, and positive sample interval")
    if not 0 < validation_fraction < 1:
        raise ValueError("Validation fraction must lie strictly between zero and one")
    engine, output = Path(engine).resolve(), Path(output)
    fresh_directory(output)
    rng = random.Random(seed)
    ids = [f"game-{i:06d}" for i in range(games)]
    shuffled = ids.copy()
    rng.shuffle(shuffled)
    val_count = max(1, min(games - 1, round(games * validation_fraction)))
    assignments = {group: ("validation" if group in set(shuffled[:val_count]) else "train") for group in ids}
    partitions: dict[str, list[dict]] = {"train": [], "validation": []}
    raw_games = []
    identities: set[str] = set()
    duplicates = candidates = 0
    for game_index, group in enumerate(ids):
        # Independent source-game RNG, unaffected by how other games terminate.
        game_rng = random.Random(seed + 1000003 * (game_index + 1))
        board = chess.Board()
        moves = []
        for ply in range(1, max_plies + 1):
            legal = sorted(board.legal_moves, key=lambda move: move.uci())
            if not legal:
                break
            move = game_rng.choice(legal)
            moves.append(move.uci())
            board.push(move)
            if board.outcome(claim_draw=True) is not None:
                break  # Terminal/draw-claim targets belong to search, not static eval.
            if ply < 6 or (ply - 6) % sample_every:
                continue
            candidates += 1
            identity = feature_identity(board)
            if identity in identities:
                duplicates += 1
                continue
            identities.add(identity)
            partitions[assignments[group]].append({"fen": board.fen(en_passant="fen"),
                "group_id": group, "ply": ply, "feature_hash": identity})
        raw_games.append({"group_id": group, "partition": assignments[group], "moves_uci": moves})
    if any(not rows for rows in partitions.values()):
        raise ValueError("A partition is empty; generate more games")
    records = partitions["train"] + partitions["validation"]
    labels = evaluate_engine(engine, [record["fen"] for record in records])
    for record, (classical, _, _) in zip(records, labels, strict=True):
        board = chess.Board(record["fen"])
        record["classical_stm_cp"] = classical
        record["target_white_cp"] = classical if board.turn == chess.WHITE else -classical
    partition_metadata = {}
    for name, rows in partitions.items():
        path = output / f"{name}.jsonl"
        path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
        partition_metadata[name] = {"file": path.name, "count": len(rows), "sha256": sha256(path)}
    raw_path = output / "raw_games.jsonl"
    raw_path.write_text("".join(json.dumps(game, sort_keys=True) + "\n" for game in raw_games), encoding="utf-8")
    manifest = {"schema_version": 1, "seed": seed, "games": games, "max_plies": max_plies,
        "sample_every": sample_every, "sample_start_ply": 6, "validation_fraction": validation_fraction,
        "groups": assignments, "partitions": partition_metadata, "candidates": candidates,
        "duplicates_removed": duplicates, "feature_overlap": 0, "engine_sha256": sha256(engine),
        "generator_sha256": sha256(Path(__file__)), "raw_games_sha256": sha256(raw_path),
        "label_source": "Regicide own classical static evaluator; side-to-move centipawns converted to white",
        "source": "Own deterministic random legal orthodox games, no external game/model data",
        "identity": "SHA256(min(board_fen, color-swapped rank-mirrored board_fen)); ignores turn/rights/ep/clocks",
        "terminal_policy": "Stop and omit a position when python-chess outcome(claim_draw=True) is not None",
        "split_policy": "Assign whole games before generation; first globally unique mirrored board input wins",
        "python": platform.python_version(), "python_chess": chess.__version__}
    write_json(output / "split_manifest.json", manifest)
    return manifest


def read_dataset(path: Path | str) -> tuple[dict, dict[str, list[dict]]]:
    path = Path(path)
    manifest = json.loads((path / "split_manifest.json").read_text(encoding="utf-8"))
    partitions = {}
    feature_sets = {}
    group_sets = {}
    for name in ("train", "validation"):
        metadata = manifest["partitions"][name]
        if metadata["file"] != f"{name}.jsonl":
            raise ValueError("Unexpected dataset filename")
        file = path / metadata["file"]
        if sha256(file) != metadata["sha256"]:
            raise ValueError("Dataset checksum mismatch")
        rows = [json.loads(line) for line in file.read_text(encoding="utf-8").splitlines()]
        if not rows or len(rows) != metadata["count"]:
            raise ValueError("Empty or inconsistent dataset partition")
        hashes = set()
        groups = set()
        for row in rows:
            board = chess.Board(row["fen"])
            identity = feature_identity(board)
            if not board.is_valid() or board.outcome(claim_draw=True) is not None:
                raise ValueError("Dataset includes invalid or terminal position")
            if identity != row["feature_hash"] or identity in hashes:
                raise ValueError("Invalid or duplicate feature identity")
            if manifest["groups"].get(row["group_id"]) != name:
                raise ValueError("Game crosses assigned data partitions")
            target = row["target_white_cp"]
            if type(target) is not int or type(row["classical_stm_cp"]) is not int:
                raise ValueError("Targets must be integer centipawns")
            if target != row["classical_stm_cp"] * (1 if board.turn else -1):
                raise ValueError("Inconsistent target score perspective")
            hashes.add(identity)
            groups.add(row["group_id"])
        feature_sets[name], group_sets[name], partitions[name] = hashes, groups, rows
    if feature_sets["train"] & feature_sets["validation"] or group_sets["train"] & group_sets["validation"]:
        raise ValueError("Training/validation leakage detected")
    return manifest, partitions


def bootstrap_float() -> dict:
    weights = np.zeros((FEATURES, HIDDEN), dtype=np.float64)
    state = 0x52454731
    for feature in range(FEATURES):
        kind = feature // 64
        if kind < 6:
            weights[feature, kind] = 4
        for h in range(6, HIDDEN):
            state = (state * 1664525 + 1013904223) & 0xFFFFFFFF
            weights[feature, h] = ((state >> 16) % 7) - 3
    return {"input_weights": weights, "hidden_bias": np.zeros(HIDDEN, dtype=np.float64),
            "output_weights": np.array([25, 80, 82.5, 125, 225, 0, 1, -1, 1, -1, 1, -1, 1, -1, 1, -1], dtype=np.float64)}


def quantize(parameters: dict) -> dict:
    model = {"output_scale": OUTPUT_SCALE,
        "hidden_bias": np.rint(np.clip(parameters["hidden_bias"], -4096, 4096)).astype(np.int64),
        "input_weights": np.rint(np.clip(parameters["input_weights"], -127, 127)).astype(np.int64),
        "output_weights": np.rint(np.clip(parameters["output_weights"] * OUTPUT_SCALE, -8192, 8192)).astype(np.int64)}
    validate_model(model)
    return model


def pack_records(rows: list[dict]) -> tuple[np.ndarray, np.ndarray]:
    # Sentinel index FEATURES is a constant zero row, not a learned feature.
    indices = np.full((len(rows), 2, 32), FEATURES, dtype=np.int64)
    for n, row in enumerate(rows):
        for view, values in enumerate(feature_indices(chess.Board(row["fen"]))):
            indices[n, view, :len(values)] = values
    return indices, np.array([row["target_white_cp"] for row in rows], dtype=np.float64)


def forward(parameters: dict, indices: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    weights = np.vstack((parameters["input_weights"], np.zeros((1, HIDDEN))))
    sums = weights[indices].sum(axis=2) + parameters["hidden_bias"]
    activated = np.clip(sums, 0, CLIP)
    differences = activated[:, 0] - activated[:, 1]
    return differences @ parameters["output_weights"], sums, differences


def error_metrics(predictions: np.ndarray, targets: np.ndarray) -> dict:
    residual = predictions - targets
    mse = float(np.mean(residual ** 2))
    return {"mse_cp2": mse, "rmse_cp": math.sqrt(mse), "mae_cp": float(np.mean(np.abs(residual)))}


def measure(parameters: dict, packed: dict, records: dict) -> dict:
    model = quantize(parameters)
    result = {}
    for name, (indices, targets) in packed.items():
        result[f"float_{name}"] = error_metrics(forward(parameters, indices)[0], targets)
        predictions = np.array([quantized_white(chess.Board(row["fen"]), model) for row in records[name]])
        result[f"quantized_{name}"] = error_metrics(predictions, targets)
    return result


def train_model(data_dir: Path | str, output_dir: Path | str, *, epochs: int = 60,
                seed: int = 20261020, batch_size: int = 128) -> dict:
    if not 1 <= epochs <= 10000 or not 1 <= batch_size <= 65536:
        raise ValueError("Invalid epoch or batch-size limit")
    data_dir, output_dir = Path(data_dir), Path(output_dir)
    manifest, records = read_dataset(data_dir)
    fresh_directory(output_dir)
    packed = {name: pack_records(rows) for name, rows in records.items()}
    parameters = bootstrap_float()
    initial_parameters = {key: values.copy() for key, values in parameters.items()}
    initial = measure(parameters, packed, records)
    first = {key: np.zeros_like(values) for key, values in parameters.items()}
    second = {key: np.zeros_like(values) for key, values in parameters.items()}
    learning_rates = {"input_weights": 0.002, "hidden_bias": 0.002, "output_weights": 0.05}
    bounds = {"input_weights": 127, "hidden_bias": 4096, "output_weights": 8192 / OUTPUT_SCALE}
    rng = np.random.default_rng(seed)
    train_x, train_y = packed["train"]
    history = []
    step = 0
    for epoch in range(1, epochs + 1):
        order = rng.permutation(len(train_y))
        for offset in range(0, len(order), batch_size):
            chosen = order[offset:offset + batch_size]
            indices, targets = train_x[chosen], train_y[chosen]
            predictions, sums, differences = forward(parameters, indices)
            # MSE on pawn units; the network and saved metrics remain centipawns.
            derivative = 2 * (predictions - targets) / (len(chosen) * 10000)
            hidden_gradient = derivative[:, None, None] * parameters["output_weights"][None, None, :]
            hidden_gradient = np.broadcast_to(hidden_gradient, sums.shape).copy()
            hidden_gradient[:, 1] *= -1
            hidden_gradient *= (sums > 0) & (sums < CLIP)
            input_gradient = np.zeros((FEATURES + 1, HIDDEN), dtype=np.float64)
            repeated = np.broadcast_to(hidden_gradient[:, :, None, :], (*indices.shape, HIDDEN))
            np.add.at(input_gradient, indices.ravel(), repeated.reshape(-1, HIDDEN))
            gradients = {"input_weights": input_gradient[:-1], "hidden_bias": hidden_gradient.sum(axis=(0, 1)),
                         "output_weights": differences.T @ derivative}
            norm = math.sqrt(sum(float(np.sum(value * value)) for value in gradients.values()))
            factor = min(1.0, 10.0 / max(norm, 1e-30))
            step += 1
            for key, parameter in parameters.items():
                gradient = gradients[key] * factor
                first[key] *= 0.9
                first[key] += 0.1 * gradient
                second[key] *= 0.999
                second[key] += 0.001 * gradient * gradient
                estimate = (first[key] / (1 - 0.9 ** step)) / (np.sqrt(second[key] / (1 - 0.999 ** step)) + 1e-8)
                parameter -= learning_rates[key] * estimate
                np.clip(parameter, -bounds[key], bounds[key], out=parameter)
                if not np.all(np.isfinite(parameter)):
                    raise ValueError("Nonfinite training parameter")
        # Record only training metrics during optimization. Validation is read
        # once before and once after the fixed, caller-chosen epoch budget.
        history.append({"epoch": epoch, **error_metrics(forward(parameters, train_x)[0], train_y)})
    final = measure(parameters, packed, records)
    model = quantize(parameters)
    model_path = output_dir / "model.nnue"
    save_model(model, model_path)
    loaded = load_model(model_path)
    if model_checksum(loaded) != model_checksum(model):
        raise ValueError("Export/import mismatch")
    np.savez(output_dir / "float_weights.npz", **parameters)
    # No pickle, optimizer continuation, or validation-dependent checkpoint.
    metrics = {"schema_version": 1, "epochs": epochs, "selected_epoch": epochs,
        "selection_policy": "Fixed final epoch chosen before training; validation never selects model",
        "seed": seed, "batch_size": batch_size, "optimizer": "Adam beta1=0.9 beta2=0.999 epsilon=1e-8",
        "loss": "mean squared error in pawn units (cp / 100)", "gradient_global_norm_limit": 10,
        "learning_rates": learning_rates, "initialization": "Own bootstrap-v1; material inputs x4, outputs /4",
        "all_layers_trained": True, "output_scale": OUTPUT_SCALE,
        "training_rows": len(records["train"]), "validation_rows": len(records["validation"]),
        "initial": initial, "final": final, "training_history": history,
        "changed_float_parameters": {key: int(np.count_nonzero(value != initial_parameters[key])) for key, value in parameters.items()},
        "changed_integer_parameters": {key: int(np.count_nonzero(model[key] != quantize(initial_parameters)[key])) for key in parameters},
        "dataset_manifest_sha256": sha256(data_dir / "split_manifest.json"),
        "dataset_files_sha256": {name: metadata["sha256"] for name, metadata in manifest["partitions"].items()},
        "engine_sha256": manifest["engine_sha256"], "trainer_sha256": sha256(Path(__file__)),
        "model_sha256": sha256(model_path), "model_checksum_fnv1a64": model_checksum(model),
        "float_weights_sha256": sha256(output_dir / "float_weights.npz"),
        "python": platform.python_version(), "numpy": np.__version__, "python_chess": chess.__version__,
        "quantization": "round-to-nearest ties-to-even; input/bias integer, output multiplied by scale8",
        "scope": "Imitates own classical static evaluation on random legal games; no measured playing strength"}
    write_json(output_dir / "metrics.json", metrics)
    return metrics


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("generate", "run"):
        command = sub.add_parser(name)
        command.add_argument("--engine", type=Path, required=True)
        command.add_argument("--output", type=Path, required=True)
        command.add_argument("--games", type=int, default=64)
        command.add_argument("--max-plies", type=int, default=64)
        command.add_argument("--sample-every", type=int, default=2)
        command.add_argument("--seed", type=int, default=20261019)
        command.add_argument("--validation-fraction", type=float, default=0.2)
    command = sub.add_parser("train")
    command.add_argument("--data", type=Path, required=True)
    command.add_argument("--output", type=Path, required=True)
    command.add_argument("--seed", type=int, default=20261020)
    for command in (sub.choices["train"], sub.choices["run"]):
        command.add_argument("--epochs", type=int, default=60)
        command.add_argument("--batch-size", type=int, default=128)
    sub.choices["run"].add_argument("--training-seed", type=int, default=20261020)
    command = sub.add_parser("check-model")
    command.add_argument("model", type=Path)
    args = parser.parse_args()
    if args.command == "check-model":
        model = load_model(args.model)
        print(json.dumps({"checksum_fnv1a64": model_checksum(model), "sha256": sha256(args.model)}))
        return
    if args.command == "train":
        result = train_model(args.data, args.output, epochs=args.epochs, seed=args.seed, batch_size=args.batch_size)
    else:
        if args.command == "run":
            fresh_directory(args.output)
        data = args.output / "data" if args.command == "run" else args.output
        result = generate_dataset(args.engine, data, games=args.games, max_plies=args.max_plies,
            sample_every=args.sample_every, seed=args.seed, validation_fraction=args.validation_fraction)
        if args.command == "run":
            result = train_model(data, args.output / "training", epochs=args.epochs,
                                 seed=args.training_seed, batch_size=args.batch_size)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)
