"""Native before/after/restart equivalence on three predeclared practice roots.

This checks research persistence and reproducibility, not playing strength.
All three paths use one binary, the same subset curriculum and fixed node cap.
The active analysis memory and runtime installation are never written.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import chess
import train_gambit_operations as current
from gambit_research_session import atomic_write
from train_gambit_policy import RuntimeTeacher

BASE_COMMIT = "ac89101efb1409955038739c5bbd048b52f895f5"
ACTIVE_MEMORY_SHA256 = "c00cef2484dac5e90ae4eaa851f5f1cb82850d51881a2be5eacfd4ec66babcfa"
ACTIVE_BINARY_SHA256 = "5011da4c5e6ef01c452bd7125e4e22b372d2c9cfb858ed8759057325acb0e6ba"
EXCLUDED_SEMANTIC_FIELDS = {"raw_log", "wall_seconds", "engine_time_ms", "uci_begin_seq", "uci_end_seq"}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def semantic(value):
    if isinstance(value, dict):
        return {key: semantic(item) for key, item in value.items() if key not in EXCLUDED_SEMANTIC_FIELDS}
    if isinstance(value, list):
        return [semantic(item) for item in value]
    return value


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_legacy(output):
    source = subprocess.run(["git", "show", BASE_COMMIT + ":scripts/train_gambit_operations.py"],
                            cwd=ROOT, check=True, capture_output=True).stdout
    path = output / "legacy_training.py"
    path.write_bytes(source)
    spec = importlib.util.spec_from_file_location("gambit_research_legacy_training", path)
    legacy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(legacy)
    # The source is byte-for-byte original. Relocation must not redirect data
    # lookups, and the provenance manifest must name the actual legacy bytes.
    legacy.ROOT = ROOT
    original_hashes = legacy.source_hashes
    def legacy_hashes():
        values = original_hashes()
        values["scripts/train_gambit_operations.py"] = hashlib.sha256(source).hexdigest()
        return values
    legacy.source_hashes = legacy_hashes
    return legacy, path


class ObservedTeacher(RuntimeTeacher):
    instances = []
    calls = []
    phase = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.validation_phase = type(self).phase
        self.validation_pid = None
        type(self).instances.append(self)

    def __enter__(self):
        result = super().__enter__()
        self.validation_pid = self._engine.transport.get_pid()
        return result

    def query(self, board, query_id):
        row = {"phase": self.validation_phase, "query_id": query_id,
               "fen": board.fen(en_passant="fen"), "history_uci": [move.uci() for move in board.move_stack],
               "requested_nodes": self.nodes, "completed": False}
        type(self).calls.append(row)
        result = super().query(board, query_id)
        row.update(completed=result["completed"], nodes=result["nodes"], depth=result["depth"],
                   bestmove_uci=result["bestmove_uci"], score=result["score"], pv_uci=result["pv_uci"])
        return result


def validate(binary, output, *, wall_seconds=180):
    binary, output = Path(binary).resolve(strict=True), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    memory = current.memory.DEFAULT_MEMORY
    memory_before = sha(memory)
    if memory_before != ACTIVE_MEMORY_SHA256:
        raise ValueError("Active memory differs from the declared validation baseline")
    legacy, legacy_path = load_legacy(output)
    data = read(current.CURRICULUM)
    cases = [copy.deepcopy(row) for row in legacy.select_cases(data, 1, 20261105)
             if row["family_id"] in {"kings", "benko", "from"}]
    if len(cases) != 3 or {row["family_id"] for row in cases} != {"kings", "benko", "from"}:
        raise ValueError("Legacy selection did not produce the declared three families")
    ids = [row["case_id"] for row in cases]
    subset = copy.deepcopy(data)
    subset["cases"] = cases
    subset.pop("curriculum_sha256", None)
    subset["counts"] = {"cases": len(cases), "families": 3, "scope": "validation input subset only"}
    subset["validation_subset"] = {"source_full_curriculum_sha256": sha(current.CURRICULUM),
        "selection": "legacy select_cases(full curriculum, per_family=1, seed=20261105), then kings/benko/from",
        "scope": "three-root implementation equivalence; not complete curriculum training or strength evaluation"}
    curriculum = output / "validation-curriculum.json"
    atomic_write(curriculum, subset)
    heldout = sorted({row["family_id"] for row in cases if row["split"] == "holdout"})
    if heldout != ["benko", "from"] or sorted(subset["split_policy"]["holdout_families"]) != heldout:
        raise ValueError("Validation subset must preserve both whole-family holdout declarations")
    source_hashes = current.source_hashes()
    source_hashes["tests/gambit_research_validation.py"] = sha(__file__)
    for name in source_hashes:
        destination = output / "execution-sources" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((ROOT / name).read_bytes())
    predeclared = {"schema": "regicide.gambit-research-native-validation.v1",
        "created_utc": datetime.now(timezone.utc).isoformat(), "baseline_commit": BASE_COMMIT,
        "legacy_source_sha256": sha(legacy_path), "source_sha256": source_hashes,
        "binary": str(binary), "binary_sha256": sha(binary), "active_memory_sha256_before": memory_before,
        "curriculum_sha256": sha(curriculum), "full_curriculum_sha256": sha(current.CURRICULUM),
        "case_ids": ids, "cases": cases, "heldout_family_ids": heldout,
        "settings": {"nodes": 100000, "owner_turns": 2, "candidates": 2, "per_family": 1,
                     "seed": 20261105, "wall_seconds": wall_seconds, "pause_after_new_queries": 2},
        "expected_queries_per_complete_path_without_terminal_children": 18,
        "semantic_exclusions": sorted(EXCLUDED_SEMANTIC_FIELDS),
        "comparison_artifacts": ["records.json", "practice.json", "queries.json"],
        "execution_order": ["legacy_continuous", "new_continuous", "new_paused", "new_resumed"],
        "legacy_relocation_adjustments": ["ROOT points to original checkout", "source_hashes reports extracted legacy module bytes"],
        "hardware": {"platform": platform.platform(), "processor": platform.processor(), "logical_cpus": os.cpu_count()},
        "software": {"python": platform.python_version(), "python_chess": chess.__version__},
        "scope": "Same fixed-node native queries; persistence equivalence, no Elo or strength claim"}
    atomic_write(output / "predeclared.json", predeclared)
    report = {"predeclared_sha256": sha(output / "predeclared.json"), "phases": [], "checks": {}, "error": None}
    ObservedTeacher.instances, ObservedTeacher.calls = [], []
    legacy.RuntimeTeacher = current.RuntimeTeacher = ObservedTeacher
    arguments = dict(curriculum=curriculum, nodes=100000, owner_turns=2, candidates=2,
                     per_family=1, seed=20261105, wall_seconds=wall_seconds)
    def run_phase(label, module, directory, **extra):
        ObservedTeacher.phase = label
        started, call_start = time.monotonic(), len(ObservedTeacher.calls)
        with (output / (label + ".stdout.log")).open("x", encoding="utf-8") as log, contextlib.redirect_stdout(log):
            answer = module.run(binary, output / directory, **arguments, **extra)
        row = {"phase": label, "output": directory, "wall_seconds": time.monotonic() - started,
               "native_calls": len(ObservedTeacher.calls) - call_start,
               "research_status": answer.get("research_status", "legacy_complete"),
               "new_queries": answer.get("new_queries"), "reused_queries": answer.get("reused_queries"),
               "reason": answer.get("reason")}
        report["phases"].append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
        return answer
    try:
        run_phase("legacy_continuous", legacy, "legacy-continuous")
        run_phase("new_continuous", current, "new-continuous", case_ids=ids)
        paused = run_phase("new_paused", current, "new-resumed", case_ids=ids, max_new_queries=2)
        if paused.get("research_status") != "paused" or paused.get("reason") != "query_budget":
            raise ValueError("The predeclared query boundary did not pause")
        resumed = run_phase("new_resumed", current, "new-resumed", case_ids=ids, resume=True)
        checks = report["checks"]
        report["semantic_comparisons"] = {}
        for filename in predeclared["comparison_artifacts"]:
            values = {name: semantic(read(output / name / filename))
                      for name in ("legacy-continuous", "new-continuous", "new-resumed")}
            hashes = {name: digest(value) for name, value in values.items()}
            equal = values["legacy-continuous"] == values["new-continuous"] == values["new-resumed"]
            report["semantic_comparisons"][filename] = {"equal": equal, "semantic_sha256": hashes}
            checks["identical_" + filename.removesuffix(".json")] = equal
        completed = read(output / "new-resumed" / "queries.json")
        resumed_calls = [row for row in ObservedTeacher.calls if row["phase"] in ("new_paused", "new_resumed")]
        phase = report["phases"][-1]
        checks["no_completed_query_recomputed_on_resume"] = len({row["query_id"] for row in resumed_calls}) == len(resumed_calls)
        checks["native_resume_calls_equal_logical_queries"] = len(resumed_calls) == len(completed)
        checks["resume_new_plus_reused_equals_logical_queries"] = phase["new_queries"] + phase["reused_queries"] == len(completed)
        checks["exactly_two_queries_reused"] = phase["reused_queries"] == 2
        checks["resumed_completed"] = resumed.get("research_status") == "complete"
        checks["selected_only_declared_case_ids"] = all(
            [row["case_id"] for row in read(output / name / "predeclared.json")["cases"]] == ids
            for name in ("new-continuous", "new-resumed"))
        checks["both_holdout_families_retained"] = all(
            read(output / name / "run.json")["heldout_family_ids"] == heldout
            for name in ("legacy-continuous", "new-continuous", "new-resumed"))
        checks["holdout_records_not_exported"] = all(
            all(row["family_id"] not in heldout for row in read(output / name / "operations_memory.json")["records"])
            for name in ("legacy-continuous", "new-continuous", "new-resumed"))
        report["logical_query_count"] = len(completed)
        report["all_new_query_calls"] = ObservedTeacher.calls
    except BaseException as error:
        report["error"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        report["engine_processes"] = [{"phase": teacher.validation_phase, "pid": teacher.validation_pid,
            "closed": teacher.closed, "cleanup": teacher.cleanup_status,
            "identity": teacher.identity, "options": teacher.options,
            "runtime_defaults": getattr(teacher, "runtime_defaults", {})}
            for teacher in ObservedTeacher.instances]
        report["active_memory_sha256_after"] = sha(memory)
        report["checks"]["active_memory_bytes_unchanged"] = report["active_memory_sha256_after"] == memory_before
        report["checks"]["binary_bytes_unchanged"] = sha(binary) == predeclared["binary_sha256"]
        report["checks"]["execution_sources_unchanged"] = all(sha(ROOT / name) == value for name, value in source_hashes.items())
        report["checks"]["all_native_processes_closed_with_zero_returncode"] = bool(ObservedTeacher.instances) and all(
            teacher.closed and teacher.cleanup_status.get("returncode_observed") is True
            and teacher.cleanup_status.get("returncode") == 0 for teacher in ObservedTeacher.instances)
        report["artifact_sha256"] = {path.relative_to(output).as_posix(): sha(path)
            for path in sorted(output.rglob("*")) if path.is_file() and path.name != "report.json"}
        report["passed"] = report["error"] is None and all(report["checks"].values())
        atomic_write(output / "report.json", report)
        current.RuntimeTeacher = RuntimeTeacher
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=ROOT / "build/runtime-engines" / ACTIVE_BINARY_SHA256 / "regicide.exe")
    parser.add_argument("--output", type=Path, default=ROOT / "build/gambit-line-research")
    parser.add_argument("--wall-seconds", type=float, default=180)
    args = parser.parse_args()
    report = validate(args.binary, args.output, wall_seconds=args.wall_seconds)
    print(json.dumps({"passed": report["passed"], "checks": report["checks"], "report": str(args.output / "report.json")}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
