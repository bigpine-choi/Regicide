"""Read-only final integrity and scope checks for post-sacrifice operations."""
from pathlib import Path
import hashlib
import json


BASE = Path(__file__).resolve().parent
ROOT = Path(__file__).resolve().parents[3]


def read(name):
    return json.loads((BASE / name).read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    snapshot = read("after-source-v2-manifest.json")
    mismatches = [name for name, value in snapshot["source_sha256"].items()
                  if not (ROOT / name).is_file() or sha(ROOT / name) != value]
    comparison, defense = read("comparison.json"), read("defense-comparison.json")
    audit = read("substitution-audit-v2/report.json")
    rejected = read("substitution-audit-v1/report.json")
    fixture = read("predeclared/fixtures.json")
    memory = json.loads((ROOT / "data/gambits/operations_memory.json").read_text(encoding="utf-8"))
    ranking = json.loads((ROOT / "data/gambits/policy.json").read_text(encoding="utf-8"))
    reports = [read(name) for name in ("native-before-v1/report.json", "native-after-v2/report.json",
               "native-before-defense-v2/report.json", "native-after-defense-v2/report.json")]
    checks = {"final_runtime_source_matches_snapshot": not mismatches,
        "native_harness_matches_saved_copy": sha(ROOT / "tests/gambit_operations_validation.py") == sha(BASE / "validation-harness-v1.py"),
        "pinned_support_matches_fixture": sha(BASE / "native-support-v1.py") == fixture["support_sha256"],
        "main_equal_binary": comparison["equal_binary"],
        "main_equal_requested_budgets": comparison["equal_requested_budgets"],
        "main_equal_fixtures": comparison["equal_fixtures"],
        "defense_equal_binary": defense["equal_binary"],
        "defense_equal_requested_budgets": defense["equal_requested_budgets"],
        "defense_equal_fixtures": defense["equal_fixtures"],
        "all_final_branches_functional_pass": all(report["failed"] == 0 for report in reports),
        "all_final_sources_unchanged": all(report["sources_unchanged"] for report in reports),
        "all_final_binaries_unchanged": all(report["binary_unchanged"] for report in reports),
        "all_final_owned_player_engines_closed": all(report["all_owned_engines_closed"] for report in reports),
        "final_all_changed_operations_current_native_floor_and_capture_screen": all(
            row["native_floor_passed"] and row["material_screen_passed"]
            for row in comparison["after"]["changed_operations"]),
        "final_geometry_no_native_concession": all(
            row["native_loss_cap_cp"] == 0 and row["native_loss_cp"] == 0
            for row in comparison["after"]["changed_operations"] if row["source"] == "native_operations_geometry"),
        "final_all_substitutions_independently_audited": audit["substitutions"] == comparison["after"]["operations_applied_alternatives"]
            and audit["exact_common_depth_queries"] == audit["substitutions"],
        "final_no_critical_extra_regression": audit["critical_cp_regressions"] == 0,
        "final_no_new_negative_mate_vs_native_baseline": audit["new_negative_mates"] == 0,
        "final_independent_audit_engine_closed": audit["audit_engine_closed"],
        "rejected_initial_version_failure_preserved": rejected["critical_cp_regressions"] == 1,
        "runtime_memory_approved": memory["status"] == "approved",
        "holdout_families_not_in_runtime_memory": not any(row["family_id"] in ("benko", "from") for row in memory["records"]),
        "holdout_main_no_memory_hits": comparison["after"]["holdout_memory_source_turns"] == 0,
        "holdout_defense_no_memory_hits": defense["after"]["holdout_memory_source_turns"] == 0,
        "old_failed_ranking_still_experimental": ranking["status"] == "experimental" and ranking["acceptance"]["passed"] is False,
        "old_failed_ranking_never_active": comparison["after"]["old_experimental_rank_active"] == 0
            and defense["after"]["old_experimental_rank_active"] == 0,
        "defensive_goal_observed_in_separate_coverage": defense["after"]["policy_goal_labels"].get("defensive_transition", 0) >= 1}
    files = ["before-source.zip", "before-source-manifest.json", "after-source.zip", "after-source-manifest.json",
             "after-source-v2.zip", "after-source-v2-manifest.json", "predeclared/fixtures.json",
             "supplemental-predeclared/fixtures.json", "supplemental-predeclared/fixtures-v2.json",
             "validation-harness-v1.py", "native-support-v1.py", "comparison.json", "defense-comparison.json",
             "comparison-rejected-v1.json", "substitution-audit-v1/report.json", "substitution-audit-v2/report.json",
             "warning-audit-v1/report.json", "substitution-audit-v2/raw-uci.log",
             "native-before-v1/report.json", "native-after-v1/report.json", "native-after-v2/report.json",
             "native-before-defense-v1/report.json", "native-before-defense-v2/report.json", "native-after-defense-v2/report.json"]
    value = {"schema": "regicide.gambit-operations-final-verification.v1", "checks": checks,
             "passed": all(checks.values()), "runtime_files": snapshot["file_count"],
             "current_source_hash_mismatches": mismatches,
             "artifacts_sha256": {name: sha(BASE / name) for name in files},
             "scope": "Integrity, functional contracts and post-play substitution audit; capped branches are not strength/Elo evidence"}
    (BASE / "verification.json").write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                                            encoding="utf-8", newline="\n")
    print(json.dumps({"passed": value["passed"], "checks": len(checks), "mismatches": mismatches,
                      "failed_checks": [name for name, passed in checks.items() if not passed]}))
    raise SystemExit(not value["passed"])


if __name__ == "__main__":
    main()
