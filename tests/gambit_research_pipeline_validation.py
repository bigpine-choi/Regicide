"""A small native revision plus conservative merge; never activate the output."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import gambit_practice_memory as memory
import merge_gambit_memory
import prepare_gambit_research
import train_gambit_operations as training
from gambit_research_session import atomic_write

BINARY_SHA = "5011da4c5e6ef01c452bd7125e4e22b372d2c9cfb858ed8759057325acb0e6ba"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def validate(research, output, binary):
    research, output, binary = (Path(path).resolve() for path in (research, output, binary))
    output.mkdir(parents=True, exist_ok=False)
    sources = {**training.source_hashes(), **{name: sha(ROOT / name) for name in (
        "scripts/merge_gambit_memory.py", "scripts/prepare_gambit_research.py",
        "tests/gambit_research_pipeline_validation.py")}}
    inputs = {str(path): sha(path) for path in [memory.DEFAULT_MEMORY,
        *(research / name for name in ("operations_memory.json", "records.json", "practice.json", "run.json"))]}
    declared = {"created_utc": datetime.now(timezone.utc).isoformat(), "source_sha256": sources,
        "inputs_sha256": inputs, "binary_sha256": sha(binary),
        "selected_record": "kings-b0c8d3df038a:1", "nodes": 200000, "owner_turns": 2, "candidates": 2,
        "scope": "One explicit prior position, fresh higher-node revision, immutable-input merge, no strength claim"}
    atomic_write(output / "predeclared.json", declared)
    for name in sources:
        target = output / "execution-sources" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / name).read_bytes())
    report = {"checks": {}, "error": None}
    try:
        focus = prepare_gambit_research.prepare(output / "focus", research=research,
                                               record_ids=[declared["selected_record"]])
        if len(focus["focus_case_ids"]) != 1:
            raise ValueError("Expected exactly one non-heldout focused starting position")
        prepared = read(output / "focus/curriculum.json")
        old = read(research / "records.json")
        original = next(row for row in old if row["record_id"] == declared["selected_record"])
        selected = next(row for row in prepared["cases"] if row["case_id"] in focus["focus_case_ids"])
        checks = report["checks"]
        checks["focused_full_history_unchanged"] = selected["moves"] == original["history_uci"] and selected["fen"] == original["fen"]
        base = read(training.CURRICULUM)
        heldout = [row for row in base["cases"] if row["split"] == "holdout"]
        checks["all_original_holdout_cases_preserved"] = all(row in prepared["cases"] for row in heldout)
        deeper = training.run(binary, output / "deeper", curriculum=output / "focus/curriculum.json",
            case_ids=focus["focus_case_ids"], nodes=200000, owner_turns=2, candidates=2,
            per_family=1, wall_seconds=120)
        checks["new_revision_complete"] = deeper["research_status"] == "complete"
        checks["new_revision_does_not_reuse_lower_budget_queries"] = deeper["reused_queries"] == 0
        checks["requested_higher_budget_is_recorded"] = all(
            row["requested_nodes"] == 200000 for row in read(output / "deeper/queries.json"))
        report["revision"] = {"case_ids": focus["focus_case_ids"], "counts": deeper["counts"],
            "new_queries": deeper["new_queries"], "reused_queries": deeper["reused_queries"],
            "cleanup": read(output / "deeper/run.json")["cleanup"]}
        old_by_state = {memory.digest([row["fen"], row["history_uci"]]): row for row in old}
        comparisons = []
        for row in read(output / "deeper/records.json"):
            earlier = old_by_state.get(memory.digest([row["fen"], row["history_uci"]]))
            if earlier is None:
                continue
            candidates = {item["uci"]: item for item in earlier["candidates"]}
            for item in row["candidates"]:
                if item["uci"] in candidates:
                    previous = candidates[item["uci"]]
                    comparisons.append({"history_uci": row["history_uci"], "uci": item["uci"],
                        "before_cp": previous["offline_owner_cp"], "after_cp": item["offline_owner_cp"],
                        "before_depth": previous["native_depth"], "after_depth": item["native_depth"]})
        report["common_candidate_comparisons_not_accuracy_labels"] = comparisons
        merged = merge_gambit_memory.merge(memory.DEFAULT_MEMORY,
            [research / "operations_memory.json", output / "deeper/operations_memory.json"], output / "merged")
        merge_report = read(output / "merged/merge-report.json")
        checks["merge_not_activated"] = merge_report["activated"] is False
        checks["merged_loader_valid"] = memory._validate(merged) is merged
        checks["base_snapshot_bytes_preserved"] = (output / "merged/inputs/000-base.json").read_bytes() == memory.DEFAULT_MEMORY.read_bytes()
        board = memory._board(original["history_uci"])
        proposed = memory.proposals(board, "pipeline", engine_identity=merged["engine_identity"],
                                    memory_path=output / "merged/operations_memory.json")
        checks["merged_candidates_require_runtime_verification"] = bool(proposed) and all(
            row["candidate_only"] is True and memory.valid_proposal(board, row,
                engine_identity=merged["engine_identity"], memory_path=output / "merged/operations_memory.json")
            for row in proposed)
        report["merge"] = {"counts": merged["counts"], "status": merge_report["status"],
                           "memory_id": merged["memory_id"], "activated": False}
    except BaseException as error:
        report["error"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        report["checks"]["all_original_inputs_unchanged"] = all(sha(path) == expected for path, expected in inputs.items())
        report["checks"]["sources_unchanged"] = all(sha(ROOT / name) == expected for name, expected in sources.items())
        report["checks"]["binary_unchanged"] = sha(binary) == declared["binary_sha256"]
        report["passed"] = report["error"] is None and all(report["checks"].values())
        atomic_write(output / "report.json", report)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--research", type=Path, default=ROOT / "build/gambit-branch-research/new-width2")
    parser.add_argument("--output", type=Path, default=ROOT / "build/gambit-research-pipeline")
    parser.add_argument("--binary", type=Path, default=ROOT / "build/runtime-engines" / BINARY_SHA / "regicide.exe")
    args = parser.parse_args()
    result = validate(args.research, args.output, args.binary)
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result["passed"] else 1)
