"""Read-only independent checks of actual frozen endgame learning-curve evidence."""
import argparse
import ast
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import subprocess

for _name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "VECLIB_MAXIMUM_THREADS",
              "NUMEXPR_NUM_THREADS", "BLIS_NUM_THREADS"):
    os.environ[_name] = "1"

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import train_neural  # set one-thread arithmetic before NumPy
import numpy as np
import chess
from classical_evaluation import classical_cp_stm
from neural_model import load_model


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def rows(path):
    raw = Path(path).read_bytes().splitlines(keepends=True)
    return [json.loads(line) for line in raw], raw


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def independent_mse(model, samples):
    x = np.zeros((len(samples), 768), dtype=np.float64)
    classical, targets = [], []
    for index, row in enumerate(samples):
        board = chess.Board(row["initial_fen"])
        for move in row["history"]:
            board.push_uci(move)
        assert board.fen(en_passant="fen") == row["fen"]
        for square, piece in board.piece_map().items():
            channel = piece.piece_type - 1 + (0 if piece.color == board.turn else 6)
            oriented = square if board.turn else square ^ 56
            x[index, channel * 64 + oriented] = 1
        classical.append(classical_cp_stm(board))
        targets.append(math.tanh(row["teacher_cp_stm"] / 600))
    first = np.maximum(0, x @ model.w1.T + model.b1)
    second = np.maximum(0, first @ model.w2.T + model.b2)
    residual = second @ model.w3 + model.b3[0]
    predicted = np.tanh((np.array(classical) + 100 * np.tanh(residual)) / 600)
    error = predicted - np.array(targets)
    return float(np.mean(error * error))


def audit(run, output):
    run = run.resolve()
    curve_dir = run / "curve"
    curve = read(curve_dir / "report.json")
    cycle = read(run / "cycle.json")
    assert curve["status"] == "complete"
    assert curve["targets_added_train"] == [0, 128, 512, 1024]
    assert curve["training_seeds"] == [2026101901, 2026101902]
    assert (curve["epochs"], curve["batch_size"], curve["learning_rate"], curve["l2"]) == (100, 64, .003, .0001)
    assert curve["architecture"] == [768, 32, 16, 1] and curve["model_version"] == 2
    snapshots = {}
    for label, reference in curve["sources"].items():
        path = curve_dir / reference["snapshot"]
        assert sha(path) == reference["manifest_sha256"]
        manifest = read(path)
        for relative, expected in reference["files_sha256"].items():
            assert sha(path.parent / relative) == expected
        samples, raw = rows(path.parent / manifest["positions_path"])
        snapshots[label] = path, manifest, samples, raw
    base_path, base_manifest, base, base_raw = snapshots["bootstrap"]
    extra_path, extra_manifest, extra, extra_raw = snapshots["endgame"]
    assert Counter(row["split"] for row in base) == {"train": 1071, "validation": 96, "test": 125}
    sample_lookup = {(row["game_id"], row["ply"]): (row, raw) for row, raw in zip(extra, extra_raw)}
    assert len(sample_lookup) == len(extra)
    schedule = curve["schedule"]["ordered_samples"]
    assert len({(row["game_id"], row["ply"]) for row in schedule}) == len(schedule)
    for row in schedule:
        original, _ = sample_lookup[row["game_id"], row["ply"]]
        assert original["split"] == "train"
        assert all(original[key] == row[key] for key in ("game_id", "ply", "family", "turn"))
    assert curve["feasible_added_train"] == [count for count in curve["targets_added_train"] if count <= len(schedule)]
    results, initial_hashes, assertions = [], {seed: set() for seed in curve["training_seeds"]}, 0
    previous = set()
    for arm in curve["arms"]:
        count = arm["added_train"]
        arm_dir = curve_dir / f"added-{count:06d}"
        manifest = read(arm_dir / "dataset/manifest.json")
        selected = [sample_lookup[item["game_id"], item["ply"]] for item in schedule[:count]]
        expected_raw = b"".join(base_raw) + b"".join(raw for _, raw in selected)
        assert (arm_dir / "dataset/positions.jsonl").read_bytes() == expected_raw
        current = {(row["game_id"], row["ply"]) for row, _ in selected}
        assert previous <= current
        previous = current
        assert arm["schedule_prefix_sha256"] == digest(schedule[:count])
        assert arm["sample_presentations"] == (1071 + count) * 100
        assert arm["adam_updates"] == math.ceil((1071 + count) / 64) * 100
        for field in ("games_path", "pgn_path"):
            extra_separator = b"\n\n" if field == "pgn_path" else b""
            assert (arm_dir / "dataset" / manifest[field]).read_bytes() == (
                (base_path.parent / base_manifest[field]).read_bytes() + extra_separator +
                (extra_path.parent / extra_manifest[field]).read_bytes())
        assessment = {split: [row for row in base if row["split"] == split] for split in ("validation", "test")}
        new_assessment = {split: [row for row in extra if row["split"] == split] for split in ("validation", "test")}
        for item in arm["runs"]:
            seed = item["seed"]
            initial = arm_dir / f"seed-{seed}/initial.nn"
            model_path = curve_dir / item["model_path"]
            assert sha(initial) == item["initial_model_sha256"]
            assert sha(model_path) == item["model_sha256"]
            initial_hashes[seed].add(sha(initial))
            history = item["history"]
            assert [row["epoch"] for row in history] == list(range(101))
            expected_epoch = min(range(len(history)), key=lambda index: history[index]["validation_mse"])
            assert item["selected_epoch"] == expected_epoch
            assert history[expected_epoch]["validation_mse"] == item["metrics"]["validation"]["selected_mse"]
            model = load_model(model_path)
            for split, samples in assessment.items():
                actual = independent_mse(model, samples)
                assert abs(actual - item["metrics"][split]["selected_mse"]) < 1e-12
                assertions += len(samples)
            for split, samples in new_assessment.items():
                if samples:
                    actual = independent_mse(model, samples)
                    assert abs(actual - item["independent_endgame"][split]["selected_mse"]) < 1e-12
                    assertions += len(samples)
            results.append({"added_train": count, "seed": seed, "selected_epoch": expected_epoch,
                "bootstrap_validation_mse": item["metrics"]["validation"]["selected_mse"],
                "bootstrap_test_mse": item["metrics"]["test"]["selected_mse"],
                "endgame_validation_mse": item["independent_endgame"].get("validation", {}).get("selected_mse"),
                "endgame_test_mse": item["independent_endgame"].get("test", {}).get("selected_mse")})
    assert all(len(value) == 1 for value in initial_hashes.values())
    assert len({next(iter(value)) for value in initial_hashes.values()}) == len(initial_hashes)
    candidate = curve["candidate"]
    largest = curve["arms"][-1]
    expected_run = largest["runs"][0]
    assert candidate == {"added_train": largest["added_train"], "seed": curve["training_seeds"][0],
        "model_path": expected_run["model_path"], "model_sha256": expected_run["model_sha256"],
        "selected_epoch": expected_run["selected_epoch"]}
    if "candidate" in cycle:
        assert cycle["candidate"] == {**candidate, "model_path": "curve/" + candidate["model_path"]}
    control = read(run / "compute-control/report.json")
    assert control["target_updates"] == largest["adam_updates"]
    assert control["actual_updates"] == control["epochs"] * 17
    assert abs(control["actual_updates"] - control["target_updates"]) <= 8.5
    for entry in control["runs"]:
        path = run / "compute-control" / f"seed-{entry['seed']}.nn"
        assert sha(path) == entry["model_sha256"]
        base_run = next(item for item in curve["arms"][0]["runs"] if item["seed"] == entry["seed"])
        metrics = entry["metrics"]
        assert metrics["history"][:101] == base_run["history"]
        assert [row["epoch"] for row in metrics["history"]] == list(range(195))
        expected_epoch = min(range(len(metrics["history"])), key=lambda index: metrics["history"][index]["validation_mse"])
        assert metrics["selected_epoch"] == expected_epoch
        model = load_model(path)
        for split, samples in assessment.items():
            assert abs(independent_mse(model, samples) - metrics["metrics"][split]["selected_mse"]) < 1e-12
            assertions += len(samples)
        for split, samples in new_assessment.items():
            if samples:
                assert abs(independent_mse(model, samples) - entry["independent_endgame"][split]["selected_mse"]) < 1e-12
                assertions += len(samples)
    state = run.parent.parent
    catalog = read(state / "catalog.json")
    assert catalog["attempts_started"] == 6 and catalog["runs"][-1] == run.relative_to(state).as_posix()
    assert cycle["attempt_index"] == 6
    assert candidate["added_train"] == 1024 and candidate["seed"] == 2026101901
    assert cycle["checkout"]["commit"] == "fee60f4e23155a0c4e2b15f83a922411d9e62b3a"
    for relative, expected in cycle["implementation_sha256"].items():
        assert sha(ROOT / relative) == expected
    for relative, expected in cycle["study_artifact_sha256"].items():
        assert sha(run / relative) == expected
    protection = read(ROOT / "build/endgame-learning-scale/protection-before.json")
    protected_checks = 0
    for relative, expected in protection["protected"].items():
        if relative == "scripts/learning_arena.py":
            # The optional certain-rejection path was added; the complete
            # acceptance/replay function bodies remain byte-for-byte identical.
            def function_body(source, name):
                tree = ast.parse(source)
                node = next(item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == name)
                return "\n".join(source.splitlines()[node.lineno-1:node.end_lineno])
            old = subprocess.check_output(["git", "-C", str(ROOT), "show",
                protection["baseline_commit"] + ":scripts/learning_arena.py"]).decode("utf-8")
            current = (ROOT / relative).read_text(encoding="utf-8")
            for name in ("promotion_decision", "verify_match_records"):
                assert function_body(old, name) == function_body(current, name)
                protected_checks += 1
            continue
        assert sha(ROOT / relative) == expected
        protected_checks += 1
    for relative, expected in protection["states"]["research"].items():
        if relative in {"catalog.json", "latest.json", "cycle.lock"}:
            continue
        assert sha(state / relative) == expected
        protected_checks += 1
    failed = read(ROOT / "build/endgame-learning-scale/attempt5-failed-snapshot.json")
    failed_dir = state / "runs" / failed["run"]
    actual_failed = {path.relative_to(failed_dir).as_posix(): sha(path) for path in failed_dir.rglob("*") if path.is_file()}
    assert actual_failed == failed["files"]
    for relative, expected in cycle["data_reuse"]["files_sha256"].items():
        assert sha(failed_dir / relative) == expected
        assert sha(run / relative) == expected
    assert cycle["data_reuse"]["source_cycle_sha256"] == sha(failed_dir / "cycle.json")
    report = {"status": "complete", "source_commit": cycle["checkout"]["commit"],
        "attempt_index": 6, "candidate": candidate, "arms": results,
        "failed_attempt5_files_preserved": len(actual_failed),
        "reused_raw_teacher_source_files_verified": len(cycle["data_reuse"]["files_sha256"]),
        "arithmetic_threads": {name: os.environ[name] for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS")},
        "independently_recomputed_assessment_rows": assertions,
        "protected_hash_checks": protected_checks,
        "same_seed_initialization": {str(seed): next(iter(hashes)) for seed, hashes in initial_hashes.items()},
        "nested_added_counts": curve["feasible_added_train"], "compute_control": control,
        "checks": ["raw bootstrap/heldout/games/PGN bytes unchanged", "exact nested scheduled train prefixes",
            "actual model hashes", "same seed identical initial bytes across all arms", "bootstrap-validation-only earliest-minimum epoch",
            "independent arithmetic of original and endgame holdout MSE", "largest feasible arm first seed fixed nominee",
            "globally increasing attempt6 and failed attempt5 fully preserved", "unchanged raw teacher/data copies from failed5",
            "protected engine/rule/acceptance-formula/registry/historical-state hashes unchanged"],
        "scope": "read-only artifact/metric audit; no native engine searches, training, model export or adoption"}
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("status", "attempt_index", "nested_added_counts",
        "independently_recomputed_assessment_rows", "protected_hash_checks")}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("run", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    audit(args.run, args.output)
