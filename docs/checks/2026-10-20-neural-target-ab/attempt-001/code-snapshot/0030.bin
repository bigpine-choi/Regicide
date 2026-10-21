"""Compare two loaded integer models with the same six evaluation workloads.

The executable preloads both models, validates full/incremental equality, then
alternates model and workload order across rounds. Loading, preflight, warmup,
checksum printing, and process startup are outside per-workload timing. Model
scores may differ: score equality is required only between neural paths using
the same model. This microbenchmark does not measure playing strength.
"""
from __future__ import annotations

import argparse
import csv
import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
WORKLOADS = (
    "classical_after", "neural_full_after", "neural_score_only",
    "neural_update_snapshot_restore", "board_play_full_undo", "board_play_incremental_undo",
)
FIELDS = (
    "round", "model_order", "model", "model_checksum", "order", "workload",
    "transitions", "iterations", "elapsed_ns", "checksum",
)


def positive(text):
    value = int(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return value


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def git(*args):
    return subprocess.run(["git", "-C", str(ROOT), *args], check=True,
                          capture_output=True, text=True, timeout=10).stdout.strip()


def source_snapshot():
    files = {Path(__file__).resolve(), ROOT / "scripts/neural_incremental_benchmark.cpp",
             ROOT / "scripts/build.ps1", ROOT / "CMakeLists.txt"}
    for directory in (ROOT / "src", ROOT / "include"):
        files.update(path for path in directory.rglob("*") if path.is_file())
    hashes = {path.relative_to(ROOT).as_posix(): sha256(path) for path in sorted(files)}
    encoded = json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {"files": hashes, "manifest_sha256": hashlib.sha256(encoded).hexdigest()}


def parse_results(stdout, iterations, repeats):
    reader = csv.DictReader(io.StringIO(stdout))
    if tuple(reader.fieldnames or ()) != FIELDS:
        raise RuntimeError("unexpected benchmark CSV columns")
    records = []
    indexed = {}
    checksums = {0: set(), 1: set()}
    for row in reader:
        record = {key: (value if key == "workload" else int(value)) for key, value in row.items()}
        model, round_number, workload = record["model"], record["round"], record["workload"]
        if model not in (0, 1) or not 0 <= round_number < repeats or workload not in WORKLOADS:
            raise RuntimeError("unexpected benchmark model, round, or workload")
        expected_index = WORKLOADS.index(workload)
        if record["model_order"] != (model if round_number % 2 == 0 else 1 - model):
            raise RuntimeError("model order did not alternate as specified")
        if record["order"] != (expected_index if round_number % 2 == 0 else 5 - expected_index):
            raise RuntimeError("workload order did not alternate as specified")
        if record["iterations"] != iterations or record["transitions"] <= 0 or record["elapsed_ns"] <= 0:
            raise RuntimeError("invalid benchmark sample size or timing")
        key = (round_number, model, workload)
        if key in indexed:
            raise RuntimeError("duplicate benchmark sample")
        record["ns_per_operation"] = record["elapsed_ns"] / iterations
        indexed[key] = record
        records.append(record)
        checksums[model].add(record["model_checksum"])
    if len(records) != repeats * 2 * len(WORKLOADS) or any(len(values) != 1 for values in checksums.values()):
        raise RuntimeError("missing samples or changing model checksum")
    if len({record["transitions"] for record in records}) != 1:
        raise RuntimeError("models used different transition suites")
    for round_number in range(repeats):
        for model in range(2):
            neural = [indexed[round_number, model, name]["checksum"] for name in WORKLOADS[1:]]
            if len(set(neural)) != 1:
                raise RuntimeError("full/incremental workload score checksums differ within a model")
        if indexed[round_number, 0, WORKLOADS[0]]["checksum"] != indexed[round_number, 1, WORKLOADS[0]]["checksum"]:
            raise RuntimeError("classical control checksum differs between models")
    for model in range(2):
        for workload in WORKLOADS:
            if len({indexed[r, model, workload]["checksum"] for r in range(repeats)}) != 1:
                raise RuntimeError("workload score changed between rounds")
    return records, indexed


def summarize(records, indexed, repeats):
    return {
        "models": {
            name: {
                "model_checksum": indexed[0, model, WORKLOADS[0]]["model_checksum"],
                "transitions": indexed[0, model, WORKLOADS[0]]["transitions"],
                "workloads": {
                    workload: {
                        "median_ns_per_operation": statistics.median(
                            indexed[r, model, workload]["ns_per_operation"] for r in range(repeats)),
                        "min_ns_per_operation": min(
                            indexed[r, model, workload]["ns_per_operation"] for r in range(repeats)),
                        "max_ns_per_operation": max(
                            indexed[r, model, workload]["ns_per_operation"] for r in range(repeats)),
                    } for workload in WORKLOADS
                },
                "median_paired_speedup_full_over_incremental": statistics.median(
                    indexed[r, model, "neural_full_after"]["elapsed_ns"] /
                    indexed[r, model, "neural_update_snapshot_restore"]["elapsed_ns"] for r in range(repeats)),
                "median_paired_speedup_board_full_over_incremental": statistics.median(
                    indexed[r, model, "board_play_full_undo"]["elapsed_ns"] /
                    indexed[r, model, "board_play_incremental_undo"]["elapsed_ns"] for r in range(repeats)),
            } for model, name in enumerate(("baseline", "qat"))
        },
        "median_paired_qat_over_baseline_cost_ratio": {
            workload: statistics.median(indexed[r, 1, workload]["elapsed_ns"] /
                                        indexed[r, 0, workload]["elapsed_ns"] for r in range(repeats))
            for workload in WORKLOADS
        },
        "samples": len(records),
        "all_within_model_neural_checksums_equal": True,
    }


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("benchmark", type=Path)
    parser.add_argument("baseline_model", type=Path)
    parser.add_argument("qat_model", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--iterations", type=positive, default=500000)
    parser.add_argument("--repeats", type=positive, default=8)
    parser.add_argument("--timeout", type=positive, default=180, help="seconds for the complete benchmark")
    args = parser.parse_args(argv)
    for name in ("benchmark", "baseline_model", "qat_model", "output"):
        setattr(args, name, getattr(args, name).expanduser().resolve())
    for name in ("benchmark", "baseline_model", "qat_model"):
        if not getattr(args, name).is_file():
            parser.error(f"{name} is not a file")
    if args.output.exists():
        parser.error("output already exists")
    if args.output in {args.benchmark, args.baseline_model, args.qat_model}:
        parser.error("output must not replace an input")
    return args


def run(args):
    command = [str(args.benchmark), str(args.iterations), str(args.repeats),
               "--model", str(args.baseline_model), "--compare-model", str(args.qat_model)]
    paths = {"benchmark": args.benchmark, "baseline_model": args.baseline_model, "qat_model": args.qat_model}
    document = {
        "schema_version": 1,
        "purpose": "same-binary evaluation cost; no strength inference",
        "started_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "configuration": {
            "iterations": args.iterations, "repeats": args.repeats,
            "model_and_workload_order_alternates_each_round": True,
            "warmup_operations_per_workload": min(args.iterations, 1000),
            "models_preloaded_outside_timing": True,
            "score_equality_checked_within_each_model": True,
        },
        "environment": {
            "platform": platform.platform(), "machine": platform.machine(), "processor": platform.processor(),
            "logical_cpus": os.cpu_count(), "processor_identifier": os.environ.get("PROCESSOR_IDENTIFIER"),
            "python": sys.version, "python_executable": sys.executable,
            "external_load_monitoring": "not sampled; machine was not isolated",
        },
        "source": {"head": git("rev-parse", "HEAD"), "status": git("status", "--porcelain"),
                   "snapshot_before": source_snapshot()},
        "inputs": {name: {"path": str(path), "sha256_before": sha256(path)} for name, path in paths.items()},
        "execution": {"command": command}, "records": [], "summary": {}, "error": None, "exit_code": 0,
    }
    started = time.perf_counter_ns()
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=args.timeout, cwd=ROOT)
        document["execution"].update(returncode=completed.returncode, stdout=completed.stdout,
                                     stderr=completed.stderr, wall_time_ns=time.perf_counter_ns() - started)
        if completed.returncode:
            raise RuntimeError(f"benchmark failed with exit code {completed.returncode}")
        records, indexed = parse_results(completed.stdout, args.iterations, args.repeats)
        document["records"] = records
        document["summary"] = summarize(records, indexed, args.repeats)
    except Exception as error:
        document["error"] = f"{type(error).__name__}: {error}"
        document["exit_code"] = 1
        if isinstance(error, subprocess.TimeoutExpired):
            document["execution"].update(
                wall_time_ns=time.perf_counter_ns() - started,
                stdout=error.stdout.decode(errors="replace") if isinstance(error.stdout, bytes) else error.stdout,
                stderr=error.stderr.decode(errors="replace") if isinstance(error.stderr, bytes) else error.stderr)
    finally:
        document["finished_utc"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        try:
            document["source"]["snapshot_after"] = source_snapshot()
            for name, path in paths.items():
                document["inputs"][name]["sha256_after"] = sha256(path)
            unchanged = document["source"]["snapshot_before"] == document["source"]["snapshot_after"] and all(
                item["sha256_before"] == item["sha256_after"] for item in document["inputs"].values())
            document["inputs_unchanged"] = unchanged
            if not unchanged:
                raise RuntimeError("sources, executable, or model files changed during measurement")
        except Exception as error:
            document["error"] = f"input recheck failed: {type(error).__name__}: {error}"
            document["exit_code"] = 1
    return document


def main(argv=None):
    args = arguments(argv)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:
        document = run(args)
        json.dump(document, output, indent=2)
        output.write("\n")
    print(json.dumps(document["summary"], indent=2))
    if document["error"]:
        print(document["error"], file=sys.stderr)
    return document["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
