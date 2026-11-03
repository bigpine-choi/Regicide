"""Generate a fresh post-freeze game holdout and compare frozen integer models.
No selection, optimization, or model rewriting occurs in this module.
"""
from __future__ import annotations
import argparse
from collections import defaultdict
import datetime
import json
from pathlib import Path
import random
import sys
import chess
import numpy as np
import neural_training as base
import neural_qat as qat


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def frozen_models(freeze_dir):
    freeze_dir = Path(freeze_dir).resolve()
    document = qat.verify_freeze(freeze_dir / "freeze.json")
    selection = json.loads(qat.resolve_frozen_file(document["files"]["selection"]).read_text(encoding="utf-8"))
    if document["selected_config"] != selection["selected_config"]:
        raise ValueError("frozen configuration differs from selected configuration")
    calibration = freeze_dir / "calibration_driver.json"
    if calibration.exists():
        record = json.loads(calibration.read_text(encoding="utf-8"))
        if record["freeze_sha256"] != base.sha256(freeze_dir / "freeze.json"):
            raise ValueError("calibration freeze mismatch")
        for role in ("plan", "driver"):
            entry = record[role]
            if base.sha256(Path(entry["path"])) != entry["sha256"]:
                raise ValueError("calibration dependency changed: " + role)
        plan = json.loads(Path(record["plan"]["path"]).read_text(encoding="utf-8"))
        executed = json.loads(qat.resolve_frozen_file(document["files"]["grid_plan"]).read_text(encoding="utf-8"))
        if plan["candidate_grid"] != executed["candidate_grid"]:
            raise ValueError("calibration plan differs from executed frozen grid")
    # Field names are deliberately centralized for the selection protocol.
    return document


def _paths(document, freeze_dir):
    baseline = qat.resolve_frozen_file(document["files"]["baseline_model"])
    selected = qat.resolve_frozen_file(document["files"]["selected_model"])
    return baseline, selected


def previous_inputs(data):
    data = Path(data).resolve()
    manifest = json.loads((data / "split_manifest.json").read_text(encoding="utf-8"))
    identities = set()
    for name in ("train", "validation"):
        record = manifest["partitions"][name]
        if record["file"] != name + ".jsonl": raise ValueError("unexpected original partition filename")
        path = data / record["file"]
        if base.sha256(path) != record["sha256"]: raise ValueError("original dataset hash mismatch")
        for line in path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            key = base.feature_identity(chess.Board(row["fen"]))
            if key != row["feature_hash"]: raise ValueError("original feature identity mismatch")
            identities.add(key)
    # Original validation labels are not used; only board identities are needed.
    return manifest, identities


def generate_holdout(freeze_dir, original_data, output, engine, *, games=64,
                     max_plies=64, sample_every=2, seed=20261022):
    if type(games) is not int or games < 1 or not 8 <= max_plies <= 512 or sample_every < 1:
        raise ValueError("invalid holdout generation limits")
    freeze_dir, original_data, output, engine = map(lambda p: Path(p).resolve(),
        (freeze_dir, original_data, output, engine))
    freeze = frozen_models(freeze_dir)
    freeze_sha = base.sha256(freeze_dir / "freeze.json")
    original, excluded = previous_inputs(original_data)
    if base.sha256(original_data / "split_manifest.json") != freeze["files"]["dataset_manifest"]["sha256"]:
        raise ValueError("holdout exclusions do not match frozen original dataset")
    if base.sha256(original_data / "train.jsonl") != freeze["files"]["training_data"]["sha256"]:
        raise ValueError("holdout exclusions do not match frozen training data")
    base.fresh_directory(output)
    records, raw_games, seen = [], [], set()
    prior_removed = within_removed = candidates = 0
    started = utc_now()
    for game_index in range(games):
        group = f"qat-heldout-{seed}-game-{game_index:06d}"
        if group in original["groups"]: raise ValueError("new holdout group overlaps original game")
        rng = random.Random(seed + 1000003 * (game_index + 1))
        board, moves = chess.Board(), []
        for ply in range(1, max_plies + 1):
            legal = sorted(board.legal_moves, key=lambda move: move.uci())
            if not legal: break
            move = rng.choice(legal); moves.append(move.uci()); board.push(move)
            if board.outcome(claim_draw=True) is not None: break
            if ply < 6 or (ply - 6) % sample_every: continue
            candidates += 1
            identity = base.feature_identity(board)
            if identity in excluded:
                prior_removed += 1; continue
            if identity in seen:
                within_removed += 1; continue
            seen.add(identity)
            records.append({"fen": board.fen(en_passant="fen"), "group_id": group,
                            "ply": ply, "feature_hash": identity})
        raw_games.append({"group_id": group, "moves_uci": moves})
    if not records: raise ValueError("no fresh holdout positions")
    labels = base.evaluate_engine(engine, [row["fen"] for row in records])
    for row, (target, _, _) in zip(records, labels, strict=True):
        board = chess.Board(row["fen"])
        row["classical_stm_cp"] = target
        row["target_white_cp"] = target * (1 if board.turn else -1)
    rows_path, raw_path = output / "holdout.jsonl", output / "raw_games.jsonl"
    for path, rows in ((rows_path, records), (raw_path, raw_games)):
        path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
    manifest = {"schema_version": 1, "created_utc": started, "finished_utc": utc_now(),
        "freeze_path": str(freeze_dir / "freeze.json"), "freeze_sha256": freeze_sha,
        "calibration_receipt_sha256": base.sha256(freeze_dir / "calibration_driver.json") if (freeze_dir / "calibration_driver.json").exists() else None,
        "original_data": str(original_data), "original_manifest_sha256": base.sha256(original_data / "split_manifest.json"),
        "generator_sha256": base.sha256(Path(__file__)), "engine_sha256": base.sha256(engine),
        "seed": seed, "games": games, "max_plies": max_plies, "sample_start_ply": 6,
        "sample_every": sample_every, "candidates": candidates, "rows": len(records),
        "excluded_prior_inputs": prior_removed, "duplicate_inputs_removed": within_removed,
        "original_game_overlap": 0, "original_mirrored_feature_overlap": 0,
        "holdout_sha256": base.sha256(rows_path), "raw_games_sha256": base.sha256(raw_path),
        "policy": "Generated after selected model/config freeze; only prior input identities used for exclusion; no original validation labels used",
        "label_source": "unchanged Regicide own classical static evaluator, white cp targets"}
    if base.sha256(freeze_dir / "freeze.json") != freeze_sha: raise ValueError("freeze changed during holdout generation")
    frozen_models(freeze_dir)
    base.write_json(output / "holdout_manifest.json", manifest)
    return manifest


def compare_records(records, baseline_model, qat_model):
    target = np.array([row["target_white_cp"] for row in records], dtype=np.float64)
    predictions = {name: np.array([base.quantized_white(chess.Board(row["fen"]), model)
        for row in records], dtype=np.int64) for name, model in (("baseline", baseline_model), ("qat", qat_model))}
    by_group = defaultdict(list)
    for i, row in enumerate(records): by_group[row["group_id"]].append(i)
    metrics = {name: base.error_metrics(values, target) for name, values in predictions.items()}
    return {"positions": len(records), "groups": len(by_group), "metrics": metrics,
        "mae_reduction_cp": metrics["baseline"]["mae_cp"] - metrics["qat"]["mae_cp"],
        "per_game": [{"group_id": group, "positions": len(indices), **{
            name: base.error_metrics(values[indices], target[indices])
            for name, values in predictions.items()}} for group, indices in sorted(by_group.items())],
        "predictions": {name: values.tolist() for name, values in predictions.items()}}


def evaluate_holdout(freeze_dir, holdout_dir, output, engine):
    freeze_dir, holdout_dir, output, engine = map(lambda p: Path(p).resolve(),
        (freeze_dir, holdout_dir, output, engine))
    if output.exists(): raise ValueError("refusing to overwrite holdout evaluation")
    freeze = frozen_models(freeze_dir)
    freeze_sha = base.sha256(freeze_dir / "freeze.json")
    metadata = json.loads((holdout_dir / "holdout_manifest.json").read_text(encoding="utf-8"))
    if metadata["freeze_sha256"] != freeze_sha: raise ValueError("holdout belongs to different freeze")
    calibration = freeze_dir / "calibration_driver.json"
    receipt_sha = base.sha256(calibration) if calibration.exists() else None
    if metadata["calibration_receipt_sha256"] != receipt_sha:
        raise ValueError("calibration receipt changed after holdout generation")
    if metadata["generator_sha256"] != base.sha256(Path(__file__)): raise ValueError("holdout generator/evaluator source changed")
    if metadata["engine_sha256"] != base.sha256(engine): raise ValueError("holdout teacher engine changed")
    if metadata["raw_games_sha256"] != base.sha256(holdout_dir / "raw_games.jsonl"):
        raise ValueError("holdout raw game hash mismatch")
    path = holdout_dir / "holdout.jsonl"
    if metadata["holdout_sha256"] != base.sha256(path): raise ValueError("holdout hash mismatch")
    original, excluded = previous_inputs(metadata["original_data"])
    if metadata["original_manifest_sha256"] != base.sha256(Path(metadata["original_data"]) / "split_manifest.json"):
        raise ValueError("original dataset manifest changed")
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    if len(records) != metadata["rows"] or not records: raise ValueError("invalid holdout count")
    seen = set()
    for row in records:
        board = chess.Board(row["fen"]); key = base.feature_identity(board)
        if not board.is_valid() or board.outcome(claim_draw=True) is not None: raise ValueError("invalid holdout board")
        if key in seen or key in excluded or key != row["feature_hash"]: raise ValueError("holdout input overlap")
        if row["group_id"] in original["groups"]: raise ValueError("holdout game overlap")
        if type(row["target_white_cp"]) is not int or type(row["classical_stm_cp"]) is not int:
            raise ValueError("holdout targets must be integer cp")
        if row["target_white_cp"] != row["classical_stm_cp"] * (1 if board.turn else -1):
            raise ValueError("holdout score perspective mismatch")
        seen.add(key)
    old_path, qat_path = _paths(freeze, freeze_dir)
    models = {"baseline": base.load_model(old_path), "qat": base.load_model(qat_path)}
    report = compare_records(records, models["baseline"], models["qat"])
    for name, model_path in (("baseline", old_path), ("qat", qat_path)):
        cpp = base.evaluate_engine(engine, [row["fen"] for row in records], model_path)
        for i, (row, values) in enumerate(zip(records, cpp, strict=True)):
            target_stm = row["classical_stm_cp"]
            predicted_stm = report["predictions"][name][i] * (1 if chess.Board(row["fen"]).turn else -1)
            if values[0] != target_stm: raise ValueError("holdout label differs from current teacher")
            if values[1] != values[2] or values[1] != predicted_stm: raise ValueError("Python/C++ full/incremental mismatch")
    report.update({"schema_version": 1, "evaluated_utc": utc_now(), "freeze_sha256": freeze_sha,
        "holdout_manifest_sha256": base.sha256(holdout_dir / "holdout_manifest.json"),
        "holdout_sha256": base.sha256(path), "engine_sha256": base.sha256(engine),
        "evaluator_sha256": base.sha256(Path(__file__)), "python_cpp_mismatches": 0,
        "baseline_model_sha256": base.sha256(old_path), "qat_model_sha256": base.sha256(qat_path),
        "scope": "static-evaluator imitation on one fresh game-group holdout; no Elo or playing-strength conclusion"})
    frozen_models(freeze_dir)
    if base.sha256(freeze_dir / "freeze.json") != freeze_sha: raise ValueError("freeze changed during evaluation")
    output.parent.mkdir(parents=True, exist_ok=True); base.write_json(output, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    generate = sub.add_parser("generate")
    generate.add_argument("--freeze", type=Path, required=True); generate.add_argument("--original-data", type=Path, required=True)
    generate.add_argument("--engine", type=Path, required=True); generate.add_argument("--output", type=Path, required=True)
    generate.add_argument("--games", type=int, default=64); generate.add_argument("--max-plies", type=int, default=64)
    generate.add_argument("--sample-every", type=int, default=2); generate.add_argument("--seed", type=int, default=20261022)
    evaluate = sub.add_parser("evaluate")
    for flag in ("freeze", "holdout", "output", "engine"): evaluate.add_argument("--" + flag, type=Path, required=True)
    args = parser.parse_args()
    if args.command == "generate": result = generate_holdout(args.freeze, args.original_data, args.output,
        args.engine, games=args.games, max_plies=args.max_plies, sample_every=args.sample_every, seed=args.seed)
    else: result = evaluate_holdout(args.freeze, args.holdout, args.output, args.engine)
    print(json.dumps({key: value for key, value in result.items() if key not in {"predictions", "per_game"}}, indent=2))

if __name__ == "__main__":
    main()
