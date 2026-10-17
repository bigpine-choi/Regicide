"""Validate saved guard/event windows and summarize two separately audited sets.

No engine is started. Run after independent_match_audit.py has audited each set.
Development and held-out performance is never pooled.
"""

import datetime
import hashlib
import json
from pathlib import Path
import subprocess


ROOT = next(parent for parent in Path(__file__).resolve().parents
            if (parent / "CMakeLists.txt").is_file() and (parent / "src/board.cpp").is_file())
HERE = Path(__file__).resolve().parent
CANDIDATE = "7b796435fefa1ef197b29bd02a103e8c8d5ee8f5"


def load(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def timestamp(text):
    return datetime.datetime.fromisoformat(text)


def main():
    guard_path = HERE / "wake-guard.json"
    guard = load(guard_path)
    assert guard["candidate_commit"] == CANDIDATE
    assert guard["prior_state"] > 0
    assert guard["requested_state"] == "ES_CONTINUOUS | ES_SYSTEM_REQUIRED"
    assert guard["display_required"] is False and guard["persistent_power_policy_changed"] is False
    assert guard["restore_succeeded"] is True
    assert guard["restore_requested_state"] == guard["prior_state"] | 2147483648
    guard_start, guard_end = timestamp(guard["started"]), timestamp(guard["finished"])
    assert guard_start < guard_end
    assert len(guard["bundles"]) == 2
    wrapper = ROOT / "scripts/run_loss_guided_comparison.ps1"
    frozen_wrapper = subprocess.check_output(["git", "-C", str(ROOT), "show",
                                               CANDIDATE + ":scripts/run_loss_guided_comparison.ps1"])
    assert wrapper.read_bytes().replace(b"\r\n", b"\n") == frozen_wrapper.replace(b"\r\n", b"\n")

    query_path, events_path = HERE / "suspend-event-query.json", HERE / "suspend-events-window.json"
    query, events = load(query_path), load(events_path)
    assert query["log"] == "System" and query["provider"] == "Microsoft-Windows-Kernel-Power"
    assert query["ids"] == [506, 507] and query["result"] == "query succeeded"
    assert query["start"] == guard["started"] and query["end"] == guard["finished"]
    assert query["events"] == len(events) == 0
    assert timestamp(query["queried_at"]) >= guard_end

    conditions = []
    previous_end = guard_start
    for index, (name, role) in enumerate((("development100", "development"), ("heldout100", "heldout"))):
        bundle = guard["bundles"][index]
        assert bundle["name"] == name and bundle["exit_code"] == 0
        start, end = timestamp(bundle["started"]), timestamp(bundle["finished"])
        assert previous_end <= start < end <= guard_end
        previous_end = end
        source = HERE / (name + ".json")
        data = load(source)
        report_path = HERE / (name + ".independent-audit.json")
        report = load(report_path)
        assert report["status"] == "PASS" and report["dataset_role"] == role
        assert data["completed"] and data["exit_code"] == 0 and not data["errors"]
        match_start = timestamp(data["started_at"])
        match_end = match_start + datetime.timedelta(seconds=data["total_elapsed_seconds"])
        assert start <= match_start < match_end <= end
        assert report["completed_games"] == report["scheduled_games"] == 16
        assert not report["incomplete_games"] and report["unplayed_games"] == 0
        for values in report["statistics"].values():
            assert values["engine_over_effective_allowance_moves"] == 0
            assert values["wall_over_requested_budget_moves"] == 0
        conditions.append({"name": name, "dataset_role": role, "match_sha256": digest(source),
                           "pgn_sha256": digest(source.with_suffix(".pgn")),
                           "individual_audit_sha256": digest(report_path),
                           "bundle": bundle, "candidate_wdl": report["candidate_wdl"],
                           "candidate_score": report["candidate_score"],
                           "paired_opening_bootstrap95_descriptive": report["paired_opening_bootstrap95_descriptive"],
                           "completed_games": report["completed_games"],
                           "total_pgn_plies": report["total_pgn_plies"],
                           "engine_selected_plies": report["engine_selected_plies"],
                           "pv_plies": report["pv_plies"], "terminations": report["terminations"],
                           "statistics": report["statistics"], "source_checks": report["source_checks"],
                           "options_verified": report["options_verified"]})
    assert conditions[0]["source_checks"] == conditions[1]["source_checks"]
    assert conditions[0]["options_verified"] == conditions[1]["options_verified"]
    result = {"status": "PASS", "audited_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "candidate_commit": CANDIDATE,
              "guard_sha256": digest(guard_path), "guard": guard,
              "event_query_sha256": digest(query_path), "event_query": query,
              "events_sha256": digest(events_path), "events": events,
              "frozen_wrapper_sha256": digest(wrapper), "conditions": conditions,
              "legality_totals_only": {"games": 32,
                                       "actual_pgn_plies": sum(row["total_pgn_plies"] for row in conditions),
                                       "engine_selected_plies": sum(row["engine_selected_plies"] for row in conditions),
                                       "pv_plies": sum(row["pv_plies"] for row in conditions)},
              "interpretation": "Held-out and development results point in opposite directions. The new fixed held-out sample improved; development performance regressed. Do not pool the two WDL percentages or claim general strength/Elo improvement.",
              "scope": "Saved PGN/JSON, source and executable hashes, guard records and live System-event query verified. No independent engine-binary rebuild; no claim of hard real-time guarantees."}
    (HERE / "environment-and-results-audit.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = ["# 패배 분석 후보의 독립 감사", "",
             "두 위치 집합을 각각 독립 재생했다. 개발 집합과 보류 집합의 승·무·패 및 득점률을 합산하지 않는다.", "",
             "| 집합, 양쪽 요청 100ms/수 | 완료 | 후보 승·무·패 | 득점률 | 짝별 기술적 부트스트랩 참고 범위 | 종결 |",
             "| --- | ---: | --- | ---: | --- | --- |"]
    for row in conditions:
        label = "개발 위치 진단" if row["dataset_role"] == "development" else "새 보류 위치 주 비교"
        interval = row["paired_opening_bootstrap95_descriptive"]["interval"]
        terms = ", ".join(f"{key} {value}" for key, value in row["terminations"].items())
        lines.append(f'| {label} | 16/16 | {" / ".join(map(str, row["candidate_wdl"]))} | {100 * row["candidate_score"]:.3f}% | {100 * interval[0]:.3f}..{100 * interval[1]:.3f}% | {terms} |')
    lines += ["", "개발 위치에서는 37.5%로 이전 버전보다 낮았고, 새 보류 위치에서는 71.875%로 높았다. 서로 반대 방향의 결과이며 광범위한 경기력 향상이나 Elo를 입증하지 않는다. 부트스트랩은 8개 고정 시작 쌍을 20,000회 재표집한 기술적 참고이며 보편적인 강도의 신뢰구간이 아니다.", "",
              "| 집합 | 엔진 | 선택 수 | 평균 완료 깊이 | 평균 최대 탐색 깊이 | 평균 노드/수 | 평균 벽시계(ms) | 최대 벽시계(ms) | 요청 시간 초과 |",
              "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for row in conditions:
        label = "개발" if row["dataset_role"] == "development" else "보류"
        for role, values in row["statistics"].items():
            version = "v0.4.1" if role == "baseline" else "v0.5.0"
            lines.append(f'| {label} | {version} | {values["searched_moves"]} | {values["mean_depth"]:.6f} | {values["mean_seldepth"]:.6f} | {values["mean_nodes"]:.6f} | {values["mean_wall_time_ms"]:.6f} | {values["max_wall_time_ms"]:.3f} | {values["wall_over_requested_budget_moves"]} |')
    totals = result["legality_totals_only"]
    lines += ["", f'실제 기보 {totals["actual_pgn_plies"]:,}반수, 엔진 선택 {totals["engine_selected_plies"]:,}반수와 PV {totals["pv_plies"]:,}수를 합법 수로 재생했다. 합계는 검사량이며 경기 성적을 통합한 값이 아니다.', "",
              "모든 판 정상 종료, 오류·미완료·미실행은 0이다. 내부 검색 예산 80ms에 대한 보고 시간 초과와 요청 100ms에 대한 벽시계 초과는 두 집합 모두 0이다. 운영체제의 일반적인 실시간 보장을 뜻하지 않는다.", "",
              "후보의 평균 완료 깊이와 노드는 두 집합 모두 낮고 seldepth는 더 높다. 연장된 탐색과 실제 방문 위치가 달라지므로 완료 깊이만으로 판단 품질이나 동일 위치 실행 속도를 평가하지 않는다.", "",
              "실행 중 임시 ES_SYSTEM_REQUIRED가 요청됐고 이전 상태 복원 성공을 확인했다. 묶음은 개발 다음 보류로 겹치지 않게 진행됐다. System의 Microsoft-Windows-Kernel-Power 506/507을 전체 guard 구간 23:06:55.557..23:11:07.334(+09:00)에서 조회했고 해당 사건은 없었다. 영구 전원 정책과 화면 켜짐 요구는 바꾸지 않았다.", "",
              "각 시작 수순·전체 이력·선후공·수와 PV·FEN·현재/예정 수 무승부 청구·자원 합계·짝별 득점·부트스트랩을 별도 확인했다. 기준/후보의 관련 소스 각각 13개, 실행 파일 SHA256, 공통 기본 옵션과 새 두 옵션, 깨끗한 동결 체크아웃 기록을 대조했다. 소스와 바이너리 해시의 정합성을 확인했으며 실행 파일을 독립 재빌드한 검사는 아니다.", "",
              "세부 자료: `development100.independent-audit.json`, `heldout100.independent-audit.json`, `environment-and-results-audit.json`, `suspend-events-window.json`, `suspend-event-query.json`. 재생 도구는 `independent_match_audit.py`와 `audit_environment.py`다."]
    (HERE / "independent-audit-summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"status": "PASS", "events": len(events), "conditions": [
        {key: row[key] for key in ("name", "candidate_wdl", "candidate_score")} for row in conditions],
        "legality_totals_only": totals}))


if __name__ == "__main__":
    main()
