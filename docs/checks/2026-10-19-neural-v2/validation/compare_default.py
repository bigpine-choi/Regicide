"""Compare unrestricted classical search outputs; not timing/strength evidence."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def query(binary, case):
    args = [str(binary), "search", "3", "--nodes", "30000", "--fen", case["initial_fen"]]
    if case["history"]:
        args += ["--play", *case["history"]]
    result = subprocess.run(args, check=True, capture_output=True, text=True, timeout=60)
    fields = dict(line.split(": ", 1) for line in result.stdout.splitlines() if ": " in line)
    fields.pop("time_ms", None)
    if not fields.get("bestmove") or not fields.get("score_cp"):
        raise ValueError("search omitted required output fields")
    return fields


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    baseline, candidate = args.baseline.resolve(strict=True), args.candidate.resolve(strict=True)
    fixture = ROOT / "tests/data/lmr_loss_positions_2026-10-18.json"
    data = ROOT / "docs/checks/2026-10-18-neural-v1/data/positions.jsonl"
    cases = [{"id": row["id"], "initial_fen": row["initial_fen"], "history": row["history"]}
             for row in json.loads(fixture.read_text(encoding="utf-8"))["records"]]
    cases.append({"id": "start", "initial_fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", "history": []})
    samples = [json.loads(line) for line in data.read_text(encoding="utf-8").splitlines()]
    for index in range(17):
        row = samples[index * (len(samples) - 1) // 16]
        cases.append({"id": f"{row['game_id']}-p{row['ply']}", "initial_fen": row["initial_fen"], "history": row["history"]})
    if len(cases) != 24:
        raise ValueError("expected 24 predefined positions")
    pairs = []
    for case in cases:
        old, new = query(baseline, case), query(candidate, case)
        differences = [key for key in sorted(set(old) | set(new)) if old.get(key) != new.get(key)]
        pairs.append({**case, "baseline": old, "candidate": new, "differences": differences,
                      "passed": not differences})
    report = {"purpose": "Default unrestricted classical equivalence only, not strength or timing evidence",
              "baseline_sha256": digest(baseline), "candidate_sha256": digest(candidate),
              "depth": 3, "node_limit": 30000, "excluded_output_fields": ["time_ms"],
              "source_sha256": {"helper": digest(__file__), "loss_fixture": digest(fixture), "learning_positions": digest(data)},
              "cases": len(cases), "passed": all(pair["passed"] for pair in pairs), "pairs": pairs}
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"{sum(pair['passed'] for pair in pairs)}/{len(pairs)} pairs identical apart from elapsed time")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
