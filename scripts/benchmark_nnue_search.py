"""Paired fixed-node NNUE cost checks; these are not strength matches."""

import argparse
import hashlib
import json
from pathlib import Path
import random
import statistics
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--model", required=True, type=Path)
    parser.add_argument("--positions", type=Path, default=ROOT / "tests/data/profile_positions.json")
    parser.add_argument("--nodes", type=int, default=10000)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.nodes < 1 or args.repeats < 1:
        parser.error("nodes and repeats must be positive")
    binary, model = args.binary.resolve(strict=True), args.model.resolve(strict=True)
    positions = json.loads(args.positions.read_text(encoding="utf-8"))["positions"]
    rng = random.Random(20261019)
    records = []
    for repeat in range(args.repeats):
        indices = list(range(len(positions)))
        rng.shuffle(indices)
        for index in indices:
            position = positions[index]
            modes = [False, True]
            rng.shuffle(modes)
            pair = {"repeat": repeat, "position": position["name"],
                    "mode_order": ["nnue" if mode else "legacy" for mode in modes]}
            for nnue in modes:
                command = [str(binary), "search", "64", "--nodes", str(args.nodes),
                           "--eval-file", str(model)]
                if nnue:
                    command.append("--nnue")
                if "fen" in position:
                    command.extend(("--fen", position["fen"]))
                if position.get("moves"):
                    command.extend(("--play", *position["moves"]))
                result = subprocess.run(command, capture_output=True, text=True,
                                        encoding="utf-8", timeout=60, check=True)
                fields = dict(line.split(": ", 1) for line in result.stdout.splitlines() if ": " in line)
                if int(fields["nodes"]) != args.nodes:
                    raise RuntimeError("search did not reach requested node budget: " + result.stdout)
                pair["nnue" if nnue else "legacy"] = fields
            records.append(pair)
    # Compare completed-result decisions as well as work, without conflating
    # an equal score on this corpus with global legacy/quantized equivalence.
    keys = ("bestmove", "score_cp", "score_mate", "depth", "nodes", "qnodes", "pv")
    disagreements = sum(any(pair["legacy"].get(key) != pair["nnue"].get(key) for key in keys)
                        for pair in records)
    ratios = [int(pair["nnue"]["time_ms"]) / int(pair["legacy"]["time_ms"])
              for pair in records if int(pair["legacy"]["time_ms"]) > 0]
    report = {"schema_version": 1, "purpose": "fixed-node cost, not playing strength",
              "binary": str(binary), "model": str(model), "nodes": args.nodes,
              "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
              "model_sha256": hashlib.sha256(model.read_bytes()).hexdigest(),
              "positions_sha256": hashlib.sha256(args.positions.read_bytes()).hexdigest(),
              "seed": 20261019,
              "pairs": len(records), "result_disagreements": disagreements,
              "timed_pairs": len(ratios),
              "median_nnue_over_legacy_time": statistics.median(ratios) if ratios else None,
              "records": records}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "records"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
