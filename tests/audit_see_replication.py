"""Replay the fresh SEE replication and its native CPU evidence; never run engines.

The two engine revisions and opening bytes are fixed independently of the harness
revision. Every attempt is inventoried, and the first clean block at each time
control is mandatory. Failed blocks remain visible and cannot supply games.
"""

import argparse
import copy
import datetime
import json
import math
from pathlib import Path
import random
import re
import sys
import tempfile

import chess

from audit_see_results import (BASELINE_COMMIT, ROLES, ROOT, audit_game, audit_pgn,
                               check_checkout, check_engines, digest, read_json,
                               require, resources_summary)

CANDIDATE_COMMIT = "0edc06e437b934d177fc1e9b0907600b6287be72"
OPENING_SHA256 = "e12daff0f80ba3e2a09b2c4e9105f5b897763a777d7a2ede46e40012d2003289"
BINARY_SHA256 = {
    "baseline": "c333ccfa7ec3efbaa7f52df4c007875e64f68d0816a847c1390aa3213d0e427b",
    "candidate": "2ba41c8303f266149323b55186f5c6a806c33f73d529d6911d6ca4b33bfa53db",
}
SEED, RESAMPLES = 20261019, 20000
CHECK_NAMES = {
    "native-tests", "native-search_tests", "native-evaluation_tests",
    "native-board_state_tests", "native-transposition_tests", "native-see_tests",
    "differential", "engine_integration", "game_runner_tests", "compare_engines_tests",
    "benchmark_search_tests", "human_play_tests", "handicap_play_tests",
    "game_archive_tests", "godot_bootstrap_tests", "godot_integration_tests",
    "handicap_godot_tests", "archive_godot_tests", "baseline-native-rules",
    "baseline-differential", "frozen-engine-source",
}


def utc(value):
    result = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(result.tzinfo is not None and result.utcoffset() == datetime.timedelta(),
            "timestamp must explicitly use UTC")
    return result


def numeric(value, label, minimum=0):
    require(isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= minimum, f"invalid {label}")
    return value


def identity(row):
    for field in ("pid", "creation_ticks"):
        require(isinstance(row[field], int) and not isinstance(row[field], bool)
                and row[field] > 0, f"invalid process {field}")
    return row["pid"], row["creation_ticks"]


def engine_name(name, names):
    name = name.casefold()
    return name in names or (name.endswith(".exe") and
                             (name.startswith("regicide") or name.startswith("stockfish")))


def checked_chain(row, root_identity):
    chain = row["ancestry_to_monitor"]
    require(isinstance(chain, list) and len(chain) >= 2 and len(chain) == len(set(chain)),
            "owned engine lacks a non-cyclic ancestry chain")
    require(chain[0] == row["pid"] and chain[1] == row["parent_pid"]
            and chain[-1] == root_identity[0], "owned engine ancestry does not reach monitor")
    require(row["creation_ticks"] >= root_identity[1], "owned engine predates monitor")
    evidence = row["ancestry_processes"]
    require(isinstance(evidence, list) and [item["pid"] for item in evidence] == chain,
            "ancestry evidence PID mismatch")
    for child, parent in zip(evidence, evidence[1:]):
        require(child["parent_pid"] == parent["pid"]
                and child["creation_ticks"] >= parent["creation_ticks"],
                "ancestry evidence creation/parent mismatch")
    require(identity(evidence[0]) == identity(row)
            and identity(evidence[-1]) == root_identity, "ancestry evidence root/engine mismatch")
    return chain


def replay_monitor(summary, rows, document, movetime, harness_commit):
    """Recompute external identity sets and native CPU deltas from every row."""
    require(summary["schema_version"] == 1, "wrong monitor schema")
    require(summary["poll_seconds"] == 0.5
            and summary["max_allowed_snapshot_gap_seconds"] == 2.0,
            "wrong CPU polling/gap policy")
    require(summary["external_cpu_tolerance_ticks"] == 0
            and summary["cpu_ticks_per_second"] == 10_000_000,
            "wrong native CPU tick policy")
    require(summary["new_or_disappearing_external_engine_is_failure"] is True,
            "external identity changes were allowed")
    require(summary["completed_idle_seconds"] >= 10, "missing ten-second idle interval")
    require(summary["contaminated"] is False and summary["valid_for_comparison"] is True
            and summary["observed_external_engine_cpu_increase"] is False,
            "monitor did not certify a clean block")
    require(summary["failure"] is None and summary["error"] is None and summary["exit_code"] == 0,
            "monitor has a failure or nonzero exit")
    require(summary["candidate_commit"] == CANDIDATE_COMMIT
            and summary["baseline_commit"] == BASELINE_COMMIT
            and summary["harness_commit"] == harness_commit
            and summary["movetime_ms"] == movetime, "monitor source/time mismatch")
    require(len(rows) == summary["snapshot_count"] and len(rows) >= 22,
            "monitor row count/idle coverage mismatch")
    require(rows[0]["phase"] == "initial" and rows[-1]["phase"] == "finished",
            "monitor start/finish rows missing")
    root_identity = (summary["monitor_root_pid"], summary["monitor_root_creation_ticks"])
    require(all(isinstance(value, int) and value > 0 for value in root_identity),
            "invalid monitor process identity")
    names = {name.casefold() for name in summary["engine_names"]}
    require({"regicide.exe", "stockfish.exe", "lc0.exe", "lc0-cpu.exe"} <= names,
            "required known engine names not monitored")
    transports = summary["transport_engines"]
    require(set(transports) == set(ROLES), "not exactly two role-labelled UCI transports")
    registered = {identity(item): role for role, item in transports.items()}
    require(len(registered) == 2, "both roles claim one UCI transport")
    owned = {identity(item): item for item in summary["owned_engines"]}
    require(len(summary["owned_engines"]) == 2 and set(owned) == set(registered),
            "excluded owned engines differ from actual UCI transports")
    for process_identity, role in registered.items():
        item = transports[role]
        require(item["command"] == document["engines"][role]["command"],
                "transport command differs from role binary")
        require(Path(item["command"][0]).name.casefold() == item["name"].casefold(),
                "transport process name differs from executable")
        checked_chain(item, root_identity)
        checked_chain(owned[process_identity], root_identity)
        for field in ("pid", "creation_ticks", "parent_pid", "name", "ancestry_to_monitor", "ancestry_processes"):
            require(item[field] == owned[process_identity][field], "transport/owned identity mismatch")
    first_external = None
    previous = None
    seen_owned = set()
    cpu_total = 0
    max_gap = 0.0
    max_duration = 0.0
    first_utc = utc(rows[0]["utc"])
    previous_utc = None
    previous_elapsed = None
    idle_complete = None
    phases = []
    for index, row in enumerate(rows):
        require(row["contaminated"] is False, "contaminated monitor row")
        elapsed = numeric(row["elapsed_seconds"], "snapshot monotonic elapsed")
        duration = numeric(row["snapshot_duration_seconds"], "snapshot duration")
        require(duration <= 2.0, "native snapshot itself exceeded the fail-closed bound")
        max_duration = max(max_duration, duration)
        timestamp = utc(row["utc"])
        gap = 0.0 if previous_elapsed is None else elapsed - previous_elapsed
        require(gap >= 0 and gap <= 2.0, "nonmonotonic or excessive snapshot gap")
        require(abs(gap - numeric(row["gap_seconds"], "snapshot gap")) <= 1e-7,
                "reported snapshot gap differs from monotonic evidence")
        require(previous_utc is None or timestamp >= previous_utc, "UTC snapshot order reversed")
        require(abs((timestamp - first_utc).total_seconds() - (elapsed - rows[0]["elapsed_seconds"])) < 2,
                "UTC and monotonic coverage diverged")
        max_gap = max(max_gap, gap)
        previous_elapsed, previous_utc = elapsed, timestamp
        current = {}
        row_identities = set()
        for process in row["processes"]:
            require(not process.get("error"), "unmeasurable engine process in raw monitor")
            process_identity = identity(process)
            require(process_identity not in row_identities, "duplicate process identity in snapshot")
            row_identities.add(process_identity)
            require(engine_name(process["name"], names), "raw snapshot includes an undeclared process")
            ticks = process["cpu_ticks"]
            require(isinstance(ticks, int) and not isinstance(ticks, bool) and ticks >= 0,
                    "invalid native process CPU counter")
            require(type(process["owned"]) is bool, "invalid owned process flag")
            require(type(process["exited_during_snapshot"]) is bool, "invalid process exit observation")
            if process["owned"]:
                require(process_identity in registered, "claimed owned process is not an actual UCI transport")
                if process_identity not in seen_owned:
                    checked_chain(process, root_identity)
                elif process["ancestry_to_monitor"] is not None:
                    checked_chain(process, root_identity)
                seen_owned.add(process_identity)
            else:
                require(process_identity not in registered, "actual transport labelled external")
                require(process["exited_during_snapshot"] is False,
                        "external engine exited during the native snapshot")
                require(process["ancestry_to_monitor"] is None, "external process claims monitor ancestry")
                require(process["ancestry_processes"] is None, "external process claims ancestor evidence")
                current[process_identity] = ticks
        if first_external is None:
            first_external = dict(current)
            require(not seen_owned, "a measured engine existed before the idle interval")
        require(set(current) == set(first_external), "external engine identity set changed")
        delta = 0
        if previous is not None:
            for process_identity, ticks in current.items():
                require(ticks >= previous[process_identity], "external CPU counter decreased")
                delta += ticks - previous[process_identity]
        require(delta == row["external_cpu_delta_ticks"], "raw external CPU delta was misreported")
        require(delta == 0, "external engine consumed native CPU ticks")
        cpu_total += delta
        previous = current
        phases.append(row["phase"])
        if row["phase"] == "idle-complete":
            require(idle_complete is None and not seen_owned, "idle phase repeated or engines launched too early")
            idle_complete = row
    require(idle_complete is not None and idle_complete["elapsed_seconds"] - rows[0]["elapsed_seconds"] >= 10,
            "raw evidence does not cover the required idle interval")
    require(seen_owned == set(registered), "a registered UCI transport never appeared in raw evidence")
    expected_external = {(item["pid"], item["creation_ticks"]): item["initial_cpu_ticks"]
                         for item in summary["external_engine_identities"]}
    require(len(expected_external) == len(summary["external_engine_identities"])
            and expected_external == first_external, "summary external identities/counters differ from first row")
    require(cpu_total == summary["external_cpu_increase_ticks"] == 0, "summary CPU tick total mismatch")
    require(abs(max_gap - summary["max_snapshot_gap_seconds"]) <= 1e-7,
            "summary maximum snapshot gap mismatch")
    require(abs(max_duration - summary["max_snapshot_duration_seconds"]) <= 1e-7,
            "summary maximum snapshot duration mismatch")
    before, after = phases.count("before-game"), phases.count("after-game")
    require(before == after == 16, "monitor does not cover all sixteen games")
    game_phases = [phase for phase in phases if phase in {"before-game", "after-game"}]
    require(game_phases == [phase for _ in range(16) for phase in ("before-game", "after-game")],
            "game guard phase order differs from serial play")
    match_start = utc(document["started_at"])
    match_end = match_start + datetime.timedelta(seconds=document["total_elapsed_seconds"])
    require(utc(summary["started_utc"]) <= first_utc <= utc(idle_complete["utc"]) <= match_start
            and match_end <= utc(rows[-1]["utc"]) <= utc(summary["finished_utc"]),
            "monitor does not cover the entire match including engine launch/cleanup")
    return {"status": "PASS", "snapshots": len(rows), "external_processes": len(first_external),
            "external_cpu_increase_ticks": cpu_total, "max_snapshot_gap_seconds": max_gap,
            "max_snapshot_duration_seconds": max_duration,
            "observed_idle_seconds": idle_complete["elapsed_seconds"] - rows[0]["elapsed_seconds"],
            "owned_transport_identities": {role: list(identity(item)) for role, item in transports.items()},
            "scope": summary["scope"]}


def paired_bootstrap(scores):
    generator = random.Random(SEED)
    samples = sorted(sum(generator.choices(scores, k=len(scores))) / len(scores) for _ in range(RESAMPLES))
    return {"seed": SEED, "resamples": RESAMPLES, "pairs": len(scores),
            "interval": [samples[int(0.025 * (RESAMPLES - 1))], samples[int(0.975 * (RESAMPLES - 1))]],
            "scope": "Descriptive fixed-opening paired percentile bootstrap, not universal strength or Elo."}


def audit_validation(path):
    data = read_json(path)
    require(data["all_passed"] is True and data["binary_sha256"] == BINARY_SHA256
            and data["engine_source_commit"] == CANDIDATE_COMMIT, "wrong validation source/binary/result")
    checks = data["checks"]
    require(data["checks_count"] == len(checks) == 21
            and {item["name"] for item in checks} == CHECK_NAMES,
            "validation does not contain all twenty-one required checks")
    for item in checks:
        require(item["result"] == "passed" and item["exit_code"] == 0, "a validation check failed")
        log = Path(item["log"])
        if not log.is_absolute():
            log = ROOT / log
        require(log.is_file() and digest(log) == item["sha256"], "validation log hash mismatch")
    require(data["candidate_native_groups"] == 94 and data["candidate_native_assertions"] == 44813,
            "native validation coverage changed")
    require(data["skipped_case_count"] == 1
            and sum(item["skipped_cases"] for item in checks) == 1
            and next(item for item in checks if item["name"] == "game_archive_tests")["skipped_cases"] == 1,
            "unexpected additional skipped validation cases")
    return {"status": "PASS", "input_json_sha256": digest(path), "checks": 21,
            "native_groups": 94, "native_assertions": 44813,
            "completed_utc": data["completed_utc"], "skipped_case_count": 1, "skip_note": data["skip_note"]}


def audit_match(path, harness_commit, movetime):
    document = read_json(path)
    require(document["completed"] is True and document["exit_code"] == 0 and not document["errors"]
            and document["valid_for_comparison"] is True, "match did not complete cleanly")
    check_checkout(document["harness_checkout"], harness_commit)
    require(document["harness_source_commit"] == harness_commit, "wrong independent harness source")
    engines = check_engines(document["engines"], CANDIDATE_COMMIT)
    require({role: item["sha256"] for role, item in engines.items()} == BINARY_SHA256,
            "match executable hash differs from predeclared frozen binary")
    require(document["limits"] == {"nodes": None, "depth": None, "movetime_ms": movetime}, "wrong time limit")
    require(document["max_plies"] == 500 and document["draw_policy"] == "claim", "wrong adjudication limits")
    opening_file = ROOT / "tests/data/see_replication_openings_2026-10-18.json"
    require(document["opening_file_sha256"] == digest(opening_file) == OPENING_SHA256,
            "fresh opening bytes changed")
    expected = read_json(opening_file)["openings"]
    require(len(expected) == len(document["selected_openings"]) == 8
            and document["games_requested"] == len(document["games"]) == 16, "wrong complete match schedule")
    points, pair_scores, terminations = [], [], {}
    resources = {role: [] for role in ROLES}
    total_plies = pv_plies = 0
    for pair, opening in enumerate(expected, 1):
        selected = document["selected_openings"][pair - 1]
        require(selected["name"] == opening["name"] and selected["moves"] == opening["moves"],
                "selected fresh opening mismatch")
        paired = []
        for offset in (0, 1):
            number = (pair - 1) * 2 + offset + 1
            record = document["games"][number - 1]
            require(record["game"] == number and record["pair"] == pair
                    and record["opening"] == opening["name"] and record["opening_uci_moves"] == opening["moves"],
                    "fresh opening or game schedule mismatch")
            require(record["white"] == ("candidate" if offset == 0 else "baseline")
                    and record["black"] == ("baseline" if offset == 0 else "candidate"), "colors not swapped")
            result = audit_game(record)
            points.append(result["points"])
            paired.append(result["points"])
            total_plies += result["plies"]
            pv_plies += result["pv_plies"]
            terminations[result["termination"]] = terminations.get(result["termination"], 0) + 1
            for role in ROLES:
                resources[role].extend(result["resources"][role])
        pair_scores.append(sum(paired) / 2)
    counts = {"candidate_wins": points.count(1.0), "candidate_draws": points.count(0.5),
              "candidate_losses": points.count(0.0), "candidate_points": sum(points),
              "candidate_score": sum(points) / 16, "completed_games": 16,
              "incomplete_games": 0, "unplayed_games": 0}
    for key, value in counts.items():
        require(document["summary"][key] == value, f"harness summary mismatch: {key}")
    require(audit_pgn(path.with_suffix(".pgn"), document) == 16, "PGN game count mismatch")
    monitor_path = path.with_name(path.stem + ".monitor.json")
    log_path = path.with_name(path.stem + ".monitor.jsonl")
    summary = read_json(monitor_path)
    require(document["external_engine_guard"] == summary, "embedded guard summary differs from sidecar")
    require(summary["monitor_log_sha256"] == digest(log_path), "monitor JSONL hash mismatch")
    rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8-sig").splitlines()]
    monitor = replay_monitor(summary, rows, document, movetime, harness_commit)
    return {"status": "PASS", "source": str(path), "input_json_sha256": digest(path),
            "input_pgn_sha256": digest(path.with_suffix(".pgn")),
            "monitor_json_sha256": digest(monitor_path), "monitor_jsonl_sha256": digest(log_path),
            "movetime_ms": movetime, "engines": engines, **counts, "actual_plies": total_plies,
            "pv_plies": pv_plies, "terminations": terminations, "pair_scores": pair_scores,
            "paired_opening_bootstrap95": paired_bootstrap(pair_scores),
            "harness_bootstrap_preserved": document["summary"]["paired_opening_bootstrap95_descriptive"],
            "monitor": monitor,
            "resources": {role: resources_summary(resources[role], movetime) for role in ROLES}}


def inventory_attempts(root, selected, harness_commit, validation_sha):
    attempts = sorted(path for path in root.iterdir() if path.is_dir() and re.fullmatch(r"attempt-[0-9]{3}", path.name))
    require(attempts and selected == attempts[-1], "audit must inventory through the latest attempt")
    require([path.name for path in attempts] == [f"attempt-{index:03}" for index in range(1, len(attempts) + 1)],
            "missing attempt numbers")
    adopted = {}
    inventory = []
    prior_end = None
    for directory in attempts:
        state_path = directory / "run-state.json"
        state = read_json(state_path)
        require(state["attempt"] == directory.name and state["candidate_commit"] == CANDIDATE_COMMIT
                and state["baseline_commit"] == BASELINE_COMMIT and state["harness_commit"] == harness_commit,
                "attempt source identity mismatch")
        require(state["binary_sha256"] == BINARY_SHA256 and state["opening_sha256"] == OPENING_SHA256
                and state["validation_sha256"] == validation_sha, "attempt frozen input fingerprint mismatch")
        require(state["sequential_workloads"] is True and state["external_engine_poll_seconds"] == 0.5
                and state["external_engine_busy_delta_ticks"] == 0 and state["idle_required_seconds"] == 10,
                "attempt resource policy changed")
        require(state["new_or_disappearing_external_engine_is_failure"] is True
                and state["entire_current_time_block_invalid_on_contamination"] is True,
                "attempt contamination policy changed")
        require(state["display_required"] is False and state["persistent_power_policy_changed"] is False
                and state["prior_wake_state"] != 0 and state["restore_succeeded"] is True,
                "temporary wake state was not restored")
        started, finished = utc(state["started_utc"]), utc(state["finished_utc"])
        require(started <= finished and (prior_end is None or prior_end <= started), "attempts overlap")
        cursor = started
        steps = []
        names = [step["name"] for step in state["steps"]]
        require(names == [name for name in ("time100", "time1000") if name in names] and names,
                "repeated or reordered time blocks in one attempt")
        for step in state["steps"]:
            name = step["name"]
            require(name not in adopted, "a completed clean time block was rerun")
            beginning, end = utc(step["started_utc"]), utc(step["finished_utc"])
            require(cursor <= beginning <= end <= finished, "time blocks overlap or exceed attempt")
            cursor = end
            monitor_path = directory / (name + ".monitor.json")
            monitor = read_json(monitor_path)
            extensions = ("json", "pgn", "monitor.json", "monitor.jsonl", "log", "stderr.log")
            artifacts = {name + "." + extension: directory / (name + "." + extension)
                         for extension in extensions if (directory / (name + "." + extension)).is_file()}
            require(set(step["artifact_sha256"]) == set(artifacts), "runner artifact fingerprint set mismatch")
            for filename, artifact in artifacts.items():
                require(step["artifact_sha256"][filename] == digest(artifact), "runner artifact hash mismatch")
            if step["valid_for_comparison"]:
                require(len(artifacts) == 6, "a clean block lacks one of its six raw artifacts")
            raw_log = directory / (name + ".monitor.jsonl")
            require(monitor["monitor_log_sha256"] == digest(raw_log), "attempt monitor log hash mismatch")
            require(monitor["harness_commit"] == harness_commit and monitor["candidate_commit"] == CANDIDATE_COMMIT
                    and monitor["baseline_commit"] == BASELINE_COMMIT
                    and monitor["movetime_ms"] == int(name[4:]), "attempt monitor source/time mismatch")
            require(step["contaminated"] is monitor["contaminated"]
                    and step["valid_for_comparison"] is monitor["valid_for_comparison"]
                    and step["exit_code"] == monitor["exit_code"], "step/monitor result mismatch")
            require(beginning <= utc(monitor["started_utc"]) <= utc(monitor["finished_utc"]) <= end,
                    "monitor escapes runner step coverage")
            item = {"name": name, "exit_code": step["exit_code"], "contaminated": step["contaminated"],
                    "valid_for_comparison": step["valid_for_comparison"],
                    "monitor_json_sha256": digest(monitor_path), "monitor_jsonl_sha256": digest(raw_log),
                    "failure": monitor["failure"]}
            if step["valid_for_comparison"]:
                require(step["exit_code"] == 0 and step["contaminated"] is False, "invalid clean step")
                adopted[name] = directory / (name + ".json")
                item["adopted"] = True
            else:
                require(step["contaminated"] is True and monitor["failure"] is not None
                        and monitor["failure"]["retry_allowed"] is True
                        and monitor["failure"]["kind"] in {"external_cpu", "external_identity_change", "sampling_gap"},
                        "retry is allowed only for documented external contamination")
                item["adopted"] = False
                partial_path = directory / (name + ".json")
                if partial_path.exists():
                    partial = read_json(partial_path)
                    item["partial_match_json_sha256"] = digest(partial_path)
                    item["excluded_partial_summary"] = partial.get("summary")
            steps.append(item)
        references = state["adopted_blocks"]
        require(set(references) == set(adopted), "runner adopted block reference set mismatch")
        for name, reference in references.items():
            require((directory / reference).resolve() == adopted[name].resolve(),
                    "runner adopted a later, failed or unrelated block")
        if directory != selected:
            require(state["contaminated"] is True and state["exit_code"] != 0 and state["error"] is not None,
                    "a prior clean/undocumented attempt was abandoned")
        else:
            require(state["exit_code"] == 0 and state["error"] is None and state["contaminated"] is False,
                    "latest runner did not complete cleanly")
        prior_end = finished
        inventory.append({"attempt": directory.name, "run_state_sha256": digest(state_path),
                          "exit_code": state["exit_code"], "contaminated": state["contaminated"],
                          "temporary_wake_restored": True, "steps": steps})
    require(set(adopted) == {"time100", "time1000"}, "both first clean time blocks are required")
    return adopted, inventory


def markdown(result):
    lines = ["# SEE 신규 시작 위치 재현실험 독립 감사", "", "전체 판정: **PASS**.", "",
             "| 조건 | SEE 승·무·패 | 득점률 | 시작 쌍 참고 95% 구간 |", "|---|---:|---:|---:|"]
    for match in result["matches"]:
        low, high = match["paired_opening_bootstrap95"]["interval"]
        lines.append(f"| {match['movetime_ms']}ms | {match['candidate_wins']}·{match['candidate_draws']}·{match['candidate_losses']} "
                     f"| {match['candidate_score']:.3%} | {low:.3%} ~ {high:.3%} |")
    lines += ["", "신규 8개 고정 시작 쌍의 기술적 참고 구간입니다. seed 20261019, 20,000회이며 보편적인 강도나 Elo 구간이 아닙니다.",
              "두 엔진의 저장된 수와 PV, 기보, 바이너리·검증 로그 해시 및 모든 CPU 감시 행을 재검산했습니다.",
              "알려진 외부 엔진의 관측 CPU 증가는 0 native ticks입니다. 표본 사이에 생겼다가 사라진 프로세스나 일반 PC 부하까지 배제하지는 못합니다.", "",
              "| 시도 | 종료 코드 | 오염 | 채택한 시간 조건 |", "|---|---:|---|---|"]
    for item in result["attempts"]:
        blocks = ", ".join(step["name"] for step in item["steps"] if step["adopted"]) or "없음"
        lines.append(f"| {item['attempt']} | {item['exit_code']} | {item['contaminated']} | {blocks} |")
    return "\n".join(lines) + "\n"


def self_test():
    origin = datetime.datetime(2026, 10, 18, tzinfo=datetime.timezone.utc)
    stamp = lambda seconds: (origin + datetime.timedelta(seconds=seconds)).isoformat()
    owned = [{"pid": pid, "creation_ticks": pid, "parent_pid": 1, "name": "regicide.exe",
              "ancestry_to_monitor": [pid, 1], "ancestry_processes": [
                  {"pid": pid, "parent_pid": 1, "creation_ticks": pid},
                  {"pid": 1, "parent_pid": 0, "creation_ticks": 1}]} for pid in (2, 3)]
    transports = {role: dict(item, command=[f"/{role}/regicide.exe", "uci"])
                  for role, item in zip(ROLES, owned)}
    rows = []
    for index in range(64):
        phase = "initial" if index == 0 else "finished" if index == 63 else "idle-complete" if index == 20 else "running"
        if 22 <= index < 54:
            phase = "before-game" if index % 2 == 0 else "after-game"
        processes = [{"pid": 10, "creation_ticks": 5, "parent_pid": 9, "name": "stockfish.exe",
                      "cpu_ticks": 42, "owned": False, "exited_during_snapshot": False,
                      "ancestry_to_monitor": None, "ancestry_processes": None}]
        if 21 <= index < 62:
            processes.extend(dict(item, cpu_ticks=index, owned=True, exited_during_snapshot=False) for item in owned)
        rows.append({"utc": stamp(index * 0.5), "elapsed_seconds": index * 0.5,
                     "phase": phase, "gap_seconds": 0 if index == 0 else 0.5,
                     "snapshot_duration_seconds": 0.01,
                     "processes": processes, "external_cpu_delta_ticks": 0, "contaminated": False})
    summary = {"schema_version": 1, "started_utc": stamp(-0.1), "finished_utc": stamp(32),
               "monitor_root_pid": 1, "monitor_root_creation_ticks": 1, "poll_seconds": 0.5,
               "max_allowed_snapshot_gap_seconds": 2.0, "cpu_ticks_per_second": 10_000_000,
               "external_cpu_tolerance_ticks": 0, "completed_idle_seconds": 10,
               "contaminated": False, "valid_for_comparison": True,
               "observed_external_engine_cpu_increase": False, "failure": None, "error": None, "exit_code": 0,
               "candidate_commit": CANDIDATE_COMMIT, "baseline_commit": BASELINE_COMMIT,
               "harness_commit": "a" * 40, "movetime_ms": 100, "snapshot_count": len(rows),
               "engine_names": ["regicide.exe", "stockfish.exe", "lc0.exe", "lc0-cpu.exe"],
               "new_or_disappearing_external_engine_is_failure": True, "owned_engines": owned,
               "transport_engines": transports, "external_cpu_increase_ticks": 0,
               "max_snapshot_gap_seconds": 0.5,
               "max_snapshot_duration_seconds": 0.01,
               "external_engine_identities": [{"pid": 10, "creation_ticks": 5, "initial_cpu_ticks": 42}],
               "scope": "synthetic pure-helper test"}
    document = {"engines": {role: {"command": item["command"]} for role, item in transports.items()},
                "started_at": stamp(10.1), "total_elapsed_seconds": 20}
    require(replay_monitor(summary, rows, document, 100, "a" * 40)["external_cpu_increase_ticks"] == 0,
            "clean synthetic monitor rejected")
    failures = []
    def reject(label, alter):
        state, snapshots, match = copy.deepcopy((summary, rows, document))
        alter(state, snapshots, match)
        try:
            replay_monitor(state, snapshots, match, 100, "a" * 40)
        except (ValueError, KeyError):
            failures.append(label)
        else:
            raise AssertionError("audit accepted corruption: " + label)
    reject("external CPU tick", lambda s, r, m: r[30]["processes"][0].update(cpu_ticks=43))
    reject("new PID", lambda s, r, m: r[30]["processes"].append(dict(r[30]["processes"][0], pid=11)))
    reject("disappearing PID", lambda s, r, m: r[30]["processes"].pop(0))
    reject("PID reuse", lambda s, r, m: r[30]["processes"][0].update(creation_ticks=6))
    reject("external exit within final snapshot", lambda s, r, m: r[-1]["processes"][0].update(exited_during_snapshot=True))
    reject("claimed owned lie", lambda s, r, m: r[30]["processes"][0].update(owned=True, ancestry_to_monitor=[10, 1]))
    reject("third owned engine", lambda s, r, m: s["owned_engines"].append(dict(owned[0], pid=4)))
    reject("wrong transport role", lambda s, r, m: s["transport_engines"]["candidate"].update(command=["/baseline/regicide.exe", "uci"]))
    reject("wrong monitor ancestry", lambda s, r, m: s["owned_engines"][0].update(ancestry_to_monitor=[2, 9]))
    reject("ancestor PID reuse", lambda s, r, m: s["owned_engines"][0]["ancestry_processes"][-1].update(creation_ticks=2))
    reject("oversized gap", lambda s, r, m: r[30].update(elapsed_seconds=20))
    reject("forged gap", lambda s, r, m: r[30].update(gap_seconds=0))
    reject("slow last snapshot", lambda s, r, m: r[-1].update(snapshot_duration_seconds=2.1))
    reject("short idle", lambda s, r, m: s.update(completed_idle_seconds=9.9))
    reject("missing row", lambda s, r, m: r.pop(30))
    reject("UTC reversal", lambda s, r, m: r[30].update(utc=stamp(1)))
    reject("incomplete match coverage", lambda s, r, m: m.update(total_elapsed_seconds=30))
    reject("forged CPU summary", lambda s, r, m: s.update(external_cpu_increase_ticks=1))
    reject("dirty row", lambda s, r, m: r[30].update(contaminated=True))
    reject("missing game guard", lambda s, r, m: r[30].update(phase="running"))
    with tempfile.TemporaryDirectory() as temporary:
        sample = Path(temporary) / "log.txt"
        sample.write_bytes(b"validated\n")
        recorded = digest(sample)
        checks = [{"name": name, "result": "passed", "log": str(sample), "sha256": recorded,
                   "exit_code": 0, "skipped_cases": int(name == "game_archive_tests")} for name in sorted(CHECK_NAMES)]
        validation = {"all_passed": True, "binary_sha256": BINARY_SHA256,
                      "engine_source_commit": CANDIDATE_COMMIT, "checks": checks, "checks_count": 21,
                      "candidate_native_groups": 94, "candidate_native_assertions": 44813,
                      "skipped_case_count": 1, "completed_utc": stamp(0), "skip_note": "synthetic"}
        validation_path = Path(temporary) / "validation.json"
        validation_path.write_text(json.dumps(validation), encoding="utf-8")
        require(audit_validation(validation_path)["checks"] == 21, "valid regression summary rejected")
        sample.write_bytes(b"changed\n")
        try:
            audit_validation(validation_path)
        except ValueError:
            pass
        else:
            raise AssertionError("tampered validation log hash accepted")
        attempt_root = Path(temporary) / "attempts"
        attempt_root.mkdir()
        synthetic = []
        for number, beginning, end, contaminated, codes in (
                (1, 0, 40, True, [("time100", 1, 20, True), ("time1000", 21, 39, False)]),
                (2, 41, 70, False, [("time1000", 42, 69, True)])):
            attempt = attempt_root / f"attempt-{number:03}"
            attempt.mkdir()
            steps = []
            for name, start, finish, valid in codes:
                raw = attempt / (name + ".monitor.jsonl")
                raw.write_text("{}\n", encoding="utf-8")
                monitor = {"started_utc": stamp(start + 0.1), "finished_utc": stamp(finish - 0.1),
                           "contaminated": not valid, "valid_for_comparison": valid,
                           "exit_code": 0 if valid else 2,
                           "failure": None if valid else {"kind": "external_cpu", "retry_allowed": True, "reason": "synthetic"},
                           "monitor_log_sha256": digest(raw), "harness_commit": "a" * 40,
                           "candidate_commit": CANDIDATE_COMMIT, "baseline_commit": BASELINE_COMMIT,
                           "movetime_ms": int(name[4:])}
                (attempt / (name + ".monitor.json")).write_text(json.dumps(monitor), encoding="utf-8")
                for extension in ("json", "pgn", "log", "stderr.log"):
                    (attempt / (name + "." + extension)).write_text("{}\n", encoding="utf-8")
                artifacts = {name + "." + extension: digest(attempt / (name + "." + extension))
                             for extension in ("json", "pgn", "monitor.json", "monitor.jsonl", "log", "stderr.log")}
                steps.append({"name": name, "started_utc": stamp(start), "finished_utc": stamp(finish),
                              "contaminated": not valid, "valid_for_comparison": valid, "exit_code": 0 if valid else 2,
                              "artifact_sha256": artifacts})
            state = {"attempt": attempt.name, "candidate_commit": CANDIDATE_COMMIT,
                     "baseline_commit": BASELINE_COMMIT, "harness_commit": "a" * 40,
                     "binary_sha256": BINARY_SHA256, "opening_sha256": OPENING_SHA256,
                     "validation_sha256": "b" * 64, "sequential_workloads": True,
                     "external_engine_poll_seconds": 0.5, "external_engine_busy_delta_ticks": 0,
                     "idle_required_seconds": 10, "new_or_disappearing_external_engine_is_failure": True,
                     "entire_current_time_block_invalid_on_contamination": True, "display_required": False,
                     "persistent_power_policy_changed": False, "prior_wake_state": 1, "restore_succeeded": True,
                     "started_utc": stamp(beginning), "finished_utc": stamp(end), "steps": steps,
                     "contaminated": contaminated, "exit_code": 2 if contaminated else 0,
                     "error": "synthetic external CPU" if contaminated else None,
                     "adopted_blocks": {"time100": "time100.json"} if number == 1 else {
                         "time100": "../attempt-001/time100.json", "time1000": "time1000.json"}}
            (attempt / "run-state.json").write_text(json.dumps(state), encoding="utf-8")
            synthetic.append((attempt, state))
        require(len(inventory_attempts(attempt_root, synthetic[-1][0], "a" * 40, "b" * 64)[1]) == 2,
                "preserved dirty attempt and first clean block adoption rejected")
        state = copy.deepcopy(synthetic[1][1])
        state["adopted_blocks"]["time1000"] = "../attempt-001/time1000.json"
        (synthetic[1][0] / "run-state.json").write_text(json.dumps(state), encoding="utf-8")
        try:
            inventory_attempts(attempt_root, synthetic[-1][0], "a" * 40, "b" * 64)
        except ValueError:
            pass
        else:
            raise AssertionError("dirty block adoption accepted")
        (synthetic[1][0] / "run-state.json").write_text(json.dumps(synthetic[1][1]), encoding="utf-8")
        state = copy.deepcopy(synthetic[1][1])
        state["steps"].insert(0, synthetic[0][1]["steps"][0])
        (synthetic[1][0] / "run-state.json").write_text(json.dumps(state), encoding="utf-8")
        try:
            inventory_attempts(attempt_root, synthetic[-1][0], "a" * 40, "b" * 64)
        except ValueError:
            pass
        else:
            raise AssertionError("rerun of first clean time block accepted")
        (synthetic[1][0] / "run-state.json").write_text(json.dumps(synthetic[1][1]), encoding="utf-8")
        bad_monitor = synthetic[0][0] / "time1000.monitor.json"
        data = read_json(bad_monitor)
        data["failure"].update(kind="ownership_error", retry_allowed=False)
        bad_monitor.write_text(json.dumps(data), encoding="utf-8")
        state = copy.deepcopy(synthetic[0][1])
        state["steps"][1]["artifact_sha256"]["time1000.monitor.json"] = digest(bad_monitor)
        (synthetic[0][0] / "run-state.json").write_text(json.dumps(state), encoding="utf-8")
        try:
            inventory_attempts(attempt_root, synthetic[-1][0], "a" * 40, "b" * 64)
        except ValueError:
            pass
        else:
            raise AssertionError("retry of non-external failure accepted")
    require(paired_bootstrap([0.5] * 8)["interval"] == [0.5, 0.5], "bootstrap helper failed")
    print(f"PASS: clean monitor, {len(failures)} corrupt-monitor rejections, validation log tamper rejection, "
          "preserved-attempt adoption and 3 rejection cases, bootstrap; no engine launched")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", nargs="?", type=Path, default=ROOT / "build/see-replication/attempt-001")
    parser.add_argument("--harness-commit")
    parser.add_argument("--candidate-commit", default=CANDIDATE_COMMIT)
    parser.add_argument("--validation-summary", type=Path, default=ROOT / "build/see-clean-evidence/validation-summary.json")
    parser.add_argument("--attemptroot", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        self_test()
        return 0
    if args.candidate_commit != CANDIDATE_COMMIT or not args.harness_commit or not re.fullmatch(r"[0-9a-f]{40}", args.harness_commit):
        parser.error("use the frozen candidate commit and full lowercase --harness-commit")
    try:
        if args.output:
            require(args.output.suffix == ".json", "audit output must have .json suffix")
            require(not args.output.exists() and not args.output.with_suffix(".md").exists(), "preserve previous audit output")
        validation = audit_validation(args.validation_summary)
        directory = args.directory.resolve()
        root = args.attemptroot.resolve() if args.attemptroot else directory.parent
        adopted, attempts = inventory_attempts(root, directory, args.harness_commit, validation["input_json_sha256"])
        result = {"status": "PASS", "candidate_commit": CANDIDATE_COMMIT, "baseline_commit": BASELINE_COMMIT,
                  "harness_commit": args.harness_commit, "opening_sha256": OPENING_SHA256,
                  "chess_version": chess.__version__, "validation": validation, "attempts": attempts,
                  "adoption_policy": "First complete clean time block at each time control; contaminated blocks wholly excluded and preserved; no result-based replacement.",
                  "matches": [audit_match(adopted[f"time{movetime}"], args.harness_commit, movetime) for movetime in (100, 1000)]}
        for match in result["matches"]:
            monitor = read_json(Path(match["source"]).with_name(f"time{match['movetime_ms']}.monitor.json"))
            require(utc(validation["completed_utc"]) <= utc(monitor["started_utc"]), "regression workload overlapped match")
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
            with args.output.with_suffix(".md").open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(markdown(result))
            print(markdown(result))
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, KeyError, TypeError, OSError, ZeroDivisionError) as exc:
        print(f"FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
