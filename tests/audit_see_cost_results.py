"""Replay the SEE cost improvement comparison without launching an engine.

The prior replication's native CPU/ancestry and first-clean-attempt replay
functions are reused with this experiment's explicit frozen identities. Their
source files and old evidence remain unchanged. Engine/option, opening, native
coverage and score checks below are specific to the v0.5.2/v0.5.3 comparison.
"""

import argparse
import copy
import json
from pathlib import Path
import random
import re
import sys
import tempfile

import chess

import audit_see_replication as reference
from audit_see_results import (ROLES, ROOT, audit_game, audit_pgn, check_checkout,
                               digest, effective_options, read_json, require,
                               resources_summary)


FREEZE_PATH = ROOT / "tests/data/see_cost_freeze_2026-10-18.json"
BASELINE_COMMIT = "0edc06e437b934d177fc1e9b0907600b6287be72"
BASELINE_SHA256 = "2ba41c8303f266149323b55186f5c6a806c33f73d529d6911d6ca4b33bfa53db"
OPENING_SHA256 = "9dafe565121ddad88cef0ca9060bd87b73a7bc52c837240835be0cf5f599ea9e"
NATIVE_CHECKS = {"native-tests", "native-search_tests", "native-evaluation_tests",
                 "native-board_state_tests", "native-transposition_tests", "native-see_tests"}
EXPECTED_OPTIONS = {"Hash": 16, "Move Overhead": 20, "UsePositionalEvaluation": True,
                    "UseQuiescence": True, "UseTranspositionTable": True,
                    "UseFastMoves": True, "UseQuietMoveOrdering": True,
                    "UseQuiescenceChecks": True, "PVS": True, "UseSEE": True}


def load_freeze(path):
    freeze = read_json(path)
    require(freeze["schema_version"] == 1, "wrong freeze schema")
    require(freeze["baseline"] == {"commit": BASELINE_COMMIT,
            "binary": "build-see-native/regicide.exe", "sha256": BASELINE_SHA256,
            "engine_name": "Regicide 0.5.2"}, "wrong pre-change SEE baseline")
    candidate = freeze["candidate"]
    require(re.fullmatch(r"[0-9a-f]{40}", candidate["commit"] or "") is not None
            and re.fullmatch(r"[0-9a-f]{64}", candidate["sha256"] or "") is not None,
            "complete and commit candidate source/hash before any measurement")
    require(candidate["engine_name"] == "Regicide 0.5.3"
            and candidate["binary"] == "build-see-cost/regicide.exe", "wrong candidate identity")
    require(freeze["openings"] == {"path": "tests/data/see_cost_openings_2026-10-18.json",
            "sha256": OPENING_SHA256}, "wrong held-out opening fingerprint")
    require(freeze["effective_options"] == EXPECTED_OPTIONS, "asymmetric or changed frozen options")
    require(freeze["bootstrap"] == {"seed": 20261020, "resamples": 20000}, "wrong bootstrap policy")
    checks = freeze["required_validation_checks"]
    require(len(checks) == len(set(checks)) == 21 and set(checks) == reference.CHECK_NAMES,
            "required regression suites changed")
    return freeze


def bind_monitor_replay(freeze):
    # These three functions read identity constants from their own module.
    # Adapt only that in-memory context; no historical source/data is rewritten.
    reference.BASELINE_COMMIT = freeze["baseline"]["commit"]
    reference.CANDIDATE_COMMIT = freeze["candidate"]["commit"]
    reference.BINARY_SHA256 = {role: freeze[role]["sha256"] for role in ROLES}
    reference.OPENING_SHA256 = freeze["openings"]["sha256"]


def check_engines(engines, freeze):
    require(set(engines) == set(ROLES), "wrong engine roles")
    result = {}
    all_options = []
    for role in ROLES:
        engine, declared = engines[role], freeze[role]
        require(engine["provided_source_commit"] == declared["commit"], f"wrong {role} source")
        require(engine["engine_id"].get("name") == declared["engine_name"], f"wrong {role} version")
        binary = Path(engine["command"][0])
        require(binary.resolve() == (ROOT / declared["binary"]).resolve(), f"wrong {role} binary path")
        require(binary.is_file() and digest(binary) == engine["sha256"] == declared["sha256"],
                f"{role} binary hash mismatch")
        options = effective_options(engine, "requested_options")
        for name, expected in freeze["effective_options"].items():
            require(name in options and type(options[name]) is type(expected) and options[name] == expected,
                    f"wrong {role} effective {name}")
        all_options.append(options)
        result[role] = {"commit": declared["commit"], "sha256": declared["sha256"],
                        "id": engine["engine_id"], "effective_options": options}
    require(all_options[0] == all_options[1], "effective UCI options differ between the engines")
    return result


def audit_validation(path, freeze):
    data = read_json(path)
    require(data["all_passed"] is True and data["binary_sha256"] == {
            role: freeze[role]["sha256"] for role in ROLES}
            and data["engine_source_commit"] == freeze["candidate"]["commit"],
            "wrong validation source/binary/result")
    checks = data["checks"]
    require(data["checks_count"] == len(checks) == len(freeze["required_validation_checks"])
            and {item["name"] for item in checks} == set(freeze["required_validation_checks"]),
            "missing or duplicated required regression checks")
    groups = assertions = skips = 0
    completed = reference.utc(data["completed_utc"])
    native_coverage = {}
    for item in checks:
        require(item["result"] == "passed" and item["exit_code"] == 0, "a validation gate failed")
        log = Path(item["log"])
        if not log.is_absolute():
            log = ROOT / log
        require(log.is_file() and digest(log) == item["sha256"], "validation log hash mismatch")
        require(reference.utc(item["started_utc"]) <= reference.utc(item["ended_utc"]) <= completed,
                "validation gate time escapes completion time")
        require(isinstance(item["skipped_cases"], int) and item["skipped_cases"] >= 0,
                "invalid skipped case count")
        skips += item["skipped_cases"]
        if item["name"] in NATIVE_CHECKS:
            summaries = re.findall(r"(?m)^(\d+) groups, (\d+) assertions, (\d+) failures\s*$",
                                   log.read_text(encoding="utf-8-sig"))
            require(len(summaries) == 1, "missing or ambiguous native coverage summary")
            group_count, assertion_count, failures = map(int, summaries[0])
            require(group_count > 0 and assertion_count > 0 and failures == 0,
                    "native suite failed or has no coverage")
            groups += group_count
            assertions += assertion_count
            native_coverage[item["name"]] = {"groups": group_count, "assertions": assertion_count}
    require(data["candidate_native_groups"] == groups and data["candidate_native_assertions"] == assertions,
            "native coverage differs from actual six suite summaries")
    require(data["skipped_case_count"] == skips == 1 and
            next(item for item in checks if item["name"] == "game_archive_tests")["skipped_cases"] == 1,
            "unexpected skipped cases")
    return {"status": "PASS", "input_json_sha256": digest(path), "checks": len(checks),
            "native_groups": groups, "native_assertions": assertions, "native_coverage": native_coverage,
            "completed_utc": data["completed_utc"], "skipped_case_count": skips, "skip_note": data["skip_note"]}


def paired_bootstrap(scores, freeze):
    config = freeze["bootstrap"]
    generator = random.Random(config["seed"])
    samples = sorted(sum(generator.choices(scores, k=len(scores))) / len(scores)
                     for _ in range(config["resamples"]))
    return {**config, "pairs": len(scores),
            "interval": [samples[int(.025 * (len(samples) - 1))], samples[int(.975 * (len(samples) - 1))]],
            "scope": "Descriptive fixed-opening paired bootstrap, not universal strength or Elo."}


def audit_match(path, harness_commit, movetime, freeze):
    document = read_json(path)
    require(document["completed"] is True and document["exit_code"] == 0 and not document["errors"]
            and document["valid_for_comparison"] is True, "match did not complete cleanly")
    check_checkout(document["harness_checkout"], harness_commit)
    require(document["harness_source_commit"] == harness_commit, "wrong frozen harness source")
    engines = check_engines(document["engines"], freeze)
    require(document["limits"] == {"nodes": None, "depth": None, "movetime_ms": movetime}, "wrong time limit")
    require(document["max_plies"] == 500 and document["draw_policy"] == "claim", "changed adjudication")
    opening_file = ROOT / freeze["openings"]["path"]
    require(document["opening_file_sha256"] == digest(opening_file) == freeze["openings"]["sha256"],
            "fresh opening bytes changed")
    expected = read_json(opening_file)["openings"]
    require(len(expected) == len(document["selected_openings"]) == 8 and
            document["games_requested"] == len(document["games"]) == 16, "wrong complete game schedule")
    points, pair_scores, terminations = [], [], {}
    resources = {role: [] for role in ROLES}
    total_plies = pv_plies = 0
    for pair, opening in enumerate(expected, 1):
        selected = document["selected_openings"][pair - 1]
        require(selected["name"] == opening["name"] and selected["moves"] == opening["moves"],
                "fresh selected opening mismatch")
        paired = []
        for offset in (0, 1):
            number = (pair - 1) * 2 + offset + 1
            record = document["games"][number - 1]
            require(record["game"] == number and record["pair"] == pair and
                    record["opening"] == opening["name"] and record["opening_uci_moves"] == opening["moves"],
                    "opening or game schedule mismatch")
            require(record["white"] == ("candidate" if offset == 0 else "baseline") and
                    record["black"] == ("baseline" if offset == 0 else "candidate"), "colors not swapped")
            result = audit_game(record)
            points.append(result["points"])
            paired.append(result["points"])
            total_plies += result["plies"]
            pv_plies += result["pv_plies"]
            terminations[result["termination"]] = terminations.get(result["termination"], 0) + 1
            for role in ROLES:
                resources[role].extend(result["resources"][role])
        pair_scores.append(sum(paired) / 2)
    counts = {"candidate_wins": points.count(1.0), "candidate_draws": points.count(.5),
              "candidate_losses": points.count(0.0), "candidate_points": sum(points),
              "candidate_score": sum(points) / 16, "completed_games": 16,
              "incomplete_games": 0, "unplayed_games": 0}
    for key, value in counts.items():
        require(document["summary"][key] == value, "harness summary mismatch: " + key)
    require(audit_pgn(path.with_suffix(".pgn"), document) == 16, "PGN count mismatch")
    monitor_path = path.with_name(path.stem + ".monitor.json")
    log_path = path.with_name(path.stem + ".monitor.jsonl")
    summary = read_json(monitor_path)
    require(document["external_engine_guard"] == summary, "embedded guard differs from sidecar")
    require(summary["monitor_log_sha256"] == digest(log_path), "monitor JSONL hash mismatch")
    rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8-sig").splitlines()]
    monitor = reference.replay_monitor(summary, rows, document, movetime, harness_commit)
    return {"status": "PASS", "source": str(path), "input_json_sha256": digest(path),
            "input_pgn_sha256": digest(path.with_suffix(".pgn")),
            "monitor_json_sha256": digest(monitor_path), "monitor_jsonl_sha256": digest(log_path),
            "movetime_ms": movetime, "engines": engines, **counts, "actual_plies": total_plies,
            "pv_plies": pv_plies, "terminations": terminations, "pair_scores": pair_scores,
            "paired_opening_bootstrap95": paired_bootstrap(pair_scores, freeze),
            "harness_bootstrap_preserved": document["summary"]["paired_opening_bootstrap95_descriptive"],
            "monitor": monitor, "resources": {role: resources_summary(resources[role], movetime) for role in ROLES}}


def markdown(result):
    lines = ["# SEE 비용 개선 독립 감사", "", "전체 판정: **PASS**.", "",
             "| 조건 | 후보 승·무·패 | 득점률 | 시작 쌍 참고 95% 구간 |", "|---|---:|---:|---:|"]
    for match in result["matches"]:
        low, high = match["paired_opening_bootstrap95"]["interval"]
        lines.append(f"| {match['movetime_ms']}ms | {match['candidate_wins']}·{match['candidate_draws']}·{match['candidate_losses']} "
                     f"| {match['candidate_score']:.3%} | {low:.3%} ~ {high:.3%} |")
    lines += ["", "새 고정 시작 위치 8쌍의 기술적 참고 구간입니다. seed 20261020, 20,000회이며 Elo 구간이 아닙니다.",
              "실제 수·PV·PGN·종료 판정·바이너리·옵션·검증 로그·모든 감시 행·첫 정상 묶음 채택을 대조했습니다.",
              "알려진 외부 엔진의 관측 CPU 증가는 0 native ticks입니다. 표본 사이의 짧은 프로세스나 일반 PC 부하까지 배제하지는 못합니다."]
    return "\n".join(lines) + "\n"


def self_test():
    # Reuse the 21 monitor-corruption and first-clean-attempt rejection fixtures.
    reference.self_test()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        freeze = read_json(FREEZE_PATH)
        freeze["candidate"].update(commit="f" * 40, sha256="a" * 64)
        manifest = root / "freeze.json"
        manifest.write_text(json.dumps(freeze), encoding="utf-8")
        freeze = load_freeze(manifest)
        checks = []
        for name in freeze["required_validation_checks"]:
            log = root / (name + ".log")
            log.write_text("1 groups, 2 assertions, 0 failures\n" if name in NATIVE_CHECKS else "PASS\n", encoding="utf-8")
            checks.append({"name": name, "result": "passed", "log": str(log), "sha256": digest(log),
                           "exit_code": 0, "skipped_cases": int(name == "game_archive_tests"),
                           "started_utc": "2026-10-18T00:00:00Z", "ended_utc": "2026-10-18T00:00:01Z"})
        summary = {"all_passed": True, "binary_sha256": {role: freeze[role]["sha256"] for role in ROLES},
                   "engine_source_commit": freeze["candidate"]["commit"], "checks": checks, "checks_count": 21,
                   "candidate_native_groups": 6, "candidate_native_assertions": 12,
                   "skipped_case_count": 1, "completed_utc": "2026-10-18T00:00:02Z", "skip_note": "synthetic"}
        path = root / "validation.json"
        path.write_text(json.dumps(summary), encoding="utf-8")
        require(audit_validation(path, freeze)["native_groups"] == 6, "actual native coverage was hardcoded")
        for label, alter in (
                ("fabricated native count", lambda data: data.update(candidate_native_assertions=99)),
                ("missing native suite", lambda data: data["checks"].pop(0)),
                ("validation after completion", lambda data: data["checks"][0].update(ended_utc="2026-10-18T01:00:00Z")),
                ("extra skipped case", lambda data: data["checks"][0].update(skipped_cases=1))):
            data = copy.deepcopy(summary)
            alter(data)
            path.write_text(json.dumps(data), encoding="utf-8")
            try:
                audit_validation(path, freeze)
            except ValueError:
                pass
            else:
                raise AssertionError("accepted " + label)
        freeze["candidate"]["commit"] = None
        manifest.write_text(json.dumps(freeze), encoding="utf-8")
        try:
            load_freeze(manifest)
        except ValueError:
            pass
        else:
            raise AssertionError("unfrozen candidate accepted")
        freeze["candidate"]["commit"] = "f" * 40
        engines = {}
        for role in ROLES:
            binary = root / (role + ".exe")
            binary.write_bytes(role.encode("ascii"))
            freeze[role].update(binary=str(binary), sha256=digest(binary))
            engines[role] = {"command": [str(binary), "uci"], "sha256": digest(binary),
                             "provided_source_commit": freeze[role]["commit"],
                             "engine_id": {"name": freeze[role]["engine_name"]}, "requested_options": {},
                             "advertised_options": {name: {"type": "check" if isinstance(value, bool) else "spin",
                                                            "default": value} for name, value in EXPECTED_OPTIONS.items()}}
        require(check_engines(engines, freeze)["baseline"]["effective_options"]["UseSEE"] is True,
                "SEE baseline identity/default was not accepted")
        for label, alter in (
                ("baseline SEE disabled", lambda data: data["baseline"]["advertised_options"]["UseSEE"].update(default=False)),
                ("old PVS baseline identity", lambda data: data["baseline"]["engine_id"].update(name="Regicide 0.5.1")),
                ("baseline claims candidate source", lambda data: data["baseline"].update(provided_source_commit="f" * 40)),
                ("asymmetric effective options", lambda data: data["candidate"]["requested_options"].update(Hash=32))):
            altered = copy.deepcopy(engines)
            alter(altered)
            try:
                check_engines(altered, freeze)
            except ValueError:
                pass
            else:
                raise AssertionError("accepted " + label)
    print("PASS: dynamic native coverage, four corrupt-validation rejections, incomplete freeze rejection, "
          "SEE baseline/default and four corrupt-engine rejections; no engine launched")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", nargs="?", type=Path, default=ROOT / "build/see-cost-comparison/attempt-001")
    parser.add_argument("--freeze", type=Path, default=FREEZE_PATH)
    parser.add_argument("--harness-commit")
    parser.add_argument("--validation-summary", type=Path, default=ROOT / "build/see-cost-evidence/validation-summary.json")
    parser.add_argument("--attemptroot", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.self_test:
            self_test()
            return 0
        freeze = load_freeze(args.freeze)
        validation = audit_validation(args.validation_summary, freeze)
        if args.validate_only:
            print(json.dumps(validation, indent=2))
            return 0
        require(args.harness_commit and re.fullmatch(r"[0-9a-f]{40}", args.harness_commit),
                "use the full frozen harness commit")
        if args.output:
            require(args.output.suffix == ".json" and not args.output.exists()
                    and not args.output.with_suffix(".md").exists(), "preserve previous audit output")
        bind_monitor_replay(freeze)
        directory = args.directory.resolve()
        root = args.attemptroot.resolve() if args.attemptroot else directory.parent
        adopted, attempts = reference.inventory_attempts(root, directory, args.harness_commit, validation["input_json_sha256"])
        freeze_sha = digest(args.freeze)
        for attempt in attempts:
            require(read_json(root / attempt["attempt"] / "run-state.json")["freeze_manifest_sha256"] == freeze_sha,
                    "attempt freeze manifest fingerprint differs")
        result = {"status": "PASS", "candidate_commit": freeze["candidate"]["commit"],
                  "baseline_commit": freeze["baseline"]["commit"], "harness_commit": args.harness_commit,
                  "opening_sha256": freeze["openings"]["sha256"], "freeze_manifest_sha256": freeze_sha,
                  "auditor_source": Path(__file__).relative_to(ROOT).as_posix(),
                  "auditor_source_sha256": digest(Path(__file__)),
                  "monitor_replay_source_sha256": digest(Path(reference.__file__)),
                  "chess_version": chess.__version__, "validation": validation, "attempts": attempts,
                  "adoption_policy": "First complete clean block per time control; preserve/exclude entire contaminated blocks; no result-based replacement.",
                  "matches": [audit_match(adopted[f"time{ms}"], args.harness_commit, ms, freeze) for ms in (100, 1000)]}
        for match in result["matches"]:
            monitor = read_json(Path(match["source"]).with_name(f"time{match['movetime_ms']}.monitor.json"))
            require(reference.utc(validation["completed_utc"]) <= reference.utc(monitor["started_utc"]),
                    "regression workload overlapped a match")
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
