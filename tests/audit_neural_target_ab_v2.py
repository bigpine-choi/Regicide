"""Versioned, read-only auditor for the fresh four-variant follow-up.

The original two-variant auditor stays unchanged. Its row, UCI, training,
integer-model, metric and game replay logic is reused, while this adapter checks
the explicit ab2 bank contract and every input exposed by the failed attempt.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import datetime as dt
import json
from pathlib import Path
from unittest import mock

import audit_neural_target_ab as v1

ORIGINAL_BANK_AUDIT = v1.audit_bank
ORIGINAL_MATCH_UCI_AUDIT = v1.audit_match_uci
ORIGINAL_MONITOR_AUDIT = v1.audit_monitor
OLD_ROOT = v1.ROOT / "docs/checks/2026-10-20-neural-target-ab/attempt-001"


def audit_prior_exclusions(bank: dict, old_root: Path = OLD_ROOT) -> dict:
    old_bank = v1.read_json(old_root / "bank.json")
    paths = [old_root / "bank.json", old_root / "trainval/train.jsonl",
             old_root / "trainval/validation.jsonl", old_root / "heldout/test.jsonl"]
    declared = {str(Path(item["path"]).resolve()): item["sha256"] for item in bank["exclusions"]["artifacts"]}
    for path in paths:
        v1.require(declared.get(str(path.resolve())) == v1.sha256(path), "follow-up omits an exposed attempt-001 artifact")
    old_inputs = set()
    old_families = set(old_bank["master_partition"])
    def expose(initial_fen, history):
        board = v1.board_from(initial_fen, [])
        old_inputs.add(v1.feature_hash(board))
        for text in history:
            board.push_uci(text)
            old_inputs.add(v1.feature_hash(board))
    for source in old_bank["sources"]:
        parent = v1.board_from(source["parent_initial_fen"], source["parent_history"])
        old_inputs.add(v1.feature_hash(parent))
        old_inputs.add(v1.feature_hash(v1.board_from(source["initial_fen"], source["history"])))
        expose(source["initial_fen"], source["history"])
    for block in (100, 1000):
        for opening in old_bank[f"match_openings_{block}"]:
            old_families.add(opening["family_id"])
            old_inputs.add(v1.feature_hash(v1.board_from(opening["initial_fen"], opening["history"])))
            expose(opening["initial_fen"], opening["history"])
    accepted = []
    for path in paths[1:]:
        rows = v1.read_rows(path)
        accepted.extend(rows)
        old_inputs.update(v1.feature_hash(v1.board_from(row["initial_fen"], row["history"])) for row in rows)
        for row in rows:
            expose(row["initial_fen"], row["history"])
    excluded = set(bank["exclusions"]["feature_hashes"])
    v1.require(old_inputs <= excluded, "follow-up exclusion list omits old parents/variants/matches/accepted inputs")
    v1.require(not old_families & set(bank["master_partition"]), "follow-up reuses old family names")
    for item in bank["sources"] + bank["match_openings_100"] + bank["match_openings_1000"]:
        identity = v1.feature_hash(v1.board_from(item["initial_fen"], item["history"]))
        v1.require(identity not in old_inputs, "follow-up source or match reuses an exposed input")
        if "parent_initial_fen" in item:
            parent = v1.feature_hash(v1.board_from(item["parent_initial_fen"], item["parent_history"]))
            v1.require(parent not in old_inputs, "follow-up parent reuses an exposed input")
    return {"old_artifacts_bound": len(paths), "old_accepted_rows": len(accepted),
            "old_exact_inputs_excluded": len(old_inputs), "old_family_name_overlap": 0}


def audit_bank(bank: dict) -> dict:
    v1.require(bank.get("variants_per_family") == 4 and bank.get("variant_seed") == 20261127
               and bank.get("split_seed") == 20261026 and bank.get("family_namespace") == "ab2",
               "follow-up bank metadata differs from the four-variant preregistration")
    sources = bank.get("sources")
    v1.require(isinstance(sources, list) and len(sources) == 320, "follow-up needs exactly 320 sources")
    groups = defaultdict(list)
    identities = set()
    parents = set()
    for source in sources:
        family = source["family_id"]
        v1.require(family.startswith("ab2-"), "follow-up family namespace changed")
        groups[family].append(source)
        board = v1.board_from(source["initial_fen"], source["history"])
        identity = v1.feature_hash(board)
        v1.require(identity not in identities, "duplicate source input within/across four-variant families")
        identities.add(identity)
        parents.add(source["parent_feature_hash"])
    v1.require(len(groups) == len(parents) == 80 and all(len(items) == 4 for items in groups.values()),
               "follow-up does not contain 80 parents and four variants each")
    for family, records in groups.items():
        items = sorted(records, key=lambda item: item["seed_id"])
        v1.require([item["seed_id"] for item in items] == [f"{family}-v{index}" for index in range(4)],
                   "four variant IDs differ from v0..v3")
        v1.require([len(item["history"])-len(item["parent_history"]) for item in items] == [2, 4, 2, 4],
                   "follow-up extensions differ from +2,+4,+2,+4")
        for item in items:
            v1.require(item["initial_fen"] == item["parent_initial_fen"]
                       and item["history"][:len(item["parent_history"])] == item["parent_history"],
                       "source does not preserve its declared family parent")
    # Run the unchanged, independently implemented original stratum/board/parent
    # checks twice; every one of the 320 sources is covered exactly once.
    report = None
    for offset in (0, 2):
        selected = [item for family in groups for item in sorted(groups[family], key=lambda row: row["seed_id"])[offset:offset+2]]
        report = ORIGINAL_BANK_AUDIT({**bank, "sources": selected})
    v1.require(not identities & set(bank["exclusions"]["feature_hashes"]), "follow-up source is an excluded old input")
    return {**report, "variants": 320, "variants_per_family": 4, "variant_seed": 20261127,
            "family_namespace": "ab2", "previous_attempt_exclusions": audit_prior_exclusions(bank)}


def audit_match_uci(directory: Path, document: dict) -> dict:
    result = ORIGINAL_MATCH_UCI_AUDIT(directory, document)
    records = [move for game in document["games"] for move in game["search_moves"]]
    pending, cursor = {}, 0
    for row in v1.read_rows(directory / "uci.jsonl"):
        role, line = row["role"], row["line"]
        if row["direction"] == "send" and line.startswith("go "):
            pending[role] = {}
        elif row["direction"] == "recv" and role in pending:
            if line.startswith("info depth "):
                info = v1.parse_info(line)
                if "score" in info:
                    pending[role] = {**info["score"], "lowerbound": "lowerbound" in line.split(),
                                     "upperbound": "upperbound" in line.split()}
            elif line.startswith("bestmove "):
                actual, reported = pending.pop(role), records[cursor]["score"]
                v1.require(reported["pov"] == "mover"
                           and reported["cp"] == (actual["stm"] if actual["kind"] == "cp" else None)
                           and reported["mate"] == (actual["stm"] if actual["kind"] == "mate" else None)
                           and reported["lowerbound"] is actual["lowerbound"]
                           and reported["upperbound"] is actual["upperbound"], "raw mover score/mate/bounds differ")
                cursor += 1
    v1.require(cursor == len(records) and not pending, "raw scored search count differs")
    return {**result, "raw_cp_mate_and_bound_reports_checked": cursor}


def audit_monitor(directory: Path, document: dict) -> dict:
    result = ORIGINAL_MONITOR_AUDIT(directory, document)
    raw, previous = v1.read_rows(directory / "monitor.jsonl"), None
    for row in raw:
        external = {(item["pid"], item["creation_ticks"]): item["cpu_ticks"]
                    for item in row["processes"] if not item["owned"]}
        if previous is not None:
            v1.require(external == previous, "an individual external native CPU counter changed")
        previous = external
    start = dt.datetime.fromisoformat(document["started_at"])
    finish = start + dt.timedelta(seconds=document["total_elapsed_seconds"])
    v1.require(dt.datetime.fromisoformat(raw[0]["utc"]) <= start
               and finish <= dt.datetime.fromisoformat(raw[-1]["utc"]) + dt.timedelta(seconds=2),
               "native CPU evidence does not cover the match interval")
    return {**result, "individual_external_cpu_counters_unchanged": True}


def audit_experiment(root: Path, *, engine: Path | None = None, data_only: bool = False,
                     match_dir: Path | None = None) -> dict:
    with mock.patch.object(v1, "audit_bank", audit_bank), mock.patch.object(v1, "audit_match_uci", audit_match_uci), \
            mock.patch.object(v1, "audit_monitor", audit_monitor):
        result = v1.audit_experiment(root, engine=engine, data_only=data_only, match_dir=match_dir)
    if (root / "heldout/uci.jsonl").is_file():
        stamp = v1.read_rows(root / "heldout/uci.jsonl")[0]["utc"]
        frozen = v1.read_json(root / "training/model-freeze.json")["created_utc"]
        v1.require(dt.datetime.fromisoformat(stamp) > dt.datetime.fromisoformat(frozen), "holdout began before model freeze")
        result["holdout_first_uci_after_model_freeze"] = True
    result["auditor_sha256"] = v1.sha256(Path(__file__))
    result["base_auditor_sha256"] = v1.sha256(Path(v1.__file__))
    result["audit_protocol"] = "ab2-four-variants-fresh-full-rerun"
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--engine", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--data-only", action="store_true")
    parser.add_argument("--match-dir", type=Path)
    args = parser.parse_args()
    result = audit_experiment(args.root.resolve(), engine=args.engine.resolve() if args.engine else None,
                              data_only=args.data_only, match_dir=args.match_dir.resolve() if args.match_dir else None)
    with args.output.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(result, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
