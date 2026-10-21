"""Additive, pre-match frozen audit adapter; original v1/v2 sources stay intact.

The v2 monitor wrapper compares its first snapshot to metadata.started_at.
The driver creates that timestamp BEFORE guard.start()/require_idle(10), so it
is a driver-start timestamp, not a first-search timestamp. This adapter retains
the original native-counter/process/phase audit and checks the actual raw first
go through last bestmove, including every before/after-game snapshot and idle.
It changes neither engine execution nor the measurement validity policy.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import sys
from unittest import mock

ROOT = next(parent for parent in Path(__file__).resolve().parents
            if (parent / "tests/audit_neural_target_ab_v2.py").is_file())
sys.path.insert(0, str(ROOT / "tests"))
import audit_neural_target_ab_v2 as v2
v1 = v2.v1


def utc(value):
    return dt.datetime.fromisoformat(value)


def coverage(monitor, raw_uci, document):
    go = [row for row in raw_uci if row["direction"] == "send" and row["line"].startswith("go ")]
    best = [row for row in raw_uci if row["direction"] == "recv" and row["line"].startswith("bestmove ")]
    v1.require(go and len(go) == len(best), "raw go/bestmove coverage count differs")
    idle = [row for row in monitor if row["phase"] == "idle-complete"]
    v1.require(len(idle) == 1 and idle[0]["elapsed_seconds"]-monitor[0]["elapsed_seconds"] >= 10,
               "missing complete ten-second pre-search idle interval")
    first, last = min(utc(row["utc"]) for row in go), max(utc(row["utc"]) for row in best)
    v1.require(utc(monitor[0]["utc"]) <= utc(idle[0]["utc"]) <= first
               and last <= utc(monitor[-1]["utc"]), "monitor does not cover actual first go through last bestmove")
    before = [row for row in monitor if row["phase"] == "before-game"]
    after = [row for row in monitor if row["phase"] == "after-game"]
    v1.require(len(before) == len(after) == len(document["games"]), "per-game CPU snapshots are missing")
    for index, game in enumerate(document["games"]):
        selected_go = [row for row in go if game["uci_seq_before"] < row["seq"] <= game["uci_seq_after"]]
        selected_best = [row for row in best if game["uci_seq_before"] < row["seq"] <= game["uci_seq_after"]]
        v1.require(selected_go and len(selected_go) == len(selected_best) == len(game["search_moves"]),
                   "per-game raw search/record count differs")
        v1.require(utc(before[index]["utc"]) <= min(utc(row["utc"]) for row in selected_go)
                   and max(utc(row["utc"]) for row in selected_best) <= utc(after[index]["utc"]),
                   "before/after-game snapshots do not bracket actual game searches")
    return {"actual_searches": len(go), "first_go_utc": first.isoformat(), "last_bestmove_utc": last.isoformat(),
            "idle_complete_utc": idle[0]["utc"], "game_search_intervals_bracketed": len(document["games"]),
            "metadata_started_at_is_driver_start_not_first_search": True}


def compatible_monitor(directory, document):
    result = v2.ORIGINAL_MONITOR_AUDIT(directory, document)
    monitor, previous = v1.read_rows(directory / "monitor.jsonl"), None
    for row in monitor:
        current = {(item["pid"], item["creation_ticks"]): item["cpu_ticks"]
                   for item in row["processes"] if not item["owned"]}
        v1.require(previous is None or current == previous, "an individual external native CPU counter changed")
        previous = current
    return {**result, **coverage(monitor, v1.read_rows(directory / "uci.jsonl"), document),
            "individual_external_cpu_counters_unchanged": True,
            "timestamp_contract_correction_only": True}


def self_test():
    def stamp(seconds):
        return (dt.datetime(2026, 10, 20, tzinfo=dt.timezone.utc) + dt.timedelta(seconds=seconds)).isoformat()
    monitor = [{"phase": name, "utc": stamp(seconds), "elapsed_seconds": seconds}
               for name, seconds in (("initial", 0), ("idle-complete", 10),
                                      ("before-game", 10.1), ("after-game", 10.5), ("finished", 11))]
    raw = [{"seq": 1, "direction": "send", "line": "go movetime 100", "utc": stamp(10.2)},
           {"seq": 2, "direction": "recv", "line": "bestmove g1f3", "utc": stamp(10.4)}]
    document = {"started_at": stamp(-.1), "games": [{"uci_seq_before": 0, "uci_seq_after": 2,
                                                     "search_moves": [{}]}]}
    coverage(monitor, raw, document)
    for target, index, value in ((monitor, 1, 9), (monitor, 2, 10.3), (monitor, 3, 10.3), (monitor, 4, 10.3)):
        old = target[index].copy()
        target[index]["utc"] = stamp(value)
        if index == 1:
            target[index]["elapsed_seconds"] = value
        try:
            coverage(monitor, raw, document)
        except ValueError:
            pass
        else:
            raise AssertionError("uncovered idle/game/search interval accepted")
        target[index] = old
    return {"status": "PASS", "valid_driver_start_before_guard_is_accepted": True,
            "independent_coverage_tamper_rejections": 4}


def audit(root, match_dir):
    frozen = v1.read_json(root / "match-audit-compatibility-freeze.json")
    v1.checked_files(frozen["files"])
    execution = v1.read_json(root / "freeze.json")
    v1.checked_files(execution["files"])
    v1.checked_files(v1.read_json(root / "training/model-freeze.json")["files"])
    document = v1.read_json(match_dir / "match.json")
    v1.require(document["frozen_before"]["files"] == document["frozen_after"]["files"],
               "match executed inputs changed between before/after snapshots")
    v1.require(document["frozen_before"]["freeze_sha256"] == document["frozen_after"]["freeze_sha256"]
               == v1.sha256(root / "freeze.json"), "match execution freeze identity differs")
    for path, expected in document["frozen_before"]["files"].items():
        v1.require(v1.sha256(path) == expected, "a recorded match input changed after measurement")
    try:
        v2.audit_monitor(match_dir, document)
    except ValueError as error:
        original = {"status": "FAIL", "error": str(error),
                    "classification": "audit timestamp-contract bug only if exactly the documented message"}
        v1.require(str(error) == "native CPU evidence does not cover the match interval",
                   "original monitor audit failed for an additional reason")
    else:
        original = {"status": "PASS"}
    with (match_dir / "original-frozen-v2-monitor-audit.json").open("x", encoding="utf-8") as stream:
        json.dump(original, stream, indent=2)
    with mock.patch.object(v1, "audit_match_uci", v2.audit_match_uci), \
            mock.patch.object(v1, "audit_monitor", compatible_monitor):
        result = v1.audit_matches(match_dir, v1.read_json(root / "bank.json"),
                                  v1.read_json(root / "training/models.json")["models"])
    v1.checked_files(frozen["files"])
    v1.checked_files(execution["files"])
    return {"status": "PASS", "adapter_sha256": v1.sha256(Path(__file__)),
            "adapter_freeze_sha256": v1.sha256(root / "match-audit-compatibility-freeze.json"),
            "original_frozen_v2_monitor": original, "matches100ms": result,
            "scope": "Additive read-only correction of driver-start versus first-search timestamp; no runtime/model/result mutation"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--match-dir", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = self_test() if args.self_test else audit(args.root.resolve(), args.match_dir.resolve())
    if args.output:
        with args.output.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(result, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
