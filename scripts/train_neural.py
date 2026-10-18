"""Train the small residual evaluator from complete Regicide self-play games.

Family splits are fixed before collection. Test labels are only evaluated after
validation has selected an epoch; neither the test loss nor playing strength is
an optimization signal. All arithmetic is NumPy float64 on one CPU thread.
"""

import os

# Set before importing NumPy, including when this module is launched as a script.
for _name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS",
              "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS", "BLIS_NUM_THREADS"):
    os.environ[_name] = "1"

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import platform
import sys

import chess
import numpy as np

from neural_model import (Adam, CORRECTION_CAP, SCALE, SHAPES, encode_features, export_model, feature_key,
                          initialize_model, load_model, loss_and_gradients,
                          l2_penalty, material_cp_stm, predict_z)

SCHEMA = "regicide.selfplay.dataset.v1"
SPLITS = ("train", "validation", "test")


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path, payload):
    temporary = path.with_suffix(path.suffix + ".partial")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
    os.replace(temporary, path)


def json_lines(path):
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                raise ValueError(f"blank JSONL line {line_number} in {path.name}")
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"malformed JSONL line {line_number} in {path.name}") from error
            if not isinstance(row, dict):
                raise ValueError(f"nonobject JSONL row in {path.name}")
            yield row


def local_data_path(manifest_path, relative):
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ValueError("dataset data paths must be nonempty relative paths")
    path = (manifest_path.parent / relative).resolve()
    if not path.is_relative_to(manifest_path.parent.resolve()) or not path.is_file():
        raise ValueError("dataset data path is missing or escapes its directory")
    return path


def require_integer(value, name):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value


def replay(initial_fen, moves):
    if not isinstance(initial_fen, str) or not isinstance(moves, list):
        raise ValueError("initial_fen and history have invalid types")
    try:
        board = chess.Board(initial_fen)
        if not board.is_valid():
            raise ValueError("invalid initial position")
        for uci in moves:
            if not isinstance(uci, str) or board.is_game_over(claim_draw=False):
                raise ValueError("invalid or postterminal game history")
            move = chess.Move.from_uci(uci)
            if move not in board.legal_moves:
                raise ValueError("illegal move in dataset history")
            board.push(move)
    except (ValueError, TypeError) as error:
        raise ValueError(f"cannot replay dataset history: {error}") from error
    return board


def position_key(board):
    # Clocks and repetition history are not represented by this neural input.
    # Exclude those counters when checking cross-split duplicate positions.
    return " ".join(board.fen(en_passant="fen").split()[:4])


def load_dataset(manifest_path, *, allow_empty_splits=False, model_version=1):
    if type(model_version) is not int or model_version not in (1, 2):
        raise ValueError("unsupported neural model version")
    if model_version == 2:
        from classical_evaluation import classical_cp_stm
    manifest_path = Path(manifest_path).resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA:
        raise ValueError("unsupported dataset manifest schema")
    if manifest.get("status", "complete") not in ("complete", "completed"):
        raise ValueError("dataset collection has not completed")
    positions_path = local_data_path(manifest_path, manifest.get("positions_path", manifest.get("samples_path")))
    games_path = local_data_path(manifest_path, manifest.get("games_path"))
    games, family_splits, exclusions = {}, {}, Counter()
    for game in json_lines(games_path):
        game_id = game.get("game_id")
        if not isinstance(game_id, str) or not game_id or game_id in games:
            raise ValueError("game ids must be unique nonempty strings")
        split, family = game.get("split"), game.get("family")
        if split not in SPLITS or not isinstance(family, str) or not family:
            raise ValueError("invalid game split or opening family")
        if family in family_splits and family_splits[family] != split:
            raise ValueError("opening family appears in multiple splits")
        family_splits[family] = split
        if not isinstance(game.get("completed"), bool):
            raise ValueError("completed must be boolean")
        if game["completed"]:
            board = replay(game.get("initial_fen"), game.get("history"))
            outcome = board.outcome(claim_draw=True)
            if outcome is None or outcome.result() != game.get("result"):
                raise ValueError("completed game result disagrees with replayed chess rules")
        else:
            exclusions["incomplete_games"] += 1
        games[game_id] = game
    if not games or (not allow_empty_splits and not any(game["completed"] for game in games.values())):
        raise ValueError("no completed games in dataset")
    rows = {split: [] for split in SPLITS}
    position_splits, feature_splits, seen_samples = {}, {}, set()
    turns = {split: Counter() for split in SPLITS}
    for sample in json_lines(positions_path):
        game_id = sample.get("game_id")
        if game_id not in games:
            raise ValueError("sample refers to an unknown game")
        game = games[game_id]
        if not game["completed"]:
            exclusions["samples_from_incomplete_games"] += 1
            continue
        for field in ("split", "family", "initial_fen"):
            if sample.get(field) != game.get(field):
                raise ValueError(f"sample/game {field} mismatch")
        history = sample.get("history")
        if not isinstance(history, list) or history != game["history"][:len(history)]:
            raise ValueError("sample history is not a prefix of its game")
        ply = require_integer(sample.get("ply"), "ply")
        if ply != len(history):
            raise ValueError("sample ply disagrees with history")
        sample_id = (game_id, ply)
        if sample_id in seen_samples:
            raise ValueError("duplicate sample within a game")
        seen_samples.add(sample_id)
        board = replay(sample["initial_fen"], history)
        if board.fen(en_passant="fen") != sample.get("fen"):
            raise ValueError("sample FEN disagrees with replayed history")
        turn = "w" if board.turn else "b"
        if sample.get("turn") != turn or board.is_game_over(claim_draw=True) or board.is_check():
            raise ValueError("sample must be a nonterminal position outside check with matching turn")
        cp = require_integer(sample.get("teacher_cp_stm"), "teacher_cp_stm")
        material = require_integer(sample.get("material_cp_stm"), "material_cp_stm")
        if material != material_cp_stm(board):
            raise ValueError("material label disagrees with feature convention")
        outcome = require_integer(sample.get("outcome_stm"), "outcome_stm")
        result_white = {"1-0": 1, "0-1": -1, "1/2-1/2": 0}[game["result"]]
        expected_outcome = result_white if board.turn else -result_white
        if outcome != expected_outcome:
            raise ValueError("outcome label has wrong side-to-move perspective")
        if require_integer(sample.get("teacher_depth"), "teacher_depth") < 1:
            raise ValueError("teacher must complete at least one depth")
        if require_integer(sample.get("teacher_nodes"), "teacher_nodes") < 1:
            raise ValueError("teacher nodes must be positive")
        pv = sample.get("teacher_pv")
        if not isinstance(pv, list) or not pv:
            raise ValueError("teacher PV must be nonempty")
        replay(board.fen(en_passant="fen"), pv)
        split = sample["split"]
        key = position_key(board)
        if key in position_splits and position_splits[key] != split:
            raise ValueError("same canonical position appears across splits")
        position_splits[key] = split
        neural_key = feature_key(board)
        if neural_key in feature_splits and feature_splits[neural_key] != split:
            raise ValueError("same neural feature input appears across splits")
        feature_splits[neural_key] = split
        target = (.8 * math.tanh(cp / SCALE) + .2 * outcome if model_version == 1
                  else math.tanh(cp / SCALE))
        baseline = material if model_version == 1 else classical_cp_stm(board)
        turns[split][turn] += 1
        rows[split].append({"features": encode_features(board), "material": baseline,
                            "target": target, "game_id": game_id, "family": sample["family"]})
    for split in SPLITS:
        if not rows[split] and not allow_empty_splits:
            raise ValueError(f"empty {split} split after exclusions")
    batches = {}
    counts = {}
    for split, samples in rows.items():
        batches[split] = (np.stack([r["features"] for r in samples]) if samples else np.empty((0, 768), dtype=np.float64),
                          np.array([r["material"] for r in samples], dtype=np.float64),
                          np.array([r["target"] for r in samples], dtype=np.float64))
        counts[split] = {"samples": len(samples),
                         "games": len({r["game_id"] for r in samples}),
                         "families": len({r["family"] for r in samples})}
    metadata = {"schema": SCHEMA, "raw_sha256": {"manifest": sha256(manifest_path),
                "positions": sha256(positions_path), "games": sha256(games_path)},
                "counts": counts, "exclusions": dict(exclusions),
                "total_games": len(games), "family_splits": family_splits,
                "collector_exclusions": manifest.get("exclusions", {}),
                "model_version": model_version,
                "turn_distribution": {split: {turn: turns[split][turn] for turn in ("w", "b")}
                                      for split in SPLITS}}
    return batches, metadata


def mse(model, batch):
    features, material, targets = batch
    predicted = (np.tanh(material / SCALE + predict_z(features, model)) if model.version == 1
                 else np.tanh((material + CORRECTION_CAP * np.tanh(predict_z(features, model))) / SCALE))
    value = float(np.mean((predicted - targets) ** 2))
    if not math.isfinite(value):
        raise ValueError("nonfinite evaluation loss")
    return value


def material_mse(batch):
    _, material, targets = batch
    return float(np.mean((np.tanh(material / SCALE) - targets) ** 2))


def train(batches, *, epochs, batch_size, seed, learning_rate=.003, progress=None,
          initial_model=None, model_version=1, l2=None):
    if epochs < 1 or batch_size < 1:
        raise ValueError("epochs and batch size must be positive")
    if type(model_version) is not int or model_version not in (1, 2):
        raise ValueError("unsupported neural model version")
    l2 = (1e-4 if model_version == 2 else 0.0) if l2 is None else l2
    if not math.isfinite(l2) or l2 < 0:
        raise ValueError("L2 coefficient must be finite and nonnegative")
    # A learning cycle carries forward weights, while each cycle starts a fresh
    # Adam optimizer. Epoch 0 remains eligible so validation can retain those
    # exact incumbent weights rather than force an inferior updated checkpoint.
    if initial_model is None:
        initial = initialize_model(seed, version=model_version)
    elif isinstance(initial_model, (str, Path)):
        initial = load_model(initial_model)
    else:
        initial = initial_model.validate().copy()
    if initial.version != model_version:
        raise ValueError("initial model version does not match requested training version")
    model, selected = initial.copy(), initial.copy()
    optimizer = Adam(model, learning_rate)
    rng = np.random.default_rng(seed)
    best_validation = mse(initial, batches["validation"])
    selected_epoch = 0
    history = [{"epoch": 0, "train_mse": mse(initial, batches["train"]),
                "validation_mse": best_validation}]
    if model_version == 2 or l2:
        history[0].update(train_l2_penalty=l2_penalty(initial, l2),
                          train_total_loss=history[0]["train_mse"] + l2_penalty(initial, l2))
    x, material, targets = batches["train"]
    for epoch in range(1, epochs + 1):
        order = rng.permutation(len(x))
        for start in range(0, len(x), batch_size):
            indices = order[start:start + batch_size]
            _, gradients = loss_and_gradients(model, x[indices], material[indices], targets[indices], l2=l2)
            optimizer.step(model, gradients)
        train_loss = mse(model, batches["train"])
        validation_loss = mse(model, batches["validation"])
        history.append({"epoch": epoch, "train_mse": train_loss,
                        "validation_mse": validation_loss})
        if model_version == 2 or l2:
            history[-1].update(train_l2_penalty=l2_penalty(model, l2),
                               train_total_loss=train_loss + l2_penalty(model, l2))
        if validation_loss < best_validation:
            best_validation, selected_epoch, selected = validation_loss, epoch, model.copy()
        if progress is not None:
            progress(history[-1], selected_epoch)
    # The test split is deliberately first touched only after model selection.
    baseline_name = "material_only_mse" if model_version == 1 else "classical_only_mse"
    metrics = {split: {"initial_mse": mse(initial, batches[split]),
                       "selected_mse": mse(selected, batches[split]),
                       baseline_name: material_mse(batches[split])} for split in SPLITS}
    result = {"selected_epoch": selected_epoch, "history": history, "metrics": metrics}
    if model_version == 2 or l2:
        penalty = l2_penalty(selected, l2)
        result["loss_components"] = {"selected_train_mse": metrics["train"]["selected_mse"],
                                     "selected_l2_penalty": penalty,
                                     "selected_train_total": metrics["train"]["selected_mse"] + penalty,
                                     "validation_selection": "MSE only; L2 is a training penalty"}
    return selected, result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--epochs", type=int, default=100)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--seed", type=int, default=20261018)
    parser.add_argument("--learning-rate", type=float, default=.003)
    parser.add_argument("--initial-model", type=Path,
                        help="start from these weights; Adam state is reset; epoch 0 stays eligible")
    parser.add_argument("--model-version", type=int, choices=(1, 2), default=1)
    parser.add_argument("--l2", type=float,
                        help="matrix-only L2; default 0 for V1 and 0.0001 for V2")
    args = parser.parse_args(argv)
    if (args.epochs < 1 or args.batch_size < 1 or args.seed < 0
            or not math.isfinite(args.learning_rate) or args.learning_rate <= 0
            or args.l2 is not None and (not math.isfinite(args.l2) or args.l2 < 0)):
        parser.error("epochs/batch-size/learning-rate must be positive and finite; seed and finite L2 nonnegative")
    effective_l2 = (1e-4 if args.model_version == 2 else 0.0) if args.l2 is None else args.l2
    args.output.mkdir(parents=True, exist_ok=False)
    report_path = args.output / "report.json"
    report = {"schema": "regicide.neural.training.v1", "status": "running",
              "started_utc": datetime.now(timezone.utc).isoformat(),
              "seed": args.seed, "epochs": args.epochs, "batch_size": args.batch_size,
              "learning_rate": args.learning_rate,
              "model_version": args.model_version,
              "model_format": f"REGICIDE_NN_V{args.model_version}",
              "baseline": "material_cp_stm" if args.model_version == 1 else "classical_cp_stm; pawn shield and pawn structure enabled",
              "correction": "600*z" if args.model_version == 1 else "100*tanh(z)",
              "correction_cap_cp": None if args.model_version == 1 else int(CORRECTION_CAP),
              "l2": {"coefficient": effective_l2, "matrices": ["w1", "w2", "w3"],
                     "biases_regularized": False, "formula": "0.5*coefficient*sum(weight_matrices^2)"},
              "optimizer": {"name": "Adam", "beta1": .9, "beta2": .999,
                            "epsilon": 1e-8, "gradient_norm_clip": 10,
                            "state_reset": True,
                            "checkpoint_policy": "weights only; moments and step counter reset each run"},
              "initialization": "warm_start" if args.initial_model is not None else "seeded_random",
              "initial_model_sha256": None,
              "rng": "NumPy default_rng PCG64",
              "implementation_sha256": {
                  "train_neural.py": sha256(Path(__file__)),
                  "neural_model.py": sha256(Path(__file__).with_name("neural_model.py"))},
              "architecture": [768, 32, 16, 1], "scale_cp": 600,
              "parameter_count": sum(math.prod(shape) for shape in SHAPES.values()),
              "target": ("0.8*tanh(teacher_cp_stm/600)+0.2*outcome_stm" if args.model_version == 1
                         else "tanh(teacher_cp_stm/600); outcomes retained for integrity checks only"),
              "loss": ("mean((tanh(material_cp_stm/600+z)-target)^2)" if args.model_version == 1
                       else "mean((tanh((classical_cp_stm+100*tanh(z))/600)-target)^2)+matrix_only_L2"),
              "selection": "lowest validation MSE; strict improvement; earliest tie; epoch 0 eligible",
              "split_policy": "predeclared opening families; no cross-split games/families/canonical positions/exact neural inputs",
              "test_policy": "one final assessment after validation selection; no tuning on test labels",
              "environment": {"python": sys.version.split()[0], "numpy": np.__version__,
                              "python_chess": chess.__version__, "platform": platform.platform(),
                              "arithmetic": "float64 CPU", "threads": 1,
                              "blas": str(np.__config__.CONFIG.get("Build Dependencies", {}).get("blas", {}))}}
    write_json(report_path, report)
    try:
        initial_model = None
        if args.initial_model is not None:
            # Read once before training. Model loading is strict and subsequent
            # source-file replacement cannot alter this in-memory checkpoint.
            before = sha256(args.initial_model)
            initial_model = load_model(args.initial_model)
            if sha256(args.initial_model) != before:
                raise ValueError("initial model changed while being loaded")
            if initial_model.version != args.model_version:
                raise ValueError("initial model version does not match requested training version")
            report["initial_model_sha256"] = before
        batches, metadata = load_dataset(args.dataset, model_version=args.model_version)
        report["dataset"] = metadata
        report["parameters_per_training_sample"] = report["parameter_count"] / metadata["counts"]["train"]["samples"]
        report["turn_distribution"] = metadata["turn_distribution"]
        if args.model_version == 2:
            report["implementation_sha256"]["classical_evaluation.py"] = sha256(Path(__file__).with_name("classical_evaluation.py"))
        def progress(epoch, selected_epoch):
            if epoch["epoch"] == 1 or epoch["epoch"] % 10 == 0 or epoch["epoch"] == args.epochs:
                print(f"epoch {epoch['epoch']}/{args.epochs} train={epoch['train_mse']:.8f} "
                      f"validation={epoch['validation_mse']:.8f} selected={selected_epoch}", flush=True)
        model, result = train(batches, epochs=args.epochs, batch_size=args.batch_size,
                              seed=args.seed, learning_rate=args.learning_rate, progress=progress,
                              initial_model=initial_model, model_version=args.model_version,
                              l2=effective_l2)
        model_path = args.output / f"small-v{args.model_version}.nn"
        export_model(model, model_path)
        report.update(result)
        report.update({"status": "complete", "model_path": model_path.name,
                       "model_sha256": sha256(model_path),
                       "finished_utc": datetime.now(timezone.utc).isoformat(),
                       "strength_status": "unmeasured; validation loss is not playing-strength evidence"})
        write_json(report_path, report)
        print(json.dumps({"status": "complete", "selected_epoch": result["selected_epoch"],
                          "model_sha256": report["model_sha256"]}), flush=True)
        return 0
    except Exception as error:
        report.update({"status": "failed", "error_type": type(error).__name__, "error": str(error),
                       "finished_utc": datetime.now(timezone.utc).isoformat()})
        write_json(report_path, report)
        print(f"training failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
