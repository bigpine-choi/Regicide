"""Freeze a precision-QAT experiment before generating a fresh game holdout.

No fitting or model selection occurs here. Prior labels are ignored when their
board identities are collected for exclusion. Every reference/ablation is fixed
before the new holdout exists, then evaluated without further adjustment.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import copy
import datetime
import json
from pathlib import Path
import random

import chess
import numpy as np

import neural_training as base
import neural_precision as precision
import neural_qat_experiment as previous

ROOT = Path(__file__).resolve().parents[1]


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def write_new(path, value):
    with Path(path).open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")


def file_entry(path):
    path = Path(path).resolve()
    try:
        name = path.relative_to(ROOT).as_posix()
    except ValueError:
        name = str(path)
    return {"path": name, "sha256": base.sha256(path)}


def resolve(entry):
    path = Path(entry["path"])
    return path if path.is_absolute() else ROOT / path


def prior_inputs(original_data, prior_holdout):
    original_data, prior_holdout = Path(original_data), Path(prior_holdout)
    original, identities = previous.previous_inputs(original_data)
    groups = set(original["groups"])
    meta = json.loads((prior_holdout / "holdout_manifest.json").read_text(encoding="utf-8"))
    if base.sha256(prior_holdout / "holdout.jsonl") != meta["holdout_sha256"]:
        raise ValueError("prior holdout hash mismatch")
    if base.sha256(prior_holdout / "raw_games.jsonl") != meta["raw_games_sha256"]:
        raise ValueError("prior raw game hash mismatch")
    rows = [json.loads(line) for line in (prior_holdout / "holdout.jsonl").read_text(encoding="utf-8").splitlines()]
    if len(rows) != meta["rows"]:
        raise ValueError("prior holdout row count mismatch")
    for row in rows:
        identity = base.feature_identity(chess.Board(row["fen"]))
        if identity != row["feature_hash"]:
            raise ValueError("prior feature identity mismatch")
        identities.add(identity)
        groups.add(row["group_id"])
    raw = [json.loads(line) for line in (prior_holdout / "raw_games.jsonl").read_text(encoding="utf-8").splitlines()]
    groups.update(game["group_id"] for game in raw)
    return identities, groups


def verify_training_plan(selected, protocol):
    entry = selected["files"].get("calibration_plan")
    if entry is None or base.sha256(protocol) != entry["sha256"]:
        raise ValueError("experiment protocol differs from selected training plan")
    plan = json.loads(Path(protocol).read_text(encoding="utf-8"))
    executed = json.loads(resolve(selected["files"]["grid_plan"]).read_text(encoding="utf-8"))
    if plan["candidate_grid"] != executed["candidate_grid"]:
        raise ValueError("selected candidate grid differs from frozen protocol")
    return plan


def freeze_experiment(selection, original_data, prior_holdout, engine, protocol, output, holdout):
    selection, original_data, prior_holdout, engine, protocol, output, holdout = (
        Path(p).resolve() for p in (selection, original_data, prior_holdout, engine, protocol, output, holdout))
    if holdout.exists():
        raise ValueError("fresh holdout already exists before experiment freeze")
    selected = precision.verify_freeze(selection / "freeze.json")
    plan = verify_training_plan(selected, protocol)
    decision = json.loads((selection / "selection.json").read_text(encoding="utf-8"))
    if selected["selected_config"] != decision["selected_config"]:
        raise ValueError("selected configuration differs from freeze")
    if base.sha256(selection / "model.nnue") != selected["files"]["selected_model"]["sha256"]:
        raise ValueError("selected model path differs from training freeze")
    baseline = resolve(selected["files"]["baseline_model"])
    initial_path = resolve(selected["files"]["initial_floats"])
    model = precision.load_model(selection / "model.nnue")
    scale = int(model["input_scale"])
    reference = precision.quantize(precision.load_float_parameters(initial_path), scale)
    changed = int(np.count_nonzero(model["input_weights"] != reference["input_weights"]))
    if changed == 0:
        raise ValueError("selected input weights have no learning changes at the same scale")
    if base.sha256(original_data / "train.jsonl") != selected["files"]["training_data"]["sha256"]:
        raise ValueError("original data differs from selected training data")
    if base.sha256(original_data / "split_manifest.json") != selected["files"]["dataset_manifest"]["sha256"]:
        raise ValueError("original manifest differs from selected training data")
    prior_inputs(original_data, prior_holdout)
    base.fresh_directory(output)
    precision.save_model(reference, output / "same-scale-initial.nnue")
    reverted = copy.deepcopy(model)
    reverted["input_weights"] = reference["input_weights"].copy()
    precision.save_model(reverted, output / "trained-inputs-reverted.nnue")
    paths = {"selected_training_freeze": selection / "freeze.json", "protocol": protocol,
             "baseline_model": baseline, "precision_model": selection / "model.nnue",
             "same_scale_initial": output / "same-scale-initial.nnue",
             "trained_inputs_reverted": output / "trained-inputs-reverted.nnue",
             "engine": engine, "experiment_source": Path(__file__),
             "prior_experiment_source": Path(previous.__file__),
             "original_manifest": original_data / "split_manifest.json",
             "original_train": original_data / "train.jsonl", "original_validation": original_data / "validation.jsonl",
             "prior_holdout_manifest": prior_holdout / "holdout_manifest.json",
             "prior_holdout": prior_holdout / "holdout.jsonl", "prior_raw_games": prior_holdout / "raw_games.jsonl"}
    for directory in (ROOT / "src", ROOT / "include"):
        for path in directory.rglob("*"):
            if path.is_file():
                paths[path.relative_to(ROOT).as_posix()] = path
    record = {"schema_version": 1, "created_utc": utc_now(), "selection_directory": str(selection),
              "original_data": str(original_data), "prior_holdout_directory": str(prior_holdout),
              "new_holdout_directory": str(holdout), "holdout_did_not_exist": True,
              "selected_config": selected["selected_config"], "holdout_plan": plan["holdout_plan"],
              "input_integer_changes_from_same_scale_initial": changed,
              "files": {key: file_entry(path) for key, path in paths.items()},
              "policy": "All models, references, runtime, data identities and sources fixed before new holdout; no tuning here"}
    write_new(output / "freeze_receipt.json", record)
    verify_experiment(output)
    return record


def verify_experiment(directory):
    record = json.loads((Path(directory) / "freeze_receipt.json").read_text(encoding="utf-8"))
    if record.get("schema_version") != 1:
        raise ValueError("unsupported experiment freeze")
    for name, entry in record["files"].items():
        if base.sha256(resolve(entry)) != entry["sha256"]:
            raise ValueError("frozen experiment file changed: " + name)
    selected = precision.verify_freeze(Path(record["selection_directory"]) / "freeze.json")
    if selected["selected_config"] != record["selected_config"]:
        raise ValueError("frozen selection changed")
    plan = verify_training_plan(selected, resolve(record["files"]["protocol"]))
    if record["holdout_plan"] != plan["holdout_plan"]:
        raise ValueError("holdout plan differs from frozen protocol")
    if selected["files"]["dataset_manifest"]["sha256"] != record["files"]["original_manifest"]["sha256"]:
        raise ValueError("original manifest differs from training freeze")
    if selected["files"]["training_data"]["sha256"] != record["files"]["original_train"]["sha256"]:
        raise ValueError("original train differs from training freeze")
    return record


def sample_games(plan, excluded, old_groups):
    games, seed = plan["games"], plan["seed"]
    max_plies, sample_every = plan["max_plies"], plan["sample_every"]
    start = plan["sample_start_ply"]
    if not (type(games) is int and games > 0 and type(seed) is int and seed >= 0
            and type(max_plies) is int and 8 <= max_plies <= 512
            and type(sample_every) is int and sample_every > 0 and 1 <= start <= max_plies):
        raise ValueError("invalid frozen generation settings")
    rows, raw, seen = [], [], set()
    candidates = removed = duplicates = 0
    for index in range(games):
        group = f"precision-heldout-{seed}-game-{index:06d}"
        if group in old_groups:
            raise ValueError("new game overlaps prior game identity")
        rng = random.Random(seed + 1000003 * (index + 1))
        board, moves = chess.Board(), []
        for ply in range(1, max_plies + 1):
            legal = sorted(board.legal_moves, key=lambda move: move.uci())
            if not legal:
                break
            move = rng.choice(legal)
            board.push(move)
            moves.append(move.uci())
            if board.outcome(claim_draw=True) is not None:
                break
            if ply < start or (ply - start) % sample_every:
                continue
            candidates += 1
            identity = base.feature_identity(board)
            if identity in excluded:
                removed += 1
                continue
            if identity in seen:
                duplicates += 1
                continue
            seen.add(identity)
            rows.append({"fen": board.fen(en_passant="fen"), "group_id": group,
                         "ply": ply, "feature_hash": identity})
        raw.append({"group_id": group, "moves_uci": moves})
    if not rows:
        raise ValueError("fresh holdout is empty")
    return rows, raw, {"candidate_positions": candidates, "excluded_prior_inputs": removed,
                       "duplicate_inputs_removed": duplicates}


def generate_holdout(directory):
    directory = Path(directory).resolve()
    record = verify_experiment(directory)
    frozen_sha = base.sha256(directory / "freeze_receipt.json")
    output = Path(record["new_holdout_directory"])
    if output.exists():
        raise ValueError("refusing to overwrite fresh holdout")
    started = utc_now()
    excluded, old_groups = prior_inputs(record["original_data"], record["prior_holdout_directory"])
    rows, raw, counts = sample_games(record["holdout_plan"], excluded, old_groups)
    labels = base.evaluate_engine(resolve(record["files"]["engine"]), [row["fen"] for row in rows])
    for row, values in zip(rows, labels, strict=True):
        row["classical_stm_cp"] = values[0]
        row["target_white_cp"] = values[0] * (1 if chess.Board(row["fen"]).turn else -1)
    base.fresh_directory(output)
    for name, data in (("holdout.jsonl", rows), ("raw_games.jsonl", raw)):
        with (output / name).open("x", encoding="utf-8", newline="\n") as handle:
            for row in data:
                handle.write(json.dumps(row, sort_keys=True) + "\n")
    meta = {"schema_version": 1, "created_utc": started, "finished_utc": utc_now(),
            "freeze_receipt_sha256": frozen_sha, "holdout_plan": record["holdout_plan"],
            "groups": len(raw), "positions": len(rows), **counts,
            "prior_mirrored_input_overlap": 0, "prior_game_overlap": 0,
            "holdout_sha256": base.sha256(output / "holdout.jsonl"),
            "raw_games_sha256": base.sha256(output / "raw_games.jsonl"),
            "teacher": "unchanged own Regicide classical static evaluation; white centipawns"}
    verify_experiment(directory)
    if base.sha256(directory / "freeze_receipt.json") != frozen_sha:
        raise ValueError("freeze receipt changed during generation")
    write_new(output / "holdout_manifest.json", meta)
    return meta


def evaluate_holdout(directory, output):
    directory, output = Path(directory).resolve(), Path(output).resolve()
    if output.exists():
        raise ValueError("refusing to overwrite heldout report")
    record = verify_experiment(directory)
    receipt_sha = base.sha256(directory / "freeze_receipt.json")
    data = Path(record["new_holdout_directory"])
    meta = json.loads((data / "holdout_manifest.json").read_text(encoding="utf-8"))
    if meta["freeze_receipt_sha256"] != receipt_sha or meta["holdout_plan"] != record["holdout_plan"]:
        raise ValueError("holdout belongs to different frozen experiment")
    for name, key in (("holdout.jsonl", "holdout_sha256"), ("raw_games.jsonl", "raw_games_sha256")):
        if base.sha256(data / name) != meta[key]:
            raise ValueError("holdout hash mismatch")
    rows = [json.loads(line) for line in (data / "holdout.jsonl").read_text(encoding="utf-8").splitlines()]
    excluded, old_groups = prior_inputs(record["original_data"], record["prior_holdout_directory"])
    expected, raw, counts = sample_games(record["holdout_plan"], excluded, old_groups)
    actual_raw = [json.loads(line) for line in (data / "raw_games.jsonl").read_text(encoding="utf-8").splitlines()]
    if raw != actual_raw or len(rows) != len(expected) or meta["positions"] != len(rows):
        raise ValueError("raw game or sampled position count differs from frozen RNG replay")
    if any(meta[key] != value for key, value in counts.items()):
        raise ValueError("holdout generation counters differ")
    for row, wanted in zip(rows, expected, strict=True):
        if any(row.get(key) != value for key, value in wanted.items()):
            raise ValueError("sampled board differs from legal frozen game replay")
        if type(row["target_white_cp"]) is not int or type(row["classical_stm_cp"]) is not int:
            raise ValueError("heldout target must be integer centipawns")
        if row["target_white_cp"] != row["classical_stm_cp"] * (1 if chess.Board(row["fen"]).turn else -1):
            raise ValueError("heldout target perspective mismatch")
    target = np.array([row["target_white_cp"] for row in rows], dtype=np.float64)
    fens = [row["fen"] for row in rows]
    names = ("baseline_model", "precision_model", "same_scale_initial", "trained_inputs_reverted")
    predictions, metrics = {}, {}
    for name in names:
        model_path = resolve(record["files"][name])
        model = precision.load_model(model_path)
        values = np.array([precision.quantized_white(chess.Board(fen), model) for fen in fens], dtype=np.int64)
        cpp = base.evaluate_engine(resolve(record["files"]["engine"]), fens, model_path)
        for index, (row, result) in enumerate(zip(rows, cpp, strict=True)):
            signed = int(values[index]) * (1 if chess.Board(row["fen"]).turn else -1)
            if result[0] != row["classical_stm_cp"]:
                raise ValueError("heldout label differs from unchanged teacher")
            if result[1] != result[2] or result[1] != signed:
                raise ValueError("Python/full/incremental prediction mismatch")
        predictions[name] = values
        metrics[name] = base.error_metrics(values, target)
    groups = defaultdict(list)
    for index, row in enumerate(rows):
        groups[row["group_id"]].append(index)
    report = {"schema_version": 1, "evaluated_utc": utc_now(), "positions": len(rows), "groups": len(groups),
              "metrics": metrics, "predictions": {key: value.tolist() for key, value in predictions.items()},
              "per_game": [{"group_id": group, "positions": len(indices), **{
                  key: base.error_metrics(values[indices], target[indices]) for key, values in predictions.items()}}
                  for group, indices in sorted(groups.items())],
              "mae_reduction_cp": metrics["baseline_model"]["mae_cp"] - metrics["precision_model"]["mae_cp"],
              "trained_input_changes_affect_prediction_rows": int(np.count_nonzero(
                  predictions["precision_model"] != predictions["trained_inputs_reverted"])),
              "python_cpp_mismatches": 0, "teacher_label_mismatches": 0,
              "freeze_receipt_sha256": receipt_sha, "holdout_manifest_sha256": base.sha256(data / "holdout_manifest.json"),
              "model_sha256": {key: record["files"][key]["sha256"] for key in names},
              "scope": "Static score imitation on one new random game-group holdout; no playing-strength or Elo inference"}
    verify_experiment(directory)
    if base.sha256(directory / "freeze_receipt.json") != receipt_sha:
        raise ValueError("freeze receipt changed during evaluation")
    output.parent.mkdir(parents=True, exist_ok=True)
    write_new(output, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    freeze = sub.add_parser("freeze")
    for name in ("selection", "original-data", "prior-holdout", "engine", "protocol", "output", "holdout"):
        freeze.add_argument("--" + name, type=Path, required=True)
    generate = sub.add_parser("generate")
    generate.add_argument("--freeze", type=Path, required=True)
    evaluate = sub.add_parser("evaluate")
    evaluate.add_argument("--freeze", type=Path, required=True)
    evaluate.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "freeze":
        result = freeze_experiment(args.selection, args.original_data, args.prior_holdout, args.engine,
                                   args.protocol, args.output, args.holdout)
    elif args.command == "generate":
        result = generate_holdout(args.freeze)
    else:
        result = evaluate_holdout(args.freeze, args.output)
    print(json.dumps({key: value for key, value in result.items() if key not in {"predictions", "per_game", "files"}}, indent=2))


if __name__ == "__main__":
    main()
