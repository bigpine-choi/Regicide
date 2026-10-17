"""Independently replay saved memory-reuse benchmarks; never starts an engine.

Run from the repository root, or pass --root. Assertions reject inconsistent
evidence. --output explicitly writes the audit JSON; otherwise prints it.
"""

import argparse
import datetime
import hashlib
import json
from pathlib import Path
import random
import statistics
import subprocess

import chess


EXPECTED = {
    "baseline": {"commit": "ef52f14569222acbfaa512245bab81558795180f",
                 "sha256": "620cdba22750978898809e32711cb117a0f4f74a08a6ab8dffe7de9046ba5340"},
    "candidate": {"commit": "4a0f95a035b1cc0eff8e8d56a14ea898621eab64",
                  "sha256": "0bac627c4b7eb8425e6812a64dad5546ed1a7e24dd603cb365f12dd569ffd2e9"}}
ROLES = ("baseline", "candidate")
GRAPH = ("score", "move", "depth", "nodes", "seldepth", "pv", "tt_counters")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_board(position):
    board = chess.Board(position.get("fen", chess.STARTING_FEN))
    for text in position.get("moves", []):
        board.push_uci(text)
    assert board.is_valid() and board.outcome(claim_draw=True) is None
    return board


def replay(result, position, depth, search_index, role):
    board = source_board(position)
    assert board.fen(en_passant="fen") == result["search_fen"]
    assert result["depth"] == depth and result["process_search_index"] == search_index
    assert result["nodes"] > 0 and result["wall_time_seconds"] > 0
    assert result["engine_time_seconds"] >= 0
    assert result["score"]["pov"] == "mover"
    assert not result["score"]["lowerbound"] and not result["score"]["upperbound"]
    assert chess.Move.from_uci(result["move"]) in board.legal_moves
    assert result["pv"] and result["pv"][0] == result["move"]
    for text in result["pv"]:
        move = chess.Move.from_uci(text)
        assert move in board.legal_moves
        board.push(move)
    counters = result["tt_counters"]
    assert set(counters) == {"probes", "hits", "cutoffs", "stores"}
    assert 0 <= counters["cutoffs"] <= counters["hits"] <= counters["probes"] <= result["nodes"]
    assert 0 <= counters["stores"] <= counters["probes"]
    memory = result["tt_memory"]
    if role == "baseline":
        assert memory is None  # v0.4 has no memory telemetry; absence is not zero.
    else:
        assert memory == {"allocations": int(search_index == 1), "reuses": int(search_index > 1),
                          "clears": 0, "bytes": (16 * 1024 * 1024 // 56) * 56}
    return len(result["pv"])


def audit(path, root, expected_depth, expected_repeats, expected_warmup):
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["exit_code"] == 0 and document["error"] is None
    assert document["require_same_search"] is True and not document["allow_score_differences"]
    assert document["seed"] == 20261017
    assert (document["depth"], document["repeats"], document["warmup"]) == (
        expected_depth, expected_repeats, expected_warmup)
    checkout = document["environment"]["harness_checkout"]
    assert checkout["commit"] == EXPECTED["candidate"]["commit"] and checkout["status_porcelain"] == ""
    positions = document["positions_file"]["document"]["positions"]
    assert len(positions) == 10
    assert sha(Path(document["positions_file"]["path"])) == document["positions_file"]["sha256"]
    originals = json.loads((root / "tests/data/profile_positions.json").read_text(encoding="utf-8"))["positions"]
    for declared, original in zip(positions, originals):
        assert {key: value for key, value in declared.items() if key != "search_fen"} == original
    for role in ROLES:
        engine = document["engines"][role]
        assert engine["declared_source_commit"] == EXPECTED[role]["commit"]
        assert engine["sha256"] == EXPECTED[role]["sha256"] == sha(Path(engine["path"]))
        assert engine["configured_options"] == {}
        advertised = engine["advertised_options"]
        for option in ("UsePositionalEvaluation", "UseQuiescence", "UseTranspositionTable", "UseFastMoves"):
            assert advertised[option]["default"] is True
        assert advertised["Hash"]["default"] == 16 and advertised["Move Overhead"]["default"] == 20
    for option in document["engines"]["baseline"]["advertised_options"]:
        assert document["engines"]["baseline"]["advertised_options"][option] == (
            document["engines"]["candidate"]["advertised_options"][option])

    warmups = document["warmup_records"]
    assert len(warmups) == 2 * expected_warmup
    searches = {role: 0 for role in ROLES}
    pv_moves = 0
    for index, record in enumerate(warmups):
        role = ROLES[index // expected_warmup]
        assert record["role"] == role and record["position_index"] == 0
        assert record["position"] == positions[0]["name"]
        searches[role] += 1
        assert record["warmup_index"] == searches[role]
        pv_moves += replay(record["result"], positions[0], expected_depth, searches[role], role)
    if expected_warmup:
        assert all(warmups[0]["result"][field] == warmups[1]["result"][field] for field in GRAPH)

    records = document["records"]
    assert len(records) == 10 * expected_repeats
    rng = random.Random(document["seed"])
    expected = []
    for repetition in range(expected_repeats):
        indices = list(range(10))
        rng.shuffle(indices)
        for index in indices:
            order = list(ROLES if (repetition + index) % 2 == 0 else reversed(ROLES))
            expected.append((repetition + 1, index, order))
    assert [(row["repetition"], row["position_index"], row["order"]) for row in records] == expected
    for record in records:
        position = positions[record["position_index"]]
        assert record["position"] == position["name"]
        assert record["scores_equal"] is True and record["search_equal"] is True
        assert record["search_differences"] == []
        for field in GRAPH:
            assert record["baseline"][field] == record["candidate"][field], (path.name, field)
        for role in ROLES:
            searches[role] += 1
            pv_moves += replay(record[role], position, expected_depth, searches[role], role)

    summary = document["summary"]
    assert summary["completed_pairs"] == len(records)
    assert summary["score_mismatches"] == summary["search_graph_mismatches"] == 0
    ratios = [row["baseline"]["wall_time_seconds"] / row["candidate"]["wall_time_seconds"] for row in records]
    assert summary["median_paired_wall_speedup_baseline_over_candidate"] == statistics.median(ratios)
    assert summary["median_paired_node_ratio_candidate_over_baseline"] == 1
    engine_ratios = [row["baseline"]["engine_time_seconds"] / row["candidate"]["engine_time_seconds"]
                     for row in records if row["baseline"]["engine_time_seconds"] > 0
                     and row["candidate"]["engine_time_seconds"] > 0]
    assert summary["engine_time_ratio_pairs_excluding_zero_or_missing_time"] == len(engine_ratios)
    assert summary["median_paired_engine_speedup_baseline_over_candidate"] == (
        statistics.median(engine_ratios) if engine_ratios else None)
    role_stats = {}
    for role in ROLES:
        samples = [row[role] for row in records]
        work = {name: sum(sample["tt_counters"][name] for sample in samples)
                for name in ("probes", "hits", "cutoffs", "stores")}
        assert summary["transposition_counters"][role] == {"samples_reporting": len(records), **work}
        memories = [sample["tt_memory"] for sample in samples if sample["tt_memory"] is not None]
        memory = {"samples_reporting": len(memories),
                  **{name: sum(item[name] for item in memories) if memories else None
                     for name in ("allocations", "reuses", "clears")},
                  "bytes_max": max(item["bytes"] for item in memories) if memories else None}
        assert memory == summary["transposition_memory"][role]
        role_stats[role] = {"nodes_total": sum(sample["nodes"] for sample in samples),
                            "wall_total_seconds": sum(sample["wall_time_seconds"] for sample in samples),
                            "engine_total_seconds": sum(sample["engine_time_seconds"] for sample in samples),
                            "zero_reported_time_samples": sum(sample["engine_time_seconds"] == 0 for sample in samples),
                            "tt_work": work, "tt_memory": memory,
                            "first_measured_sample": {"position": records[0]["position"], **samples[0]}}
    per_position = []
    for declared in positions:
        pairs = [row for row in records if row["position"] == declared["name"]]
        assert len(pairs) == expected_repeats
        ratio = statistics.median(row["baseline"]["wall_time_seconds"] / row["candidate"]["wall_time_seconds"] for row in pairs)
        published = next(row for row in summary["per_position"] if row["name"] == declared["name"])
        assert ratio == published["median_paired_wall_speedup_baseline_over_candidate"]
        for role in ROLES:
            assert statistics.median(row[role]["wall_time_seconds"] for row in pairs) == published[role]["median_wall_time_seconds"]
            assert statistics.median(row[role]["nodes"] for row in pairs) == published[role]["median_nodes"]
        per_position.append({"name": declared["name"], "median_paired_wall_speedup": ratio})
    return {"input": path.name, "input_sha256": sha(path), "pairs": len(records), "warmups": len(warmups),
            "pv_moves_replayed_including_warmups": pv_moves, "all_search_graphs_identical": True,
            "median_paired_wall_speedup": statistics.median(ratios),
            "aggregate_wall_speedup": role_stats["baseline"]["wall_total_seconds"] / role_stats["candidate"]["wall_total_seconds"],
            "faster_candidate_pairs": sum(ratio > 1 for ratio in ratios), "role_statistics": role_stats,
            "per_position": per_position,
            "regressed_position_medians": [row for row in per_position if row["median_paired_wall_speedup"] < 1],
            "first_measured_pair_speedup": ratios[0],
            "warmup_evidence": warmups}


def source_provenance(root):
    checked = {}
    for role in ROLES:
        source = root / "build-memory-baseline/source" if role == "baseline" else root
        commit = EXPECTED[role]["commit"]
        paths = subprocess.check_output(["git", "-C", str(root), "ls-tree", "-r", "--name-only", commit], text=True).splitlines()
        relevant = [path for path in paths if path.startswith(("src/", "include/", "scripts/")) or path == "CMakeLists.txt"]
        for path in relevant:
            expected = subprocess.check_output(["git", "-C", str(root), "show", commit + ":" + path]).replace(b"\r\n", b"\n")
            assert (source / path).read_bytes().replace(b"\r\n", b"\n") == expected, (role, path)
        checked[role] = {"commit": commit, "source_files_checked_with_newline_normalization": len(relevant)}
    return checked


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--evidence", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = {"audited_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "method": "independent saved-evidence replay; no engine search or rebuild",
              "source_provenance": source_provenance(args.root),
              "limitations": ["Ten fixed positions; repeats measure timing variability, not independent playing-strength evidence.",
                              "Cold run contains one cold candidate allocation per process, followed by retained-buffer searches; not 100 independent cold starts.",
                              "Baseline v0.4 has no allocation telemetry; its per-search allocation is established by source, not new counters.",
                              "Binary hash and source agreement do not independently prove binary build provenance.",
                              "Wall time includes UCI/reset traffic; integer engine milliseconds are too coarse for many depth-one searches.",
                              "Native layout is checked separately if tt-layout-probe.jsonl is present; layout proof does not prove engine-binary build provenance."]}
    layout_path = args.evidence / "tt-layout-probe.jsonl"
    if layout_path.exists():
        layout = [json.loads(line) for line in layout_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert len(layout) == 2 and {row["role"] for row in layout} == set(ROLES)
        for row in layout:
            assert row == {"role": row["role"], "entry_bytes": 56, "capacity": 299593,
                           "allocated_bytes": 16777208, "valid": True}
        report["native_layout_probe"] = {"status": "passed", "file_sha256": sha(layout_path), "records": layout}
    else:
        report["native_layout_probe"] = {"status": "pending", "expected_entry_bytes": 56,
                                         "expected_capacity": 299593, "expected_allocated_bytes": 16777208}
    report["benchmarks"] = [audit(args.evidence / (name + ".json"), args.root, depth, repeats, warmup)
                            for name, depth, repeats, warmup in (("depth1-cold", 1, 10, 0),
                                                               ("depth1-warm", 1, 10, 1),
                                                               ("depth3-warm", 3, 5, 1))]
    report["all_assertions_passed"] = True
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
