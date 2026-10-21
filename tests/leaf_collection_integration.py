"""Bounded collector validation and prior-binary parity; never trains or labels."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import time

import chess

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run(command, stdin=None):
    return subprocess.run([str(x) for x in command], input=stdin, text=True,
                          capture_output=True, check=True, timeout=60).stdout


def search(binary, model, row, switches):
    command = [binary, "search", "64", "--nodes", "10000", "--hash", "16",
               "--nnue", "--nnue-model", model, *switches,
               "--fen", row.get("initial_fen", chess.STARTING_FEN)]
    if row["history"]:
        command += ["--play", *row["history"]]
    result = dict(line.split(":", 1) for line in run(command).splitlines())
    result.pop("time_ms")
    return {key: value.strip() for key, value in result.items()}


def validate(old_binary, new_binary, collector, model):
    started = time.perf_counter()
    source = ROOT / "docs/checks/2026-10-22-nnue-generalization/data-001/selected-roots.json"
    # These are already-exposed development roots. Never inspect reserved new
    # gate positions for implementation validation.
    roots = json.loads(source.read_text(encoding="utf-8"))[::5][:12]
    assert len(roots) == 12
    parity = []
    for index, row in enumerate(roots):
        for name, flags in [("default", []), ("full_refresh", ["--nnue-full-refresh"]),
                            ("no_tt", ["--no-tt"])]:
            before = search(old_binary, model, row, flags)
            after = search(new_binary, model, row, flags)
            assert before == after, (index, name, before, after)
            parity.append({"root_index": index * 5, "configuration": name,
                           "bestmove": after["bestmove"], "score_cp": after.get("score_cp"),
                           "nodes": int(after["nodes"]), "depth": int(after["depth"]),
                           "all_reported_non_time_fields_equal": True})
    stdin = "".join(f"check-{i}\t{row.get('initial_fen', chess.STARTING_FEN)}\t"
                    + " ".join(row["history"]) + "\n" for i, row in enumerate(roots[:2]))
    command = [collector, "--model", model, "--nodes", "10000", "--samples", "4",
               "--seed", "2026102204"]
    first, second = run(command, stdin), run(command, stdin)
    assert first == second, "collector not deterministic"
    records = [json.loads(line) for line in first.splitlines()]
    all_samples = []
    for record in records:
        sampling = record["sampling"]
        assert sampling["total_static_evaluations"] == (sampling["eligible_static_evaluations"]
            + sampling["excluded_checked"] + sampling["excluded_draw_claim"])
        assert len(record["samples"]) == 4
        assert record["search"]["nodes"] == 10000
        assert record["search"]["stopped"]
        assert sampling["samples_from_unfinished_iteration"] == sum(
            sample["iteration_depth"] > record["search"]["depth"] for sample in record["samples"])
        for sample in record["samples"]:
            assert sample["history"] == record["root_history"] + sample["path_from_root"]
            board = chess.Board(record["initial_fen"])
            for move in sample["history"]:
                board.push_uci(move)
            assert board.fen(en_passant="fen") == sample["fen"]
            assert len(sample["path_from_root"]) == sample["ply"]
            assert not board.is_check() and not board.is_game_over(claim_draw=False)
            assert not sample["current_draw_claim"] and not sample["draw_claim_adjusted"]
            assert sample["phase"] == "quiescence_stand_pat" and sample["qply"] >= 0
            all_samples.append(sample)
    scores = run([old_binary, "eval-batch", "--nnue-model", model],
                 "".join(s["fen"] + "\n" for s in all_samples)).splitlines()
    for sample, score in zip(all_samples, scores, strict=True):
        _, full, incremental = map(int, score.split("\t"))
        assert sample["raw_score_cp"] == full == incremental
    bad = subprocess.run([str(x) for x in command], input="bad\tfen\tnot-uci\n", text=True,
                         capture_output=True, timeout=30)
    assert bad.returncode != 0 and not bad.stdout
    return {"completed": True, "passed": True,
            "scope": "12 previously exposed roots x 3 configurations; 2-root collector smoke only",
            "old_binary_sha256": sha(old_binary), "new_binary_sha256": sha(new_binary),
            "collector_sha256": sha(collector), "model_sha256": sha(model),
            "source_sha256": sha(source), "parity_cases": len(parity), "parity": parity,
            "collector_reproducible": True, "collector_samples": len(all_samples),
            "sample_replay_full_incremental_mismatches": 0, "invalid_input_rejected": True,
            "wall_seconds": time.perf_counter() - started}


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("old_binary", type=Path)
    parser.add_argument("new_binary", type=Path)
    parser.add_argument("collector", type=Path)
    parser.add_argument("model", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = validate(*(p.resolve() for p in (args.old_binary, args.new_binary, args.collector, args.model)))
    if args.output:
        if args.output.exists():
            raise FileExistsError(args.output)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("passed", "parity_cases", "collector_samples", "wall_seconds")}))


if __name__ == "__main__":
    main()
