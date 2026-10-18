"""Measure a preregistered, nested endgame-data learning curve.

The bootstrap dataset owns epoch-selection validation and the original test.
New endgame holdouts are measurements only. No arena, learning-store registry,
champion, adoption threshold, or source dataset is modified by this tool.
"""

import argparse
from collections import Counter, defaultdict, deque
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import sys

# The trainer configures one-thread arithmetic before importing NumPy.
import train_neural as TRAIN
import numpy as np

from learning_corpus import _source
from neural_model import export_model, feature_key, initialize_model, load_model
from teacher_quality import RESERVED_OPENINGS, _reserved_guard


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "regicide.endgame.learning-curve.v1"
DEFAULT_COUNTS = (0, 128, 512, 1024)
DEFAULT_SEEDS = (2026101901, 2026101902)
SCHEDULE_SEED = 2026101900


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode("utf-8")).hexdigest()


def _raw_source(path):
    source = _source(path, allow_empty_splits=True)
    if source["manifest"].get("status") != "complete":
        raise ValueError("learning-curve sources must be complete")
    for key, rows in (("games", source["games"]), ("positions", source["positions"])):
        field = "games_path" if key == "games" else "positions_path"
        relative = source["manifest"].get(field, source["manifest"].get("samples_path"))
        raw = TRAIN.local_data_path(source["path"], relative).read_bytes()
        lines = raw.splitlines(keepends=True)
        if len(lines) != len(rows) or any(json.loads(line) != row for line, row in zip(lines, rows)):
            raise ValueError("source JSONL bytes changed during validation")
        # Concatenation must not alter the last source row or join two JSON rows.
        if lines and not lines[-1].endswith(b"\n"):
            raise ValueError("source JSONL must end with a newline for byte-preserving composition")
        source[key + "_raw"] = raw
        source[key + "_lines"] = lines
    source["pgn_raw"] = (TRAIN.local_data_path(source["path"], source["manifest"]["pgn_path"]).read_bytes()
                         if "pgn_path" in source["manifest"] else None)
    unfinished = {game["game_id"] for game in source["games"] if not game["completed"]}
    if any(row["game_id"] in unfinished for row in source["positions"]):
        raise ValueError("learning-curve rows may not refer to an unfinished game")
    for game in source["games"]:
        if not game["completed"] and TRAIN.replay(game["initial_fen"], game["history"]).outcome(claim_draw=True) is not None:
            raise ValueError("terminal game is incorrectly marked unfinished")
    source["unused_incomplete_games"] = len(unfinished)
    source["reserved_guard"] = _reserved_guard(source)
    return source


def _keys(row):
    board = TRAIN.replay(row["initial_fen"], row["history"])
    return TRAIN.position_key(board), feature_key(board)


def _holdout(source):
    return {split: {"samples": sum(row["split"] == split for row in source["positions"]),
                    "raw_rows_sha256": hashlib.sha256(b"".join(
                        line for row, line in zip(source["positions"], source["positions_lines"])
                        if row["split"] == split)).hexdigest()}
            for split in ("validation", "test")}


def prepare_schedule(baseline, additions, schedule_seed=SCHEDULE_SEED):
    """Validate disjoint holdouts, then order unique additions by family/game.

    Exact position and network-input duplicates of the baseline are excluded,
    never substituted for the original row. Each active family contributes one
    row per round; within a family actual turns alternate when possible and
    active games within each turn contribute one per round.
    The schedule is independent of labels, losses, and all training RNG seeds.
    """
    if type(schedule_seed) is not int or schedule_seed < 0:
        raise ValueError("schedule seed must be a nonnegative integer")
    for split in TRAIN.SPLITS:
        if not any(row["split"] == split for row in baseline["positions"]):
            raise ValueError("bootstrap train/validation/test splits must all be nonempty")
    game_ids = {game["game_id"] for game in baseline["games"]}
    if game_ids.intersection(game["game_id"] for game in additions["games"]):
        raise ValueError("source datasets reuse a game id")
    histories = {(game["initial_fen"], tuple(game["history"])) for game in baseline["games"]}
    if any((game["initial_fen"], tuple(game["history"])) in histories for game in additions["games"]):
        raise ValueError("endgame dataset reuses a bootstrap game history")
    grouped_histories = {}
    for game in (*baseline["games"], *additions["games"]):
        key = (game["initial_fen"], tuple(game["history"]))
        if key in grouped_histories and grouped_histories[key] != game["split"]:
            raise ValueError("same complete game history appears across splits")
        grouped_histories[key] = game["split"]
    family_splits = dict(baseline["metadata"]["family_splits"])
    for family, split in additions["metadata"]["family_splits"].items():
        if family in family_splits and family_splits[family] != split:
            raise ValueError("opening family changes split between source datasets")
        family_splits[family] = split
    base_positions, base_features = set(), set()
    for row in baseline["positions"]:
        position, features = _keys(row)
        base_positions.add(position)
        base_features.add(features)
    extra_holdout_positions, extra_holdout_features = set(), set()
    for row in additions["positions"]:
        if row["split"] != "train":
            position, features = _keys(row)
            if position in base_positions or features in base_features:
                raise ValueError("independent endgame holdout overlaps a bootstrap input")
            extra_holdout_positions.add(position)
            extra_holdout_features.add(features)
    candidates, exclusions = [], Counter()
    seen_positions, seen_features = set(), set()
    for index, row in enumerate(additions["positions"]):
        if row["split"] != "train":
            continue
        position, features = _keys(row)
        if position in base_positions or features in base_features:
            exclusions["bootstrap_input_duplicate"] += 1
            continue
        if position in extra_holdout_positions or features in extra_holdout_features:
            raise ValueError("endgame training input overlaps independent holdout")
        if position in seen_positions or features in seen_features:
            exclusions["added_training_input_duplicate"] += 1
            continue
        seen_positions.add(position)
        seen_features.add(features)
        candidates.append(index)
    def rank(*values):
        return _digest([schedule_seed, *values])
    families = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for index in candidates:
        row = additions["positions"][index]
        families[row["family"]][row["turn"]][row["game_id"]].append(index)
    queues = {}
    active_turns = {}
    for family, turns in families.items():
        queues[family] = {}
        for turn, games in turns.items():
            queues[family][turn] = deque(deque(sorted(indices, key=lambda index: rank(
                family, turn, game, additions["positions"][index]["ply"])))
                for game, indices in sorted(games.items(), key=lambda item: rank(family, turn, item[0])))
        active_turns[family] = deque(sorted(turns, key=lambda turn: rank(family, turn)))
    active = deque(sorted(queues, key=rank))
    schedule = []
    while active:
        family = active.popleft()
        turn = active_turns[family].popleft()
        games = queues[family][turn]
        game = games.popleft()
        schedule.append(game.popleft())
        if game:
            games.append(game)
        if games:
            active_turns[family].append(turn)
        if active_turns[family]:
            active.append(family)
    return schedule, {"eligible_unique_added_train": len(schedule), "exclusions": dict(exclusions),
                      "schedule_seed": schedule_seed,
                      "policy": "one row per active family per round; alternate actual turn within family when available; round-robin active games within turn; SHA256 seeded order independent of labels",
                      "ordered_samples": [{"game_id": additions["positions"][index]["game_id"],
                                           "ply": additions["positions"][index]["ply"],
                                           "family": additions["positions"][index]["family"],
                                           "turn": additions["positions"][index]["turn"]}
                                          for index in schedule]}


def _check_freeze(paths, expected):
    for key, path in paths.items():
        if TRAIN.sha256(path) != expected[key]:
            raise ValueError("learning-curve frozen input changed: " + key)


def _snapshot(source, directory, prefix):
    directory.mkdir()
    for name in source["files_sha256"]:
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((source["path"].parent / name).read_bytes())
    (directory / source["path"].name).write_bytes(source["path"].read_bytes())
    return {"manifest_sha256": source["sha256"], "files_sha256": source["files_sha256"],
            "snapshot": prefix + "/" + source["path"].name}


def _write_arm_dataset(baseline, additions, indices, output):
    output.mkdir()
    (output / "games.jsonl").write_bytes(baseline["games_raw"] + additions["games_raw"])
    (output / "positions.jsonl").write_bytes(baseline["positions_raw"] + b"".join(
        additions["positions_lines"][index] for index in indices))
    files = ["games.jsonl", "positions.jsonl"]
    manifest = {"schema": TRAIN.SCHEMA, "status": "complete", "games_path": "games.jsonl",
                "positions_path": "positions.jsonl",
                "composition": "all original bootstrap rows unchanged; exact prefix of added training schedule; no new holdout enters training or epoch selection"}
    if baseline["pgn_raw"] is not None and additions["pgn_raw"] is not None:
        # The separating newline is outside both original byte streams. Their
        # entire histories and original PGN bytes remain intact in snapshots.
        (output / "games.pgn").write_bytes(baseline["pgn_raw"] + b"\n\n" + additions["pgn_raw"])
        files.append("games.pgn")
        manifest["pgn_path"] = "games.pgn"
    manifest["files_sha256"] = {name: TRAIN.sha256(output / name) for name in files}
    TRAIN.write_json(output / "manifest.json", manifest)
    _, metadata = TRAIN.load_dataset(output / "manifest.json", model_version=2)
    raw = (output / "positions.jsonl").read_bytes().splitlines(keepends=True)
    rows = list(TRAIN.json_lines(output / "positions.jsonl"))
    for split, expected in _holdout(baseline).items():
        actual = hashlib.sha256(b"".join(line for row, line in zip(rows, raw)
                                         if row["split"] == split)).hexdigest()
        if actual != expected["raw_rows_sha256"]:
            raise ValueError("bootstrap held-out JSONL bytes changed")
    return metadata


def _validate_configuration(counts, seeds, epochs, batch_size, learning_rate, l2):
    if (not isinstance(counts, (tuple, list)) or len(counts) < 2 or counts[0] != 0
            or any(type(value) is not int or value < 0 for value in counts)
            or any(first >= second for first, second in zip(counts, counts[1:]))):
        raise ValueError("added counts must be strictly increasing integers starting at zero")
    if (not isinstance(seeds, (tuple, list)) or len(seeds) < 2 or len(set(seeds)) != len(seeds)
            or any(type(seed) is not int or seed < 0 for seed in seeds)):
        raise ValueError("at least two distinct nonnegative integer training seeds are required")
    if (type(epochs) is not int or epochs < 1 or type(batch_size) is not int or batch_size < 1
            or not math.isfinite(learning_rate) or learning_rate <= 0
            or not math.isfinite(l2) or l2 < 0):
        raise ValueError("invalid training configuration")


def run_curve(baseline_path, additions_path, output, *, counts=DEFAULT_COUNTS,
              seeds=DEFAULT_SEEDS, epochs=100, batch_size=64, learning_rate=.003,
              l2=.0001, schedule_seed=SCHEDULE_SEED, initial_model=None):
    _validate_configuration(counts, seeds, epochs, batch_size, learning_rate, l2)
    output = Path(output).resolve()
    if output.exists():
        raise FileExistsError(output)
    baseline, additions = _raw_source(baseline_path), _raw_source(additions_path)
    schedule, schedule_report = prepare_schedule(baseline, additions, schedule_seed)
    feasible = [count for count in counts if count <= len(schedule)]
    if len(feasible) < 2:
        raise ValueError("insufficient unique added training rows for the smallest declared nonzero arm")
    frozen_paths = {}
    for label, source in (("bootstrap", baseline), ("endgame", additions)):
        frozen_paths[label + "/manifest"] = source["path"]
        frozen_paths.update({label + "/" + name: source["path"].parent / name
                             for name in source["files_sha256"]})
    for name in ("endgame_learning_curve.py", "train_neural.py", "neural_model.py",
                 "classical_evaluation.py", "learning_corpus.py", "teacher_quality.py",
                 "collect_learning_data.py"):
        frozen_paths["scripts/" + name] = ROOT / "scripts" / name
    frozen_paths["reserved_openings"] = RESERVED_OPENINGS
    fixed_initial = None
    if initial_model is not None:
        initial_model = Path(initial_model).resolve()
        frozen_paths["initial_model"] = initial_model
        before = TRAIN.sha256(initial_model)
        fixed_initial = load_model(initial_model)
        if fixed_initial.version != 2 or TRAIN.sha256(initial_model) != before:
            raise ValueError("initial model must be an unchanged V2 checkpoint")
    frozen = {name: TRAIN.sha256(path) for name, path in frozen_paths.items()}
    output.mkdir(parents=True)
    report = {"schema": SCHEMA, "status": "running", "targets_added_train": list(counts),
              "feasible_added_train": feasible, "skipped_infeasible_added_train": [count for count in counts if count not in feasible],
              "schedule": schedule_report, "training_seeds": list(seeds), "model_version": 2,
              "architecture": [768, 32, 16, 1], "epochs": epochs, "batch_size": batch_size,
              "learning_rate": learning_rate, "l2": l2, "schedule_seed": schedule_seed,
              "training_budget_policy": "same epochs/passes and hyperparameters; total sample presentations and Adam updates grow with data size, so this is not a constant-compute experiment",
              "checkpoint_selection": "lowest original bootstrap validation MSE only; earliest strict improvement; epoch zero eligible",
              "candidate_policy": "largest feasible preregistered arm, first declared seed only; never choose on test, new endgame holdout, second seed, or arena results",
              "test_policy": "all tests and new endgame holdouts assessed only after each checkpoint is fixed; no tuning or selection from their scores",
              "initialization": "same warm-start weights per seed and arm" if fixed_initial is not None else "same seeded V2 random weights per seed across every arm",
              "frozen_input_sha256": frozen, "bootstrap_holdout": _holdout(baseline),
              "unused_incomplete_source_games": {"bootstrap": baseline["unused_incomplete_games"],
                                                  "endgame": additions["unused_incomplete_games"]},
              "incomplete_game_policy": "retain raw unfinished games/PGN as provenance only; no sample may refer to one, no row is synthesized, no ply cap becomes a draw",
              "endgame_holdout": _holdout(additions), "arms": [],
              "environment": {"python": sys.version.split()[0], "numpy": np.__version__,
                              "platform": platform.platform(), "threads": 1, "arithmetic": "float64 CPU"},
              "strength_status": "unmeasured; data fitting metrics are not playing-strength evidence",
              "adoption": "not attempted; this runner does not access any champion, registry, attempt counter or arena"}
    report_path = output / "report.json"
    TRAIN.write_json(report_path, report)
    try:
        (output / "sources").mkdir()
        report["sources"] = {label: _snapshot(source, output / "sources" / label, "sources/" + label)
                              for label, source in (("bootstrap", baseline), ("endgame", additions))}
        _check_freeze(frozen_paths, frozen)
        extra_batches, _ = TRAIN.load_dataset(additions["path"], allow_empty_splits=True, model_version=2)
        for count in feasible:
            arm = output / f"added-{count:06d}"
            temporary = output / (arm.name + ".partial")
            temporary.mkdir()
            indices = schedule[:count]
            metadata = _write_arm_dataset(baseline, additions, indices, temporary / "dataset")
            batches, _ = TRAIN.load_dataset(temporary / "dataset" / "manifest.json", model_version=2)
            rows = [additions["positions"][index] for index in indices]
            arm_report = {"added_train": count, "dataset": metadata,
                          "sample_presentations": epochs * len(batches["train"][0]),
                          "adam_updates": epochs * math.ceil(len(batches["train"][0]) / batch_size),
                          "added_game_count": len({row["game_id"] for row in rows}),
                          "added_family_count": len({row["family"] for row in rows}),
                          "added_family_samples": dict(sorted(Counter(row["family"] for row in rows).items())),
                          "added_turn_samples": dict(sorted(Counter(row["turn"] for row in rows).items())),
                          "schedule_prefix_sha256": _digest(schedule_report["ordered_samples"][:count]),
                          "runs": []}
            for seed in seeds:
                _check_freeze(frozen_paths, frozen)
                initial = fixed_initial.copy() if fixed_initial is not None else initialize_model(seed, version=2)
                run_dir = temporary / f"seed-{seed}"
                run_dir.mkdir()
                export_model(initial, run_dir / "initial.nn")
                model, result = TRAIN.train(batches, epochs=epochs, batch_size=batch_size,
                    seed=seed, learning_rate=learning_rate, initial_model=initial, model_version=2, l2=l2)
                result["independent_endgame"] = {
                    split: {"samples": len(extra_batches[split][0]),
                            "initial_mse": TRAIN.mse(initial, extra_batches[split]),
                            "selected_mse": TRAIN.mse(model, extra_batches[split]),
                            "classical_only_mse": TRAIN.material_mse(extra_batches[split])}
                    for split in ("validation", "test") if len(extra_batches[split][0])}
                export_model(model, run_dir / "small-v2.nn")
                _check_freeze(frozen_paths, frozen)
                run = {"seed": seed, "initial_model_sha256": TRAIN.sha256(run_dir / "initial.nn"),
                       "model_sha256": TRAIN.sha256(run_dir / "small-v2.nn"),
                       "model_path": f"{arm.name}/{run_dir.name}/small-v2.nn", **result}
                TRAIN.write_json(run_dir / "report.json", run)
                arm_report["runs"].append(run)
                print(json.dumps({"added_train": count, "seed": seed,
                                  "selected_epoch": result["selected_epoch"], "status": "complete"}), flush=True)
            TRAIN.write_json(temporary / "report.json", arm_report)
            _check_freeze(frozen_paths, frozen)
            os.replace(temporary, arm)
            report["arms"].append(arm_report)
            TRAIN.write_json(report_path, report)
        _check_freeze(frozen_paths, frozen)
        candidate = report["arms"][-1]["runs"][0]
        report.update(status="complete", candidate={"added_train": feasible[-1],
                      "seed": seeds[0], "model_path": candidate["model_path"],
                      "model_sha256": candidate["model_sha256"], "selected_epoch": candidate["selected_epoch"]})
        TRAIN.write_json(report_path, report)
        return report
    except Exception as error:
        report.pop("candidate", None)
        report.update(status="failed", error_type=type(error).__name__, error=str(error))
        TRAIN.write_json(report_path, report)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--endgame", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--add-counts", type=int, nargs="+", default=list(DEFAULT_COUNTS))
    parser.add_argument("--seeds", type=int, nargs="+", default=list(DEFAULT_SEEDS))
    parser.add_argument("--epochs", type=int, default=100)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--learning-rate", type=float, default=.003)
    parser.add_argument("--l2", type=float, default=.0001)
    parser.add_argument("--schedule-seed", type=int, default=SCHEDULE_SEED)
    parser.add_argument("--initial-model", type=Path)
    args = parser.parse_args(argv)
    try:
        run_curve(args.baseline, args.endgame, args.output, counts=args.add_counts,
                  seeds=args.seeds, epochs=args.epochs, batch_size=args.batch_size,
                  learning_rate=args.learning_rate, l2=args.l2,
                  schedule_seed=args.schedule_seed, initial_model=args.initial_model)
        return 0
    except (ValueError, OSError) as error:
        print(f"learning curve failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
