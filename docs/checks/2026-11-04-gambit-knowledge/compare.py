"""Summarize frozen native diagnostics; do not infer strength from these cases."""
from pathlib import Path
import hashlib
import json
import statistics

ROOT = Path(__file__).resolve().parents[3]
DIRECTORY = Path(__file__).resolve().parent


def read(relative):
    return json.loads((DIRECTORY / relative).read_text(encoding="utf-8"))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    before_reports = [read(n + "/report.json") for n in
                      ("native-before-v1", "native-before-supplement-v1")]
    after_reports = [read(n + "/report.json") for n in
                     ("native-after-v1", "native-after-supplement-v1")]
    before = {r["case"]["id"]: r for report in before_reports for r in report["results"]}
    after = {r["case"]["id"]: r for report in after_reports for r in report["results"]}
    rows, matched = [], []
    for key, old in before.items():
        new = after[key]
        assert old["case"]["history_uci"] == new["case"]["history_uci"]
        assert old["case"]["seed"] == new["case"]["seed"]
        assert old["case"]["before_fen"] == new["case"]["before_fen"]
        knowledge = [(t, t["receipt"].get("knowledge")) for t in new["own_turns"]
                     if isinstance(t["receipt"].get("knowledge"), dict)]
        old_positions = {(t["before_fen"], tuple(t["receipt"]["history_uci"])): t for t in old["own_turns"]}
        for turn in new["own_turns"]:
            previous = old_positions.get((turn["before_fen"], tuple(turn["receipt"]["history_uci"])))
            if previous is not None:
                matched.append({"case": key, "ply": turn["ply"], "fen": turn["before_fen"],
                    "before_uci": previous["uci"], "after_uci": turn["uci"],
                    "before_loss_cp": previous["audit"].get("loss_cp"),
                    "after_loss_cp": turn["audit"].get("loss_cp"),
                    "knowledge_changed_native_candidate": turn["receipt"].get("knowledge", {}).get("applied") is True})
        rows.append({"id": key, "family": old["case"]["family"], "split": old["case"]["split"],
            "before_moves": [t["san"] for t in old["own_turns"]],
            "after_moves": [t["san"] for t in new["own_turns"]],
            "before_loss_cp": [t["audit"].get("loss_cp") for t in old["own_turns"]],
            "after_loss_cp": [t["audit"].get("loss_cp") for t in new["own_turns"]],
            "knowledge_turns": len(knowledge),
            "knowledge_matches_native_baseline": sum(v.get("reason") == "knowledge_move_is_baseline" for _t, v in knowledge),
            "knowledge_changed_native_candidate": sum(v.get("applied") is True for _t, v in knowledge),
            "learned_rank_active": sum(v.get("ranking", {}).get("active") is True for _t, v in knowledge),
            "learned_rank_changed_order": sum(v.get("ranking", {}).get("applied") is True for _t, v in knowledge),
            "ranking_reasons": sorted({v.get("ranking", {}).get("reason", "missing") for _t, v in knowledge}),
            "conditional_defense_replies_before": sum(t["source"] == "predeclared_conditional_defense" for t in old["opponent_turns"]),
            "conditional_defense_replies_after": sum(t["source"] == "predeclared_conditional_defense" for t in new["opponent_turns"]),
            "before_passed": old["passed"], "after_passed": new["passed"]})
    all_before = [t for r in before.values() for t in r["own_turns"]]
    all_after = [t for r in after.values() for t in r["own_turns"]]
    changed = [(r["case"]["id"], t) for r in after.values() for t in r["own_turns"]
               if t["receipt"].get("knowledge", {}).get("applied") is True]
    quality = []
    for case_id, turn in changed:
        detail = turn["receipt"]["knowledge"]
        evidence = detail.get("evidence") or {}
        quality.append({"case": case_id, "ply": turn["ply"], "uci": turn["uci"],
                        "native_loss_cp": detail.get("loss_cp"),
                        "external_loss_cp": turn["audit"].get("loss_cp"),
                        "material_screen_passed": evidence.get("material_screen", {}).get("admitted") is True,
                        "native_floor_passed": type(detail.get("loss_cp")) is int and detail["loss_cp"] <= 35})
    manifest = read("after-source-manifest.json")
    mismatches = [n for n, value in manifest["source_sha256"].items()
                  if not (ROOT / n).is_file() or digest(ROOT / n) != value]
    losses_before = [t["audit"]["loss_cp"] for t in all_before if t["audit"].get("loss_cp") is not None]
    losses_after = [t["audit"]["loss_cp"] for t in all_after if t["audit"].get("loss_cp") is not None]
    report = {"schema": "regicide.gambit-knowledge-comparison.v1",
        "scope": "Functional continuation diagnostics; authored prefixes; correlated small sample; not complete matches/Elo",
        "branches_per_version": len(rows), "native_turns_before": len(all_before), "native_turns_after": len(all_after),
        "all_branches_passed": all(r["before_passed"] and r["after_passed"] for r in rows),
        "all_runtime_snapshots_unchanged": all(r["sources_unchanged"] for r in before_reports + after_reports),
        "all_binaries_unchanged": all(r["binary_unchanged"] for r in before_reports + after_reports),
        "equal_binary": len({r["binary_sha256"] for r in before_reports + after_reports}) == 1,
        "equal_requested_budgets": len({json.dumps(r["config"], sort_keys=True) for r in before_reports + after_reports}) == 1,
        "current_source_hash_mismatches": mismatches,
        "all_audits_depth_12": all(t["audit"].get("depth") == 12 for t in all_before + all_after),
        "audit_depth_counts_before": {str(depth): sum(t["audit"].get("depth") == depth for t in all_before)
                                      for depth in sorted({t["audit"].get("depth") for t in all_before})},
        "audit_depth_counts_after": {str(depth): sum(t["audit"].get("depth") == depth for t in all_after)
                                     for depth in sorted({t["audit"].get("depth") for t in all_after})},
        "descriptive_only": {"mean_external_loss_cp_before": round(statistics.mean(losses_before), 4),
            "mean_external_loss_cp_after": round(statistics.mean(losses_after), 4),
            "max_external_loss_cp_before": max(losses_before), "max_external_loss_cp_after": max(losses_after),
            "external_loss_over_150_before": sum(v > 150 for v in losses_before),
            "external_loss_over_150_after": sum(v > 150 for v in losses_after),
            "median_wall_ms_before": statistics.median(t["wall_ms"] for t in all_before),
            "median_wall_ms_after": statistics.median(t["wall_ms"] for t in all_after)},
        "knowledge_turns": sum(r["knowledge_turns"] for r in rows),
        "knowledge_matches_native_baseline": sum(r["knowledge_matches_native_baseline"] for r in rows),
        "knowledge_changed_native_candidate": len(changed),
        "learned_rank_active": sum(r["learned_rank_active"] for r in rows),
        "learned_rank_changed_order": sum(r["learned_rank_changed_order"] for r in rows),
        "all_changed_candidates_passed_native_floor_and_capture_screen": all(
            r["native_floor_passed"] and r["material_screen_passed"] for r in quality),
        "changed_candidate_quality": quality, "matched_exact_roots": matched, "cases": rows,
        "interpretation": ["Knowledge graph activation and trained-model deployment are separate facts",
            "A rejected experimental model must not be reported as active learned play",
            "Later positions differ between versions; average audit loss is descriptive, not a paired strength estimate",
            "External audit depth12 is bounded verification, not proof that no tactical loss exists"]}
    (DIRECTORY / "comparison.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({k: v for k, v in report.items() if k not in ("cases", "interpretation", "changed_candidate_quality", "matched_exact_roots")}, ensure_ascii=False))
    assert report["all_branches_passed"] and report["all_runtime_snapshots_unchanged"] and report["all_binaries_unchanged"]
    assert report["equal_binary"] and report["equal_requested_budgets"] and not mismatches
    assert report["all_changed_candidates_passed_native_floor_and_capture_screen"]


if __name__ == "__main__":
    main()
