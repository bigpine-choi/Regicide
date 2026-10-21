"""Same-binary, fixed-depth neural full-refresh versus incremental cost check.

Uses only Python's standard library. Every sample starts a fresh console process
and compares every emitted field except time_ms. Wall latency includes process
startup and console I/O; engine time is retained independently. No matches,
ratings, or strength conclusions are produced. All opening positions are run in
each repeat, in seeded shuffled order; the two evaluation modes alternate order.
"""

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import random
import re
import statistics
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
ROLES = ("full_refresh", "incremental")
REQUIRED_FIELDS = {
    "bestmove", "depth", "nodes", "qnodes", "seldepth", "tt_probes", "tt_hits",
    "tt_cutoffs", "tt_stores", "tt_allocations", "tt_reuses", "tt_clears",
    "tt_bytes", "see_calls", "stopped", "pv", "time_ms",
}


def positive(text):
    value = int(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return value


def nonnegative(text):
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError("must be nonnegative")
    return value


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def git(*arguments):
    return subprocess.run(["git", "-C", str(ROOT), *arguments], check=True,
                          capture_output=True, text=True, timeout=10).stdout.strip()


def source_snapshot():
    files = {ROOT / "CMakeLists.txt", ROOT / "scripts/build.ps1", Path(__file__).resolve(),
             ROOT / "scripts/neural_incremental_benchmark.cpp"}
    for directory in (ROOT / "src", ROOT / "include"):
        files.update(path for path in directory.rglob("*") if path.is_file())
    hashes = {str(path.relative_to(ROOT)).replace("\\", "/"): sha256(path)
              for path in sorted(files) if path.is_file()}
    serialized = json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {"files": hashes, "manifest_sha256": hashlib.sha256(serialized).hexdigest()}


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("engine", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--positions", type=Path, default=ROOT / "tests/data/profile_positions.json")
    parser.add_argument("--depth", type=positive, default=3)
    parser.add_argument("--repeats", type=positive, default=5)
    parser.add_argument("--warmup", type=nonnegative, default=1,
                        help="samples per role on the first position, outside paired summaries")
    parser.add_argument("--seed", type=int, default=20261019)
    parser.add_argument("--timeout", type=positive, default=60, help="per-process timeout, seconds")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    try:
        for name in ("engine", "positions", "output"):
            setattr(args, name, getattr(args, name).expanduser().resolve())
        if not args.engine.is_file():
            raise ValueError("engine executable does not exist")
        if args.output in {args.engine, args.positions}:
            raise ValueError("output must not replace an input")
        if args.output.exists() and not args.overwrite:
            raise ValueError("output exists; use --overwrite")
        args.position_document = json.loads(args.positions.read_text(encoding="utf-8-sig"))
        args.selected_positions = args.position_document["positions"]
        if not isinstance(args.selected_positions, list) or not args.selected_positions:
            raise ValueError("positions must be a nonempty list")
        names = set()
        for position in args.selected_positions:
            name = position["name"]
            if not isinstance(name, str) or not name or name in names:
                raise ValueError("position names must be unique, nonempty strings")
            names.add(name)
            if "fen" in position and not isinstance(position["fen"], str):
                raise ValueError("fen must be a string")
            moves = position.get("moves", [])
            if not isinstance(moves, list) or any(not isinstance(move, str) or
                    not re.fullmatch(r"[a-h][1-8][a-h][1-8][qrbn]?", move) for move in moves):
                raise ValueError("moves must be a list of coordinate move strings")
    except (KeyError, TypeError, ValueError, OSError) as error:
        parser.error(str(error))
    return args


def parse_output(output):
    fields = {}
    for line in output.splitlines():
        if not line.strip():
            continue
        key, separator, value = line.partition(":")
        if not separator or key in fields or not re.fullmatch(r"[a-z_]+", key):
            raise RuntimeError(f"unexpected/duplicate console field: {line!r}")
        value = value.strip()
        if key == "pv":
            fields[key] = value.split()
        elif re.fullmatch(r"-?\d+", value):
            fields[key] = int(value)
        else:
            fields[key] = value
    missing = REQUIRED_FIELDS - fields.keys()
    if missing or ("score_cp" in fields) == ("score_mate" in fields):
        raise RuntimeError(f"missing or ambiguous search fields: {sorted(missing)}")
    if any(not isinstance(fields[key], int) for key in REQUIRED_FIELDS - {"bestmove", "pv"}):
        raise RuntimeError("search emitted a noninteger counter")
    score_field = "score_cp" if "score_cp" in fields else "score_mate"
    if not isinstance(fields[score_field], int):
        raise RuntimeError("search emitted a noninteger score")
    if not isinstance(fields["bestmove"], str) or not re.fullmatch(
            r"[a-h][1-8][a-h][1-8][qrbn]?", fields["bestmove"]):
        raise RuntimeError("search did not emit a coordinate bestmove")
    if not fields["pv"] or fields["pv"][0] != fields["bestmove"]:
        raise RuntimeError("bestmove and principal variation differ")
    if any(not re.fullmatch(r"[a-h][1-8][a-h][1-8][qrbn]?", move) for move in fields["pv"]):
        raise RuntimeError("principal variation contains a malformed coordinate move")
    return fields


def sample(args, role, position):
    command = [str(args.engine), "search", str(args.depth), "--nnue"]
    if role == "full_refresh":
        command.append("--nnue-full-refresh")
    if "fen" in position:
        command += ["--fen", position["fen"]]
    if position.get("moves"):
        command += ["--play", *position["moves"]]
    record = {"command": command, "wall_time_ns": None, "returncode": None,
              "stdout": None, "stderr": None, "fields": None, "deterministic_fields": None}
    started = time.perf_counter_ns()
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=args.timeout,
                                   cwd=ROOT)
        record.update(wall_time_ns=time.perf_counter_ns() - started,
                      returncode=completed.returncode, stdout=completed.stdout, stderr=completed.stderr)
        if completed.returncode != 0:
            raise RuntimeError(f"engine exited {completed.returncode}")
        fields = parse_output(completed.stdout)
        record["fields"] = fields
        if fields["depth"] != args.depth or fields["stopped"] or fields["nodes"] <= 0:
            raise RuntimeError("search did not finish the requested fixed depth")
        if fields["time_ms"] < 0:
            raise RuntimeError("engine reported negative time")
        record["engine_time_ms"] = fields["time_ms"]
        record["deterministic_fields"] = {key: value for key, value in fields.items() if key != "time_ms"}
    except subprocess.TimeoutExpired as error:
        record.update(wall_time_ns=time.perf_counter_ns() - started, error=f"timeout after {args.timeout}s",
                      stdout=error.stdout.decode(errors="replace") if isinstance(error.stdout, bytes) else error.stdout,
                      stderr=error.stderr.decode(errors="replace") if isinstance(error.stderr, bytes) else error.stderr)
    except Exception as error:
        record["error"] = f"{type(error).__name__}: {error}"
    return record


def median_or_none(values):
    return statistics.median(values) if values else None


def summarize(records):
    complete = [record for record in records if record.get("deterministic_equal") is not None]
    engine_ratios = [record["full_refresh"]["engine_time_ms"] / record["incremental"]["engine_time_ms"]
                     for record in complete if record["incremental"]["engine_time_ms"] > 0
                     and record["full_refresh"]["engine_time_ms"] > 0]
    summary = {
        "completed_pairs": len(complete),
        "deterministic_mismatches": sum(not record["deterministic_equal"] for record in complete),
        "median_paired_wall_speedup_full_over_incremental": median_or_none([
            record["full_refresh"]["wall_time_ns"] / record["incremental"]["wall_time_ns"]
            for record in complete]),
        "median_paired_engine_speedup_full_over_incremental": median_or_none(engine_ratios),
        "engine_time_ratio_pairs_excluding_zero_or_missing": len(engine_ratios),
        "per_position": [],
    }
    for name in dict.fromkeys(record["position"] for record in complete):
        selected = [record for record in complete if record["position"] == name]
        summary["per_position"].append({"position": name, "pairs": len(selected),
            **{role: {"median_wall_time_ns": median_or_none([record[role]["wall_time_ns"] for record in selected]),
                      "median_engine_time_ms": median_or_none([record[role]["engine_time_ms"] for record in selected])}
               for role in ROLES}})
    return summary


def run(args, output):
    document = {
        "schema_version": 1, "purpose": "exact search equivalence and evaluation cost; no strength inference",
        "started_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(), "finished_utc": None,
        "configuration": {"depth": args.depth, "repeats": args.repeats, "warmup_per_role": args.warmup,
                          "seed": args.seed, "timeout_seconds": args.timeout,
                          "wall_latency_includes_process_startup": True,
                          "comparison_excludes_only": ["time_ms"], "sequential_processes": True},
        "environment": {"platform": platform.platform(), "machine": platform.machine(),
                        "processor": platform.processor(), "logical_cpus": os.cpu_count(),
                        "processor_identifier": os.environ.get("PROCESSOR_IDENTIFIER"),
                        "python": sys.version, "python_executable": sys.executable,
                        "clock": vars(time.get_clock_info("perf_counter")),
                        "external_load_monitoring": "not sampled; machine was not isolated"},
        "source": {"head": git("rev-parse", "HEAD"), "status": git("status", "--porcelain"),
                   "snapshot_before": source_snapshot(), "snapshot_after": None},
        "engine": {"path": str(args.engine), "sha256_before": sha256(args.engine), "sha256_after": None},
        "positions": {"path": str(args.positions), "sha256_before": sha256(args.positions),
                      "sha256_after": None, "document": args.position_document},
        "warmup_records": [], "records": [], "summary": {}, "error": None, "exit_code": None,
    }

    def save():
        document["summary"] = summarize(document["records"])
        output.seek(0)
        json.dump(document, output, ensure_ascii=False, indent=2)
        output.write("\n")
        output.truncate()
        output.flush()

    def checked_sample(role, position, destination):
        result = sample(args, role, position)
        destination[role] = result
        save()
        if result.get("error"):
            raise RuntimeError(f"{role} {position['name']}: {result['error']}")

    try:
        save()
        for number in range(args.warmup):
            for role in ROLES:
                warmup = {"warmup_index": number + 1, "position": args.selected_positions[0]["name"]}
                document["warmup_records"].append(warmup)
                checked_sample(role, args.selected_positions[0], warmup)
        randomizer = random.Random(args.seed)
        for repeat in range(args.repeats):
            indices = list(range(len(args.selected_positions)))
            randomizer.shuffle(indices)
            for index in indices:
                position = args.selected_positions[index]
                order = list(ROLES if (repeat + index) % 2 == 0 else reversed(ROLES))
                record = {"repeat": repeat + 1, "position_index": index, "position": position["name"],
                          "order": order, "deterministic_equal": None, "differences": None}
                document["records"].append(record)
                for role in order:
                    checked_sample(role, position, record)
                full = record["full_refresh"]["deterministic_fields"]
                incremental = record["incremental"]["deterministic_fields"]
                record["differences"] = [key for key in sorted(full.keys() | incremental.keys())
                                         if full.get(key) != incremental.get(key)]
                record["deterministic_equal"] = not record["differences"]
                save()
                print(f"pair {len(document['records'])}/{args.repeats * len(args.selected_positions)}: "
                      f"{position['name']} deterministic_equal={record['deterministic_equal']}", flush=True)
        document["exit_code"] = 1 if document["summary"]["deterministic_mismatches"] else 0
    except BaseException as error:
        document["error"] = f"{type(error).__name__}: {error}"
        document["exit_code"] = 130 if isinstance(error, KeyboardInterrupt) else 2
        print(document["error"], file=sys.stderr, flush=True)
    finally:
        try:
            document["source"]["snapshot_after"] = source_snapshot()
            document["engine"]["sha256_after"] = sha256(args.engine)
            document["positions"]["sha256_after"] = sha256(args.positions)
            document["inputs_unchanged"] = (
                document["source"]["snapshot_before"] == document["source"]["snapshot_after"] and
                document["engine"]["sha256_before"] == document["engine"]["sha256_after"] and
                document["positions"]["sha256_before"] == document["positions"]["sha256_after"])
            if not document["inputs_unchanged"]:
                document["error"] = "source, executable, or opening suite changed during measurement"
                document["exit_code"] = 2
        except Exception as error:
            document["error"] = f"input recheck failed: {type(error).__name__}: {error}"
            document["exit_code"] = 2
        document["finished_utc"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        save()
    return document


def main(argv=None):
    args = arguments(argv)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w+" if args.overwrite else "x+", encoding="utf-8") as output:
        document = run(args, output)
    print(json.dumps(document["summary"], ensure_ascii=False, indent=2))
    return document["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
