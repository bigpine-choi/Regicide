"""Summarize immutable native operation branches without strength claims."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import statistics


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def nested(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from nested(item)
    elif isinstance(value, list):
        for item in value:
            yield from nested(item)


def metrics(report):
    turns = [{**turn, "_case": row["case"]} for row in report["results"] for turn in row["own_turns"]]
    opponent = [turn for row in report["results"] for turn in row["opponent_turns"]]
    audits = [turn["audit"] for turn in turns]
    losses = [audit["loss_cp"] for audit in audits if audit.get("loss_cp") is not None]
    opponent_audits = [turn["audit"] for turn in opponent if "audit" in turn]
    opponent_losses = [audit["loss_cp"] for audit in opponent_audits if audit.get("loss_cp") is not None]
    goals = Counter()
    operational_turns = 0
    memory_source_turns = 0
    selected_memory_turns = 0
    operations_applied = 0
    memory_applied_alternatives = 0
    memory_baseline_matches = 0
    holdout_memory_observed = 0
    changed_operations = []
    old_rank_active = 0
    for turn in turns:
        candidates = list(nested(turn["receipt"]))
        old_rank_active += any(row.get("policy") == "regicide-gambit-ranking-v1" and row.get("active") is True
                               for row in candidates)
        detail = turn["receipt"].get("operations")
        context = turn["receipt"].get("operations_context") or {}
        operation = detail if isinstance(detail, dict) else {}
        operational_turns += bool(operation)
        memory_rows = [row for row in nested(operation) if row.get("source") == "native_practice_memory"]
        memory_source_turns += bool(memory_rows)
        holdout_memory_observed += bool(memory_rows) and turn["_case"]["split"] == "holdout"
        selected_memory_turns += any(row.get("uci") == turn["uci"] for row in memory_rows)
        evidence = operation.get("evidence") or {}
        source = evidence.get("source") or {}
        memory_selected = source.get("source") == "native_practice_memory"
        operations_applied += operation.get("applied") is True
        memory_applied_alternatives += operation.get("applied") is True and memory_selected
        memory_baseline_matches += operation.get("reason") == "knowledge_move_is_baseline" and memory_selected
        if operation.get("applied") is True:
            baseline = operation.get("baseline") or {}
            common, final = evidence.get("candidate_compared") or {}, evidence.get("candidate_final") or {}
            cap = evidence.get("loss_cap_cp", operation.get("loss_cap_cp", context.get("loss_cap_cp")))
            floor = (all(type(value) in (int, float) for value in
                         [baseline.get("cp"), common.get("cp"), final.get("cp"), cap])
                     and min(common["cp"], final["cp"]) >= baseline["cp"] - cap)
            changed_operations.append({"case": turn["_case"]["id"], "family_id": turn["_case"]["family_id"],
                "ply": turn["ply"], "uci": turn["uci"],
                "goal": context.get("goal"), "source": source.get("source"),
                "native_loss_cp": operation.get("loss_cp"), "native_loss_cap_cp": cap,
                "native_floor_passed": floor,
                "material_screen_passed": (evidence.get("material_screen") or {}).get("admitted") is True,
                "external_loss_cp": turn["audit"].get("loss_cp")})
        # These are policy labels, never ground-truth plans or proof of success.
        goal = context.get("goal")
        if goal:
            goals[goal] += 1
    return {"branches": len(report["results"]), "own_turns": len(turns), "opponent_turns": len(opponent),
        "functional_passed": all(row["passed"] for row in report["results"]),
        "sources_unchanged": report.get("sources_unchanged"), "binary_unchanged": report.get("binary_unchanged"),
        "all_owned_engines_closed": report.get("all_owned_engines_closed"),
        "own_audit_status_counts": dict(Counter(audit["status"] for audit in audits)),
        "own_audit_common_depths": dict(Counter(str(audit.get("depth")) for audit in audits)),
        "own_finite_cp_audits": len(losses), "own_external_loss_over_150": sum(loss > 150 for loss in losses),
        "own_external_loss_over_300": sum(loss > 300 for loss in losses),
        "own_max_external_loss_cp": max(losses, default=None),
        "own_mean_external_loss_cp_descriptive": round(statistics.mean(losses), 4) if losses else None,
        "positive_mate_missed": sum(audit.get("reference_mate_own") is not None and audit["reference_mate_own"] > 0
            and not (audit.get("played_mate_own") is not None and audit["played_mate_own"] > 0) for audit in audits),
        "new_negative_mate": sum(audit.get("played_mate_own") is not None and audit["played_mate_own"] < 0
            and not (audit.get("reference_mate_own") is not None and audit["reference_mate_own"] < 0) for audit in audits),
        "opponent_sampled_audits": len(opponent_audits),
        "opponent_finite_cp_audits": len(opponent_losses),
        "opponent_external_loss_at_most_50": sum(loss <= 50 for loss in opponent_losses),
        "opponent_external_loss_over_150": sum(loss > 150 for loss in opponent_losses),
        "outside_book_turns": sum(turn["outside_book"] for turn in turns),
        "beyond_old_window_turns": sum(turn["beyond_old_initiative_window"] for turn in turns),
        "branches_debt_recovered": sum(row.get("descriptive_observations", {}).get("debt_fully_recovered", False)
                                        for row in report["results"]),
        "operational_policy_receipt_turns": operational_turns,
        "memory_source_observed_turns": memory_source_turns,
        "selected_move_has_memory_source": selected_memory_turns,
        "operations_applied_alternatives": operations_applied,
        "memory_applied_alternatives": memory_applied_alternatives,
        "memory_baseline_matches": memory_baseline_matches,
        "holdout_memory_source_turns": holdout_memory_observed,
        "changed_operations": changed_operations,
        "old_experimental_rank_active": old_rank_active, "policy_goal_labels": dict(goals),
        "cases": [{"id": row["case"]["id"], "family_id": row["case"]["family_id"],
                   "split": row["case"]["split"], "passed": row["passed"],
                   "moves": [turn["san"] for turn in row["own_turns"]],
                   "observations": row.get("descriptive_observations"), "result": row.get("result"),
                   "termination": row.get("termination"),
                   "max_external_loss_cp": max((turn["audit"]["loss_cp"] for turn in row["own_turns"]
                                                 if turn["audit"].get("loss_cp") is not None), default=None)}
                  for row in report["results"]]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    before = read(args.before)
    if "finished_utc" not in before:
        raise ValueError("Before run is not finished")
    value = {"schema": "regicide.gambit-operations-comparison.v1", "scope": "Capped post-sacrifice native practice branches; correlated sample, not matches or Elo",
             "before": metrics(before), "before_report_sha256": hashlib.sha256(args.before.read_bytes()).hexdigest()}
    if args.after:
        after = read(args.after)
        if "finished_utc" not in after:
            raise ValueError("After run is not finished")
        value.update(after=metrics(after), equal_binary=before["binary_sha256"] == after["binary_sha256"],
                     equal_requested_budgets=before["config"] == after["config"],
                     equal_fixtures=before["fixtures_sha256"] == after["fixtures_sha256"],
                     after_report_sha256=hashlib.sha256(args.after.read_bytes()).hexdigest())
        first = []
        for old, new in zip(before["results"], after["results"]):
            assert old["case"]["id"] == new["case"]["id"]
            a, b = old["own_turns"][0], new["own_turns"][0]
            assert a["before_fen"] == b["before_fen"]
            first.append({"case": old["case"]["id"], "before_uci": a["uci"], "after_uci": b["uci"],
                "same_audit_depth": a["audit"].get("depth") == b["audit"].get("depth"),
                "before_audit": a["audit"], "after_audit": b["audit"]})
        value["same_start_first_move_comparisons"] = first
    args.output.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({key: {k: v for k, v in item.items() if k != "cases"} for key, item in value.items()
                      if key in ("before", "after")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
