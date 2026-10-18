"""Post-run descriptive report only; never supplies learning or selection input."""
from collections import Counter
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import statistics


ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "build/neural-cost-quality/quality-analysis.json"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def distribution(values):
    values = sorted(values)
    if not values:
        return {"count": 0}
    return {"count": len(values), "mean": statistics.mean(values),
            "median": statistics.median(values), "minimum": values[0], "maximum": values[-1],
            "p90_nearest_rank": values[math.ceil(.9 * len(values)) - 1]}


def line_groups(path):
    result = {"train": [], "validation": [], "test": []}
    for line in path.read_bytes().splitlines(keepends=True):
        result[json.loads(line)["split"]].append(line)
    return result


def main():
    runs = sorted((ROOT / "build/learning-cycle/runs").glob("000004-*"))
    if len(runs) != 1:
        raise ValueError("expected exactly one attempt 4 run")
    run = runs[0]
    report_path = run / "quality/quality-report.json"
    manifest_path = run / "quality/manifest.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if report.get("status") != "complete" or manifest.get("status") != "complete":
        raise ValueError("quality has not completed")
    source_manifest_path = run / "corpus/manifest.json"
    source_manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
    source_positions_path = source_manifest_path.parent / source_manifest["positions_path"]
    output_positions_path = manifest_path.parent / manifest["positions_path"]
    source_rows = [json.loads(line) for line in source_positions_path.read_text(encoding="utf-8").splitlines()]
    output_rows = [json.loads(line) for line in output_positions_path.read_text(encoding="utf-8").splitlines()]
    records = report["records"]
    accepted = [record for record in records if record["accepted"]]
    rejected = [record for record in records if not record["accepted"]]
    deltas = [record["stages"][-1]["cp_stm"] - record["original_teacher_cp_stm"] for record in accepted]
    depth_deltas = [record["stages"][-1]["completed_depth"] - source_rows[record["source_row_index"]]["teacher_depth"]
                    for record in accepted]
    old_hce_gaps = [abs(record["original_teacher_cp_stm"] - record["hce_cp_stm"]) for record in accepted]
    new_hce_gaps = [abs(record["stages"][-1]["cp_stm"] - record["hce_cp_stm"]) for record in accepted]
    source_groups, output_groups = line_groups(source_positions_path), line_groups(output_positions_path)
    holds = {}
    for split in ("validation", "test"):
        original, derived = b"".join(source_groups[split]), b"".join(output_groups[split])
        canonical = lambda rows: hashlib.sha256(json.dumps([row for row in rows if row["split"] == split],
                sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
        holds[split] = {"original_rows": len(source_groups[split]), "derived_rows": len(output_groups[split]),
            "exact_lines_equal": original == derived,
            "original_raw_lines_sha256": hashlib.sha256(original).hexdigest(),
            "derived_raw_lines_sha256": hashlib.sha256(derived).hexdigest(),
            "canonical_rows_equal": canonical(source_rows) == canonical(output_rows),
            "canonical_sha256": canonical(source_rows)}
    depth = {}
    for index, nodes in enumerate(report["policy"]["node_budgets"]):
        stages = [record["stages"][index] for record in records]
        accepted_stages = [record["stages"][index] for record in accepted]
        depth[str(nodes)] = {"completed_depth_histogram_all": dict(sorted(Counter(
            str(stage["completed_depth"]) for stage in stages).items())),
            "completed_depth_all": distribution([stage["completed_depth"] for stage in stages if stage["completed_depth"] is not None]),
            "completed_depth_accepted": distribution([stage["completed_depth"] for stage in accepted_stages]),
            "actual_nodes_all": distribution([stage["reported_nodes"] for stage in stages if stage["reported_nodes"] is not None]),
            "eligible_exact_cp_all": sum(stage["exclusion"] is None for stage in stages),
            "exclusions": dict(Counter(stage["exclusion"] for stage in stages if stage["exclusion"] is not None))}
    same_original_move = sum(source_rows[record["source_row_index"]]["teacher_pv"][0] == record["stages"][-1]["best_move"]
                             for record in accepted)
    details = {"schema": "regicide.teacher-quality.postrun-analysis.v1", "status": "complete",
        "use": "post-run descriptive reporting only; never used for model, hyperparameter, policy or promotion selection",
        "source_commit": "426830231d5291991a589cf73b058c88b653d1fe", "run": run.name, "report_sha256": digest(report_path),
        "manifest_sha256": digest(manifest_path), "source_manifest_sha256": digest(source_manifest_path),
        "policy": report["policy"], "policy_sha256": report["policy_sha256"],
        "counts": {"requested": len(records), "accepted": len(accepted), "excluded": len(rejected),
            "acceptance_fraction": len(accepted) / len(records) if records else None,
            "queries_completed": report["queries_completed"], "trainable": manifest["trainable"],
            "source_by_split": dict(Counter(row["split"] for row in source_rows)),
            "output_by_split": dict(Counter(row["split"] for row in output_rows)),
            "accepted_games": len({record["game_id"] for record in accepted}),
            "accepted_families": len({source_rows[record["source_row_index"]]["family"] for record in accepted}),
            "accepted_turns": dict(Counter(record["turn"] for record in accepted)),
            "excluded_turns": dict(Counter(record["turn"] for record in rejected))},
        "exclusions": dict(Counter(record["exclusion"] for record in rejected)),
        "accepted_label_change_cp_stm": {"signed_final_minus_original": distribution(deltas),
            "absolute_final_minus_original": distribution([abs(value) for value in deltas]),
            "unchanged": sum(value == 0 for value in deltas),
            "absolute_at_most_30": sum(abs(value) <= 30 for value in deltas),
            "absolute_over_100": sum(abs(value) > 100 for value in deltas),
            "original_best_move_unchanged": same_original_move,
            "original_best_move_changed": len(accepted) - same_original_move,
            "original_vs_hce_absolute_gap": distribution(old_hce_gaps),
            "final_vs_hce_absolute_gap": distribution(new_hce_gaps),
            "interpretation": "Label changes or gaps from static HCE do not establish greater correctness or playing strength."},
        "accepted_completed_depth_change": {"final_minus_original": distribution(depth_deltas),
            "deeper": sum(value > 0 for value in depth_deltas), "same": sum(value == 0 for value in depth_deltas),
            "shallower": sum(value < 0 for value in depth_deltas),
            "original_depth": distribution([source_rows[record["source_row_index"]]["teacher_depth"] for record in accepted]),
            "final_depth": distribution([record["stages"][-1]["completed_depth"] for record in accepted]),
            "original_reported_nodes": distribution([record["original_teacher_nodes"] for record in accepted])},
        "metric_comparison_limit": "Train labels changed, so before/after train MSE is not an error reduction against one unchanged target. Validation/test retain their original labels. Actual strength requires the separate unchanged promotion arena.",
        "last_two_cp_delta": {"all_finite": distribution([record["last_two_cp_delta"] for record in records if record["last_two_cp_delta"] is not None]),
                              "accepted": distribution([record["last_two_cp_delta"] for record in accepted])},
        "depth_by_node_budget": depth, "heldout_preservation": holds,
        "game_file_bytes_equal": (source_manifest_path.parent / source_manifest["games_path"]).read_bytes() == (manifest_path.parent / manifest["games_path"]).read_bytes(),
        "pgn_bytes_equal": (source_manifest_path.parent / source_manifest["pgn_path"]).read_bytes() == (manifest_path.parent / manifest["pgn_path"]).read_bytes(),
        "quality_wall_seconds": (datetime.fromisoformat(report["finished_utc"]) - datetime.fromisoformat(report["started_utc"])).total_seconds(),
        "checks": {"quality_report_hash_matches_manifest": manifest["quality_report_sha256"] == digest(report_path),
                   "report_source_hash_matches_corpus": report["source_manifest_sha256"] == digest(source_manifest_path),
                   "expected_three_queries_per_training_position": report["queries_completed"] == 3 * len(records),
                   "accepted_rows_match_output_train_count": len(accepted) == sum(row["split"] == "train" for row in output_rows),
                   "report_counts_match": report["accepted_train_positions"] == len(accepted)}}
    if not all(details["checks"].values()) or not all(item["exact_lines_equal"] and item["canonical_rows_equal"] for item in holds.values()):
        raise ValueError("post-run quality consistency checks failed")
    with OUTPUT.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(details, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    print(json.dumps({"output": str(OUTPUT), "counts": details["counts"], "exclusions": details["exclusions"]}))


if __name__ == "__main__":
    main()
