"""Read old arena logs; export aggregate engineering diagnostics only.

No engines run. No individual FEN, selected move, score, PV, outcome labels or
training example is exported. Invalid matches are listed but never aggregated.
"""
import argparse
import collections
import datetime
import hashlib
import json
from pathlib import Path
import statistics


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def metrics(moves):
    reported = [move for move in moves if move.get("nodes") is not None and move["elapsed_seconds"] > 0]
    nodes = sum(move["nodes"] for move in reported)
    elapsed = sum(move["elapsed_seconds"] for move in reported)
    depths = [move["depth"] for move in moves if move.get("depth") is not None]
    per_move = [move["nodes"] / move["elapsed_seconds"] for move in reported]
    return {"moves": len(moves), "moves_with_nodes_and_time": len(reported),
            "reported_nodes": nodes, "transport_seconds": elapsed,
            "weighted_nodes_per_second": nodes / elapsed if elapsed else None,
            "median_move_nodes_per_second": statistics.median(per_move) if per_move else None,
            "median_completed_depth": statistics.median(depths) if depths else None,
            "mean_completed_depth": statistics.mean(depths) if depths else None,
            "depth_counts": dict(sorted(collections.Counter(depths).items())),
            "mean_move_seconds": statistics.mean(move["elapsed_seconds"] for move in moves) if moves else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=Path, default=Path("build/learning-cycle/runs"))
    parser.add_argument("--output", type=Path, default=Path("build/neural-improvement-diagnosis/old-arena-speed.json"))
    args = parser.parse_args()
    report = {"schema": "regicide.old-arena-throughput-diagnosis.v1",
              "generated_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "scope": "Stored match aggregates on different positions; not an isolated inference benchmark, not a new strength experiment.",
              "data_use": "Aggregate engineering diagnosis only; protected arena positions/moves/labels are not exported for training or development selection.",
              "runs": []}
    for run in sorted(args.runs.iterdir()):
        path = run / "arena/arena.json"
        if not path.is_file():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        guard = data.get("resource_guard", {})
        valid = (data["status"] == "complete" and data["completed"] and not data["errors"]
                 and guard.get("valid_for_comparison") and not guard.get("contaminated"))
        item = {"run": run.name, "source_path": path.as_posix(), "source_sha256": digest(path),
                "attempt_index": data["attempt_index"], "status": data["status"],
                "excluded_from_comparison": not valid, "binary_sha256": data["binary_sha256"],
                "candidate_sha256": data["candidate_sha256"], "movetime_ms": data["movetime_ms"],
                "requested_pairs": data["requested_pairs"], "recorded_games": len(data["games"]),
                "resource_guard": {name: guard.get(name) for name in
                    ("valid_for_comparison", "contaminated", "failure", "snapshot_count",
                     "max_snapshot_gap_seconds", "external_cpu_increase_ticks")}}
        if valid:
            role_moves = {role: [move for game in data["games"] for move in game["search_moves"]
                                 if move["role"] == role] for role in ("baseline", "candidate")}
            item["roles"] = {role: metrics(moves) for role, moves in role_moves.items()}
            item["candidate_to_baseline_weighted_nps_ratio"] = (
                item["roles"]["candidate"]["weighted_nodes_per_second"] /
                item["roles"]["baseline"]["weighted_nodes_per_second"])
            groups = collections.defaultdict(list)
            for role, moves in role_moves.items():
                for move in moves:
                    pieces = sum(character.isalpha() for character in move["fen_before"].split()[0])
                    bucket = "25-32 pieces" if pieces >= 25 else "13-24 pieces" if pieces >= 13 else "<=12 pieces"
                    groups[(bucket, role)].append(move)
            item["piece_count_buckets"] = [{"bucket": bucket, "role": role, **metrics(moves)}
                                            for (bucket, role), moves in sorted(groups.items())]
        report["runs"].append(item)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(args.output.as_posix())


if __name__ == "__main__":
    main()
