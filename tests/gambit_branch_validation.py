"""Native equivalence, restart and bounded coverage checks for research forks.

Uses only Regicide, with fixed-node queries and predeclared curriculum roots.
Coverage is measured separately from playing strength and wall-clock speed.
"""
import argparse
import contextlib
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

import chess

import gambit_research_validation as support

ROOT = support.ROOT
sys.path.insert(0, str(ROOT / "scripts"))
import train_gambit_operations as current
from gambit_research_session import atomic_write
from train_gambit_policy import RuntimeTeacher

BASE_COMMIT = "2a344c6f1efb2ef3170ae2dcf8631514627573dc"


class ObservedTeacher(support.ObservedTeacher):
    def query(self, board, query_id, *, root_moves=None):
        row = {"phase": self.validation_phase, "query_id": query_id,
               "fen": board.fen(en_passant="fen"), "history_uci": [move.uci() for move in board.move_stack],
               "requested_nodes": self.nodes, "completed": False,
               "root_moves_uci": None if root_moves is None else [move.uci() for move in root_moves]}
        type(self).calls.append(row)
        kwargs = {} if root_moves is None else {"root_moves": root_moves}
        result = RuntimeTeacher.query(self, board, query_id, **kwargs)
        row.update(completed=result["completed"], nodes=result["nodes"], depth=result["depth"],
                   bestmove_uci=result["bestmove_uci"], score=result["score"], pv_uci=result["pv_uci"])
        return result


def coverage(directory):
    queries = support.read(directory / "queries.json")
    records = support.read(directory / "records.json")
    practice = support.read(directory / "practice.json")
    manifest = support.read(directory / "run.json")
    return {"logical_queries": len(queries), "requested_nodes": len(queries) * manifest["nodes"],
            "actual_reported_nodes": sum(row["nodes"] for row in queries),
            "declared_node_cap": manifest.get("max_research_nodes"),
            "coverage_budget_limited": manifest.get("coverage_budget_limited", False),
            "owner_records": len(records), "unique_full_history_states": len({tuple(row["history_uci"]) for row in records}),
            "unique_position_keys": len({row["position_key"] for row in records}),
            "declared_branches": len(practice), "continued_branches": sum(bool(row["turns"]) for row in practice),
            "alternative_reply_queries": sum(row["role"] == "opponent_alternative" for row in queries),
            "branches": [{"case_id": row["case_id"], "branch_id": row.get("branch_id"),
                "initial_history_uci": row["initial_history_uci"], "final_history_uci": row["final_history_uci"],
                "owner_turns": len(row["turns"]), "turn_numbers": [turn["turn"] for turn in row["turns"]],
                "result": row["result"]} for row in practice]}


def fork_checks(directory):
    queries = support.read(directory / "queries.json")
    records = support.read(directory / "records.json")
    practice = support.read(directory / "practice.json")
    lookup = {row["query_id"]: row for row in queries}
    checks = {"alternative_replies_match_legal_restricted_root_sets": True,
              "fork_histories_include_the_selected_alternative_reply": True,
              "forks_exclude_the_primary_reply": True, "distinct_branch_full_histories": True,
              "restricted_scores_never_label_main_memory_candidates": True}
    for query in queries:
        if query["role"] != "opponent_alternative":
            continue
        board = current.memory._board(query["history_uci"])
        roots = query.get("root_moves_uci")
        checks["alternative_replies_match_legal_restricted_root_sets"] &= bool(
            isinstance(roots, list) and roots and roots == sorted(set(roots))
            and query["bestmove_uci"] in roots
            and all(chess.Move.from_uci(uci) in board.legal_moves for uci in roots))
    for branch in practice:
        if "fork" not in branch:
            continue
        fork = branch["fork"]
        query = lookup[fork["reply_query_id"]]
        primary = lookup[fork["parent_record_id"] + ":" + fork["fork_owner_move"]]
        expected = query["history_uci"] + [query["bestmove_uci"]]
        board = current.memory._board(expected)
        checks["fork_histories_include_the_selected_alternative_reply"] &= (
            query["role"] == "opponent_alternative" and query["bestmove_uci"] == fork["opponent_move"]
            and branch["initial_history_uci"] == expected and branch["initial_fen"] == board.fen(en_passant="fen"))
        checks["forks_exclude_the_primary_reply"] &= primary["bestmove_uci"] not in query["root_moves_uci"]
    checks["distinct_branch_full_histories"] = len({tuple(row["initial_history_uci"]) for row in practice}) == len(practice)
    for record in records:
        for candidate in record["candidates"]:
            query = lookup[candidate["query_id"]]
            checks["restricted_scores_never_label_main_memory_candidates"] &= (
                "root_moves_uci" not in query and query["role"] != "opponent_alternative"
                and candidate["offline_owner_cp"] == -query["score"]["stm"])
    return checks


def validate(binary, output, *, wall_seconds=180):
    binary, output = Path(binary).resolve(strict=True), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    memory_before = support.sha(current.memory.DEFAULT_MEMORY)
    if memory_before != support.ACTIVE_MEMORY_SHA256:
        raise ValueError("Active memory differs from the predeclared baseline")
    support.BASE_COMMIT = BASE_COMMIT
    legacy, legacy_path = support.load_legacy(output)
    full = support.read(current.CURRICULUM)
    cases = [copy.deepcopy(row) for row in legacy.select_cases(full, 1, 20261105)
             if row["family_id"] in {"benko", "from", "kings"}]
    if len(cases) != 3 or {row["family_id"] for row in cases} != {"benko", "from", "kings"}:
        raise ValueError("Missing one of the three predeclared families")
    ids = [row["case_id"] for row in cases]
    kings_id = next(row["case_id"] for row in cases if row["family_id"] == "kings")
    heldout = sorted({row["family_id"] for row in cases if row["split"] == "holdout"})
    if heldout != ["benko", "from"]:
        raise ValueError("Both holdout families must remain declared")
    subset = copy.deepcopy(full)
    subset["cases"] = cases
    subset.pop("curriculum_sha256", None)
    subset["counts"] = {"cases": 3, "families": 3, "scope": "validation input subset only"}
    subset["validation_subset"] = {"source_full_curriculum_sha256": support.sha(current.CURRICULUM),
        "selection": "2a344c6 select_cases(full, per_family=1, seed=20261105), then benko/from/kings",
        "scope": "implementation and coverage validation; no full-curriculum training or strength claim"}
    curriculum = output / "validation-curriculum.json"
    atomic_write(curriculum, subset)
    source_hashes = current.source_hashes()
    for path in (Path(__file__), Path(support.__file__)):
        source_hashes[path.relative_to(ROOT).as_posix()] = support.sha(path)
    for name in source_hashes:
        destination = output / "execution-sources" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((ROOT / name).read_bytes())
    predeclared = {"schema": "regicide.gambit-branch-native-validation.v1",
        "created_utc": datetime.now(timezone.utc).isoformat(), "baseline_commit": BASE_COMMIT,
        "legacy_source_sha256": support.sha(legacy_path), "source_sha256": source_hashes,
        "binary": str(binary), "binary_sha256": support.sha(binary), "active_memory_sha256_before": memory_before,
        "curriculum_sha256": support.sha(curriculum), "full_curriculum_sha256": support.sha(current.CURRICULUM),
        "case_ids": ids, "cases": cases, "heldout_family_ids": heldout,
        "settings": {"nodes": 100000, "owner_turns": 2, "candidates": 2, "per_family": 1,
                     "seed": 20261105, "max_branches": 2, "wall_seconds": wall_seconds},
        "expected_queries_without_terminal_children": {"width1": 18, "width2": 30},
        "pause_after_new_queries": 4,
        "same_cap_comparison": {"case_id": kings_id, "owner_turns": 4,
                                "opponent_replies": [1, 2], "max_research_nodes": 1000000,
                                "interpretation": "same requested-node ceiling, possibly different consumed work due to whole-turn reservations"},
        "semantic_exclusions": sorted(support.EXCLUDED_SEMANTIC_FIELDS),
        "legacy_relocation_adjustments": ["ROOT points to checkout", "source manifest identifies extracted legacy bytes"],
        "scope": "before/after and restart equality; bounded branch coverage, not Elo, human strength or speed"}
    atomic_write(output / "predeclared.json", predeclared)
    report = {"predeclared_sha256": support.sha(output / "predeclared.json"), "phases": [], "checks": {}, "error": None}
    ObservedTeacher.instances, ObservedTeacher.calls = [], []
    legacy.RuntimeTeacher = current.RuntimeTeacher = ObservedTeacher
    arguments = dict(curriculum=curriculum, nodes=100000, owner_turns=2, candidates=2,
                     per_family=1, seed=20261105, wall_seconds=wall_seconds)
    def run_phase(label, module, directory, **extra):
        ObservedTeacher.phase = label
        started, count = time.monotonic(), len(ObservedTeacher.calls)
        settings = {**arguments, **extra}
        with (output / (label + ".stdout.log")).open("x", encoding="utf-8") as stream, contextlib.redirect_stdout(stream):
            answer = module.run(binary, output / directory, **settings)
        row = {"phase": label, "output": directory, "wall_seconds": time.monotonic() - started,
               "native_calls": len(ObservedTeacher.calls) - count, "research_status": answer.get("research_status"),
               "new_queries": answer.get("new_queries"), "reused_queries": answer.get("reused_queries"),
               "reason": answer.get("reason")}
        report["phases"].append(row)
        print(json.dumps(row), flush=True)
        return answer
    try:
        run_phase("legacy_width1", legacy, "legacy-width1")
        run_phase("new_width1", current, "new-width1", case_ids=ids, opponent_replies=1, max_branches=2)
        run_phase("new_width2", current, "new-width2", case_ids=ids, opponent_replies=2, max_branches=2)
        pause = run_phase("new_width2_pause", current, "new-width2-resumed", case_ids=ids,
                          opponent_replies=2, max_branches=2, max_new_queries=4)
        if pause.get("research_status") != "paused" or pause.get("reason") != "query_budget":
            raise ValueError("Predeclared pause boundary was not reached")
        run_phase("new_width2_resume", current, "new-width2-resumed", case_ids=ids,
                  opponent_replies=2, max_branches=2, resume=True)
        for width in (1, 2):
            run_phase("capped_width" + str(width), current, "capped-width" + str(width),
                      case_ids=[kings_id], owner_turns=4, opponent_replies=width, max_branches=2,
                      max_research_nodes=1000000)
        checks = report["checks"]
        report["semantic_comparisons"] = {}
        for comparison, left, right in (("width1_before_after", "legacy-width1", "new-width1"),
                                         ("width2_restart", "new-width2", "new-width2-resumed")):
            report["semantic_comparisons"][comparison] = {}
            for filename in ("records.json", "practice.json", "queries.json"):
                first, second = [support.semantic(support.read(output / folder / filename)) for folder in (left, right)]
                equal = first == second
                report["semantic_comparisons"][comparison][filename] = {
                    "equal": equal, "sha256": {left: support.digest(first), right: support.digest(second)}}
                checks[comparison + "_" + filename.removesuffix(".json")] = equal
        checks.update(fork_checks(output / "new-width2"))
        completed = support.read(output / "new-width2-resumed" / "queries.json")
        resumed_calls = [row for row in ObservedTeacher.calls if row["phase"] in ("new_width2_pause", "new_width2_resume")]
        resume_phase = next(row for row in report["phases"] if row["phase"] == "new_width2_resume")
        checks["completed_queries_not_recomputed_after_pause"] = len({row["query_id"] for row in resumed_calls}) == len(resumed_calls)
        checks["resume_new_plus_reused_equals_logical_queries"] = resume_phase["new_queries"] + resume_phase["reused_queries"] == len(completed)
        checks["four_queries_reused_after_pause"] = resume_phase["reused_queries"] == 4
        checks["resume_native_calls_equal_completed_queries"] = len(resumed_calls) == len(completed)
        directories = ("legacy-width1", "new-width1", "new-width2", "new-width2-resumed", "capped-width1", "capped-width2")
        checks["all_phase_runs_completed"] = all(row["research_status"] == "complete" for row in report["phases"] if row["phase"] != "new_width2_pause")
        checks["holdout_declarations_preserved"] = all(support.read(output / name / "run.json")["heldout_family_ids"] == heldout for name in directories)
        checks["holdout_records_not_exported"] = all(
            all(row["family_id"] not in heldout for row in support.read(output / name / "operations_memory.json")["records"])
            for name in directories)
        report["coverage"] = {name: coverage(output / name) for name in directories}
        checks["both_capped_runs_respect_whole_logical_node_ceiling"] = all(
            report["coverage"]["capped-width" + str(width)]["requested_nodes"] <= 1000000 for width in (1, 2))
        checks["capped_width2_contains_a_continued_alternative_history"] = any(
            row["branch_id"] is not None and row["owner_turns"] > 0
            for row in report["coverage"]["capped-width2"]["branches"])
        checks["width2_contains_more_than_one_continued_branch_per_case"] = all(
            sum(row["case_id"] == case_id and row["owner_turns"] > 0
                for row in report["coverage"]["new-width2"]["branches"]) == 2 for case_id in ids)
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
        report["active_memory_sha256_after"] = support.sha(current.memory.DEFAULT_MEMORY)
        report["checks"]["active_memory_bytes_unchanged"] = report["active_memory_sha256_after"] == memory_before
        report["checks"]["binary_bytes_unchanged"] = support.sha(binary) == predeclared["binary_sha256"]
        report["checks"]["execution_sources_unchanged"] = all(support.sha(ROOT / name) == value for name, value in source_hashes.items())
        report["checks"]["all_native_processes_closed_with_zero_returncode"] = bool(ObservedTeacher.instances) and all(
            teacher.closed and teacher.cleanup_status.get("returncode_observed") is True
            and teacher.cleanup_status.get("returncode") == 0 for teacher in ObservedTeacher.instances)
        report["artifact_sha256"] = {path.relative_to(output).as_posix(): support.sha(path)
            for path in sorted(output.rglob("*")) if path.is_file() and path.name != "report.json"}
        report["passed"] = report["error"] is None and all(report["checks"].values())
        atomic_write(output / "report.json", report)
        current.RuntimeTeacher = RuntimeTeacher
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=ROOT / "build/runtime-engines" / support.ACTIVE_BINARY_SHA256 / "regicide.exe")
    parser.add_argument("--output", type=Path, default=ROOT / "build/gambit-branch-research")
    parser.add_argument("--wall-seconds", type=float, default=180)
    args = parser.parse_args()
    report = validate(args.binary, args.output, wall_seconds=args.wall_seconds)
    print(json.dumps({"passed": report["passed"], "checks": report["checks"], "report": str(args.output / "report.json")}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
