"""Audit a completed, fixed-condition LMR rematch without running any engine.

Run only after match-load.json has been finalized. The prior committed audit's
match/sidecar checks are reused; search/build/UI tests are not rerun or claimed.
New outputs are exclusive and can be generated again against a redacted copy.
"""
from pathlib import Path
import argparse
import collections
import datetime
import hashlib
import importlib.util
import json
import math
import statistics
import subprocess

import chess


ROOT = next(p for p in (Path(__file__).resolve().parent,
                       *Path(__file__).resolve().parents, Path.cwd(), *Path.cwd().parents)
            if (p / ".git").exists())
PLAN_COMMIT = "143323097ef8cf6bdfe685b149bd0e52a5ff5f45"
PLAN_PATH = "docs/experiments/2026-10-18-lmr-idle-rematch-plan.md"
OPENING_PATH = "tests/data/lmr_openings_2026-10-18.json"
OPENING_SHA = "82dee5ce8ada64fa981e2009afdb3508d78d363553d0d60d163e02d0953006e3"
OLD_DIRECTORY = ROOT / "docs/checks/2026-10-18-lmr"
OLD_AUDIT = OLD_DIRECTORY / "audit-lmr.py"
EXPECTED_BROWSER_PATTERN = "^(whale|chrome|msedge|firefox|brave|opera|vivaldi|arc|iexplore|chromium|thorium|waterfox|floorp|librewolf)$"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def module_from_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def utc(value):
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))


def finite_nonnegative(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def markdown(report):
    match, load = report["match"], report["load"]
    w, d, loss = match["WDL"]
    score = match["completed_game_score"]
    score_text = f"{score:.2%}" if score is not None else "산출 불가"
    web = load["match"]["cpu_seconds"]["webview_cpu_seconds"]
    web_percent = load["match"]["average_host_capacity_percent"]["webview_cpu_seconds"]
    lines = ["# 브라우저 부하 제거 후 LMR 재대국 독립 감사", "",
             f"상태: {report['status']}. 검사 {report['checks']}개, 실패 {len(report['failed_checks'])}개.", "",
             f"새 대국: {w}승 {d}무 {loss}패, 완료 {match['completed']}판, 득점률 {score_text}. 미완료 {match['unfinished']}판은 무승부와 점수 분모에서 제외했다.", "",
             "기존 대국은 4승6무6패(43.75%)였다. 두 실행은 합산하지 않았으며 결과 차이를 브라우저 부하만의 인과 효과로 해석하지 않는다.", "",
             f"실행 전 {load['preflight']['covered_seconds']:.2f}초, 대국 중 {load['match']['covered_seconds']:.2f}초와 종료 후 {load['postflight']['covered_seconds']:.2f}초의 관측에서 일반 브라우저 프로세스 수와 CPU 증가가 모두 0인지 재계산했다. 채택 기준 통과: {load['computed_browser_load_criterion_passed']}.", "",
             f"별도 관측한 내장 WebView2는 대국 중 {web:.6f} CPU초, 전체 {report['logical_cpus']}논리CPU 용량의 평균 {web_percent:.6f}%였다. 해당 프로세스가 존재하므로 모든 웹 렌더러가 없었다고 표현하지 않는다.", "",
             "PGN 전체 합법 수·무승부 청구와 종료, 고정8시작점/선후공16판, 동일 실행 파일/소스 식별자와 옵션, 각 검색의 전체 이력·선택 수·노드·깊이·시간·LMR 기록을 대조했다. 이전 회귀검사와 고정 깊이 실험은 새로 실행하지 않았다.", "",
             "8개 고정 시작점의 소표본이다. 결과는 이 조건의 관측이며 일반적인 기력 향상·Elo를 확정하지 않는다. CPU 표본은 짧게 실행된/접근 불가 프로세스, GPU·온도·클럭·예약 작업을 완전히 포착하지 못한다.", ""]
    if report["failed_checks"]:
        lines += ["실패 항목:", "", *[f"- {value}" for value in report["failed_checks"]], ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--output-prefix", default="independent-idle-rematch-audit")
    args = parser.parse_args()
    evidence = args.evidence.expanduser().resolve()
    if not args.output_prefix or Path(args.output_prefix).name != args.output_prefix:
        parser.error("--output-prefix must be a filename stem")
    outputs = [evidence / (args.output_prefix + suffix) for suffix in (".json", ".md")]
    if any(path.exists() for path in outputs):
        parser.error("Use new output names; existing audits are never replaced")
    required = [evidence / name for name in
                ("match-200ms.json", "match-200ms.pgn", "match-200ms-lmr.json",
                 "preflight-load.json", "match-load.json", "run-match.ps1", "run-monitored.ps1")]
    if any(not path.is_file() or not path.stat().st_size for path in required):
        parser.error("Finished nonempty evidence including final match-load.json is required")
    prior = module_from_file("regicide_prior_lmr_audit", OLD_AUDIT)
    prior.HERE = evidence
    prior.ROOT = ROOT
    check, load = prior.check, prior.load
    data = load(evidence / "match-200ms.json")
    previous = load(OLD_DIRECTORY / "match-200ms.json")
    plan = subprocess.check_output(["git", "show", f"{PLAN_COMMIT}:{PLAN_PATH}"], cwd=ROOT)
    opening_bytes = subprocess.check_output(["git", "show", f"{PLAN_COMMIT}:{OPENING_PATH}"], cwd=ROOT)
    opening = json.loads(opening_bytes)
    check("fixed opening bytes at preregistered plan", hashlib.sha256(opening_bytes).hexdigest() == OPENING_SHA)
    check("plan includes all frozen source and executable identities",
          all(token.encode() in plan for token in (prior.CODE, prior.BASE, prior.CANDIDATE_SHA,
                                                   prior.BASE_SHA, OPENING_SHA)))
    check("harness ran preregistered plan commit", data["harness_checkout"]["commit"] == PLAN_COMMIT)
    check("unchanged opening source hash", data["opening_file_sha256"] == previous["opening_file_sha256"] == OPENING_SHA)
    check("unchanged complete opening document and selection", data["opening_document"] == previous["opening_document"]
          and data["selected_openings"] == previous["selected_openings"])
    check("source opening sequences and names exact", [(x["name"], x["moves"]) for x in data["selected_openings"]]
          == [(x["name"], x["moves"]) for x in opening["openings"]])
    check("same limits/schedule/claims", all(data[key] == previous[key] for key in
          ("limits", "max_plies", "draw_policy", "games_requested", "pair_order")))
    identities = []
    for role, expected_sha in (("baseline", prior.BASE_SHA), ("candidate", prior.CANDIDATE_SHA)):
        engine, old = data["engines"][role], previous["engines"][role]
        check("same engine bytes and options " + role, engine["sha256"] == old["sha256"] == expected_sha
              and engine["requested_options"] == old["requested_options"]
              and engine["validated_options"] == old["validated_options"]
              and engine["advertised_options"] == old["advertised_options"]
              and engine["engine_id"] == old["engine_id"])
        command_path = engine["command"][0]
        replacements = {"$REPO": str(ROOT), "$BASELINE_REPO": str(ROOT.parent / "Regicide")}
        for token, replacement in replacements.items():
            command_path = command_path.replace(token, replacement)
        binary = Path(command_path)
        observed = digest(binary) if binary.is_file() else None
        if observed is not None:
            check("observed executable bytes " + role, observed == expected_sha)
        identities.append({"role": role, "expected_sha256": expected_sha,
                           "observed_sha256": observed, "bytes_available": observed is not None})
    check("previous WDL retained separately", [previous["summary"][key] for key in
          ("candidate_wins", "candidate_draws", "candidate_losses")] == [4, 6, 6])
    match = prior.audit_match()
    check("all scheduled games attempted", len(data["games"]) == 16 and data["summary"]["unplayed_games"] == 0)
    pairs = []
    for index, item in enumerate(data["selected_openings"], 1):
        games = [game for game in data["games"] if game["pair"] == index]
        complete = len(games) == 2 and all(game["completed"] for game in games)
        points = sum(game["candidate_points"] for game in games) if complete else None
        pair = {"pair": index, "opening": item["name"], "games": [game["game"] for game in games],
                "completed": complete, "results": [game["result"] for game in games],
                "candidate_points": points, "candidate_score": points / 2 if complete else None}
        check("independent pair summary " + str(index), data["summary"]["pairs"][index - 1] == pair)
        pairs.append(pair)
    match["paired_openings"] = pairs
    match["recorded_descriptive_intervals"] = {key: data["summary"][key] for key in
        ("wilson95_descriptive", "wilson_interpretation", "paired_opening_bootstrap95_descriptive")}
    for role in ("baseline", "candidate"):
        moves = [move for game in data["games"] for move in game["search_moves"] if move["role"] == role]
        resource = data["summary"]["resources"][role]
        check("overall node/move resource total " + role,
              resource["searched_moves"] == len(moves)
              and resource["reported_nodes"] == sum(x["nodes"] for x in moves if x["nodes"] is not None))
        depths = [x["depth"] for x in moves if x["depth"] is not None]
        match.setdefault("search_depth_descriptive", {})[role] = {
            "searched_moves": len(moves), "reported_depth_moves": len(depths),
            "mean": statistics.mean(depths) if depths else None,
            "median": statistics.median(depths) if depths else None,
            "scope": "Final reported completed depth; a selective LMR depth is not equal work or strength."}
    pre = load(evidence / "preflight-load.json")
    monitor = load(evidence / "match-load.json")
    check("monitor identity", monitor["mode"] == "same-condition-idle-rematch"
          and monitor["logical_cpus"] == 16 and monitor["interval_target_ms"] == 500
          and monitor["exit_code"] == data["exit_code"]
          and monitor["standalone_browser_pattern"] == EXPECTED_BROWSER_PATTERN)
    check("preflight duration", pre["duration_seconds"] >= 20 and bool(pre["samples"]))
    initial = pre["initial_inventory"]
    initial_ok = initial["browser_processes"] == initial["regicide_processes"] == 0
    load_report = {}
    no_browser = initial_ok
    pre_ok = initial_ok
    cpu_fields = ("browser_cpu_seconds", "webview_cpu_seconds", "regicide_cpu_seconds",
                  "all_observed_cpu_seconds", "monitor_cpu_seconds")
    for phase, rows in (("preflight", pre["samples"]), ("match", monitor["samples"]),
                        ("postflight", monitor["postflight_samples"])):
        check("nonempty monitor phase " + phase, bool(rows))
        elapsed_prior = 0.0 if phase != "postflight" else monitor["samples"][-1]["elapsed_seconds"]
        for index, row in enumerate(rows):
            label = f"{phase} sample {index + 1}"
            check(label + " phase/time", row["phase"] == phase and row["interval_seconds"] > 0
                  and prior.near(row["elapsed_seconds"] - elapsed_prior, row["interval_seconds"]))
            elapsed_prior = row["elapsed_seconds"]
            check(label + " valid CPU", all(finite_nonnegative(row[key]) for key in cpu_fields))
            check(label + " valid process counts", all(isinstance(row[key], int) and row[key] >= 0 for key in
                  ("browser_processes", "webview_processes", "regicide_processes",
                   "unexpected_regicide_processes", "unreadable_cpu_processes")))
            browser_ok = row["browser_processes"] == 0 and row["browser_cpu_seconds"] == 0
            engine_ok = (row["regicide_processes"] <= 2 and row["unexpected_regicide_processes"] == 0
                         if phase == "match" else row["regicide_processes"] == 0)
            check(label + " browser zero", browser_ok)
            check(label + " no other Regicide", engine_ok)
            no_browser = no_browser and browser_ok and engine_ok
            if phase == "preflight":
                pre_ok = pre_ok and browser_ok and engine_ok
        seconds = sum(row["interval_seconds"] for row in rows)
        totals = {key: sum(row[key] for row in rows) for key in cpu_fields}
        load_report[phase] = {"samples": len(rows), "covered_seconds": seconds,
            "cpu_seconds": totals,
            "average_host_capacity_percent": {key: 100 * value / seconds / 16 for key, value in totals.items()},
            "maximum_process_counts": {key: max(row[key] for row in rows) for key in
                ("browser_processes", "webview_processes", "regicide_processes", "unexpected_regicide_processes")},
            "maximum_interval_webview_logical_cpu_equivalents": max(
                row["webview_cpu_seconds"] / row["interval_seconds"] for row in rows),
            "samples_with_unreadable_cpu": sum(row["unreadable_cpu_processes"] > 0 for row in rows)}
    check("postflight at least four samples", len(monitor["postflight_samples"]) >= 4)
    check("two comparison engines observed", load_report["match"]["maximum_process_counts"]["regicide_processes"] == 2)
    check("stored preflight acceptance recomputed", pre["passed"] == pre_ok and pre_ok)
    check("stored browser criterion recomputed", monitor["browser_load_criterion_passed"] == no_browser and no_browser)
    check("recording envelope", utc(monitor["started_utc"]) <= utc(data["started_at"])
          <= utc(monitor["finished_utc"]))
    sidecar = load(evidence / "match-200ms-lmr.json")
    check("telemetry finalized within monitor envelope", utc(sidecar["started_utc"]) >= utc(monitor["started_utc"])
          and utc(sidecar["finished_utc"]) <= utc(monitor["finished_utc"]))
    check("preflight precedes rematch", utc(pre["started_utc"]) + datetime.timedelta(seconds=pre["duration_seconds"])
          <= utc(monitor["started_utc"]) + datetime.timedelta(seconds=1))
    load_report["computed_browser_load_criterion_passed"] = no_browser
    report = {"schema": "regicide-independent-idle-rematch-audit-v1",
              "audit_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "plan_commit": PLAN_COMMIT, "plan_sha256_at_commit": hashlib.sha256(plan).hexdigest(),
              "opening_sha256": OPENING_SHA, "logical_cpus": 16,
              "checks": prior.CHECKS, "failed_checks": prior.FAILURES,
              "status": "passed_with_small_sample_limits" if not prior.FAILURES else "failed",
              "match": match, "load": load_report, "executable_observations": identities,
              "prior_run_separate": {"WDL": [4, 6, 6], "score": 0.4375,
                                     "scope": "Previous browser-loaded run; not pooled or used to claim a causal browser effect."},
              "limits": ["Eight fixed paired starts/16 scheduled games; no general strength or Elo conclusion.",
                         "No new rule regression or fixed-depth benchmark is claimed; exact prior binaries are reused.",
                         "Standalone browser zero is endpoint process/CPU observation, not an exclusive or fully idle host.",
                         "Embedded WebView2 is reported separately; other app renderers/GPU, thermal and clock effects are not ruled out.",
                         "Short-lived/exited/inaccessible processes can escape sampling; new browser/webview lifetime charging can overcount boundary CPU.",
                         "Telemetry line monotonicity relies on recorded capture flags; no separate complete raw UCI stream is retained.",
                         "A declared source commit plus executable hash is not proof of reproducible compilation.",
                         "Recorded intervals are descriptive runner outputs, not a universal statistical strength bound."],
              "audit_script_sha256": digest(Path(__file__)), "reused_audit_sha256": digest(OLD_AUDIT),
              "input_hash_scope": "Hashes identify this exact evidence copy. Public redaction changes evidence bytes, never declared executable identities.",
              "evidence_sha256": {str(path.relative_to(evidence)) if path.is_relative_to(evidence)
                                  else str(path.relative_to(ROOT)): digest(path) for path in sorted(prior.USED)}}
    for path, content in zip(outputs, (json.dumps(report, ensure_ascii=False, indent=2) + "\n", markdown(report))):
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
    print(json.dumps({"status": report["status"], "checks": report["checks"],
                      "failures": report["failed_checks"], "WDL": match["WDL"],
                      "browser_load_criterion_passed": no_browser}, ensure_ascii=False))
    return int(bool(prior.FAILURES))


if __name__ == "__main__":
    raise SystemExit(main())
