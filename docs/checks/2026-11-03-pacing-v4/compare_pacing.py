"""Same-input v3/v4 target comparison; no engine call or real wait.

Reuses the five authored boards and exact raw packets already recorded in the
v3 diagnostic. Hypothetical clocks have zero elapsed time to isolate target
arithmetic. Those historical one-second searches were not performed under the
new allocations, so this cannot measure actual latency or playing strength.
"""
from pathlib import Path
import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import statistics
import subprocess
import sys
import types

ROOT = next(path for path in Path(__file__).resolve().parents
            if (path / "scripts/engine_pacing.py").is_file())
sys.path.insert(0, str(ROOT / "scripts"))
import chess
import chess.engine
import engine_pacing as current
from check_human_rhythm import packet_evidence

BASELINE = "79d0587863c099f810ec91c3226991020da9adab"
DATA = "docs/checks/2026-11-03-human-rhythm/diagnostic/records.jsonl"
SOURCES = ("scripts/engine_pacing.py", "scripts/decision_support.py", "scripts/evaluation_stability.py",
           "scripts/sparring_evaluation.py", "scripts/check_human_rhythm.py", "tests/pacing_v4_tests.py")
CLOCKS = (600000, 240000, 180000, 120000, 10000)
SEEDS = tuple(f"human-rhythm-diagnostic-{number:03d}" for number in range(32))


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def clock(board, left):
    return {"enabled": True, "status": "running", "running_color": "white" if board.turn else "black",
            "white_ms": left, "black_ms": left, "turn_elapsed_ms": 0, "increment_ms": 0}


def restore(info):
    result = copy.deepcopy(info)
    if isinstance(result.get("score"), dict):
        value = result["score"]
        side = value["pov"] == "white"
        white = chess.engine.Cp(value["cp_white"]) if value["cp_white"] is not None else chess.engine.Mate(value["mate_white"])
        result["score"] = chess.engine.PovScore(chess.engine.PovScore(white, chess.WHITE).pov(side), side)
    if isinstance(result.get("pv"), list):
        result["pv"] = [chess.Move.from_uci(uci) for uci in result["pv"]]
    return result


def run(output):
    output.mkdir(parents=True, exist_ok=False)
    hashes = lambda: {name: digest(ROOT / name) for name in SOURCES}
    before = hashes()
    raw = subprocess.check_output(["git", "show", BASELINE + ":scripts/engine_pacing.py"], cwd=ROOT)
    legacy = types.ModuleType("pinned_v3_pacing")
    exec(compile(raw, "<pinned-v3-pacing>", "exec"), legacy.__dict__)
    assert legacy.POLICY == "regicide-engine-pacing-v3"
    assert current.POLICY == "regicide-engine-pacing-v4"
    records = []
    inputs = [json.loads(line) for line in (ROOT / DATA).read_text(encoding="utf-8").splitlines()]
    assert len(inputs) == 5
    for item in inputs:
        spec = item["fixture"]
        board = chess.Board(spec["root_fen"])
        for uci in spec["history_uci"]:
            board.push_uci(uci)
        assert board.fen(en_passant="fen") == spec["fen"]
        events = [(event["received_ms"], restore(event["info"])) for event in item["native"]["events"]]
        move = chess.Move.from_uci(item["native"]["returned_uci"])
        packets, selected = packet_evidence(board, events, move.uci())
        assert selected is not None
        original = board.fen(en_passant="fen"), list(board.move_stack), copy.deepcopy(packets), copy.deepcopy(selected)
        for seed in SEEDS:
            for left in CLOCKS:
                state = clock(board, left)
                old_plan, new_plan = legacy.plan(board, state, seed=seed), current.plan(board, state, seed=seed)
                assert current.plan(board, state, seed=seed) == new_plan
                for key in ("game_tempo_factor", "local_beat_factor", "variation_factor", "recommended_total_ms"):
                    assert old_plan[key] == new_plan[key], key
                plans = []
                for module, plan in ((legacy, old_plan), (current, new_plan)):
                    plan["allocation"] = module.search_budget(plan, state, 5000, automatic=True)
                    original_plan = copy.deepcopy(plan)
                    refined = module.refine(board, plan, state, selected_move=move, baseline_packets=packets,
                                            selected_info=selected, seed=seed)
                    assert refined["allocation"] == original_plan["allocation"] and plan == original_plan
                    assert refined["target_total_ms"] <= min(45000, max(0, left - refined["reserve_ms"]))
                    if left <= 10000:
                        assert module.remaining_wait_ms(refined, state) == 0
                    plans.append(refined)
                old, new = plans
                assert new_plan["target_total_ms"] <= old_plan["target_total_ms"]
                assert new["target_total_ms"] <= old["target_total_ms"]
                records.append({"position": spec["id"], "seed": seed, "clock_ms": left,
                    "selected_uci": move.uci(), "v3_initial_target_ms": old_plan["target_total_ms"],
                    "v4_initial_target_ms": new_plan["target_total_ms"],
                    "v3_refined_target_ms": old["target_total_ms"], "v4_refined_target_ms": new["target_total_ms"],
                    "initial_reduction_ms": old_plan["target_total_ms"] - new_plan["target_total_ms"],
                    "refined_reduction_ms": old["target_total_ms"] - new["target_total_ms"],
                    "v3_pressure_factor": old["pressure_factor"], "v4_pressure_factor": new["pressure_factor"],
                    "v4_baseline_budget_ms": new_plan["allocation"]["baseline_ms"],
                    "v4_total_search_ms": new_plan["allocation"]["total_search_ms"],
                    "profile": new["rhythm_profile"]})
        assert (board.fen(en_passant="fen"), list(board.move_stack), packets, selected) == original
    (output / "records.jsonl").write_text("\n".join(json.dumps(row) for row in records) + "\n", encoding="utf-8")
    summaries = []
    for left in CLOCKS:
        rows = [row for row in records if row["clock_ms"] == left]
        summaries.append({"clock_ms": left, "cases": len(rows),
            "mean_initial_reduction_ms": round(statistics.mean(row["initial_reduction_ms"] for row in rows), 3),
            "mean_refined_reduction_ms": round(statistics.mean(row["refined_reduction_ms"] for row in rows), 3),
            "position_means": [{"position": item["fixture"]["id"],
                "mean_v3_refined_ms": round(statistics.mean(row["v3_refined_target_ms"] for row in rows if row["position"] == item["fixture"]["id"]), 3),
                "mean_v4_refined_ms": round(statistics.mean(row["v4_refined_target_ms"] for row in rows if row["position"] == item["fixture"]["id"]), 3)} for item in inputs]})
    test = subprocess.run([sys.executable, str(ROOT / "tests/pacing_v4_tests.py")], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    (output / "unit.log").write_bytes(test.stdout)
    assert test.returncode == 0, test.stdout.decode(errors="replace")
    after = hashes()
    assert before == after
    result = {"created_utc": datetime.now(timezone.utc).isoformat(), "all_passed": True, "cases": len(records),
              "source_sha256": before, "source_sha256_after": after, "sources_unchanged": True,
              "input_records": DATA, "input_sha256": digest(ROOT / DATA), "baseline_commit": BASELINE,
              "baseline_pacing_sha256": hashlib.sha256(raw).hexdigest(),
              "diagnostic_driver_sha256": digest(__file__), "unit_tests": 13, "clock_summaries": summaries,
              "scope": __doc__, "interpretation": "Target calculations only; changed automatic search budgets can change real search results. No new native search, artificial hold, strength or human-fit claim."}
    (output / "comparison.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"all_passed": True, "cases": len(records), "summaries": summaries}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    run(parser.parse_args().output.resolve())
