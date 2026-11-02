"""Same-input v4/v5 target distributions; no engine call or real wait.

Reuses five authored positions and exact raw search packets recorded by the
v3 diagnostic. All hypothetical clocks start at zero elapsed time, without
previous-turn history. The old one-second searches were not performed under
these allocations. These are timing-policy calculations, not actual response
times, a sample of real games, human-fit measurements, or playing strength.
"""
from pathlib import Path
import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import math
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

BASELINE = "79d230f3806484a0fd0997b7db94900ffcdea96a"
DATA = "docs/checks/2026-11-03-human-rhythm/diagnostic/records.jsonl"
SOURCES = ("scripts/engine_pacing.py", "scripts/decision_support.py", "scripts/evaluation_stability.py",
           "scripts/sparring_evaluation.py", "scripts/check_human_rhythm.py")
CLOCKS = (600000, 240000, 180000, 120000, 60000, 10000)
SEEDS = tuple(f"human-rhythm-diagnostic-{number:03d}" for number in range(128))


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


def distribution(rows, version):
    values = sorted(row[version + "_refined_target_ms"] for row in rows)
    # Nearest rank percentiles are transparent for this finite seeded sample.
    percentile = lambda fraction: values[max(0, math.ceil(len(values) * fraction) - 1)]
    long_values = [row[version + "_refined_target_ms"] for row in rows
                   if row[version + "_profile"] == "long_deliberation"]
    return {"count": len(values), "mean_ms": round(statistics.mean(values), 3),
            "p10_ms": percentile(.10), "p50_ms": percentile(.50),
            "p90_ms": percentile(.90), "p99_ms": percentile(.99), "max_ms": max(values),
            "at_most_150_ms_count": sum(value <= 150 for value in values),
            "long_deliberation_count": len(long_values),
            "long_deliberation_mean_ms": round(statistics.mean(long_values), 3) if long_values else None}


def summarize(rows):
    return {"cases": len(rows), "v4": distribution(rows, "v4"), "v5": distribution(rows, "v5"),
            "mean_initial_reduction_ms": round(statistics.mean(row["initial_reduction_ms"] for row in rows), 3),
            "mean_refined_reduction_ms": round(statistics.mean(row["refined_reduction_ms"] for row in rows), 3)}


def run(output):
    output.mkdir(parents=True, exist_ok=False)
    hashes = lambda: {name: digest(ROOT / name) for name in SOURCES}
    before = hashes()
    raw = subprocess.check_output(["git", "show", BASELINE + ":scripts/engine_pacing.py"], cwd=ROOT)
    legacy = types.ModuleType("pinned_v4_pacing")
    exec(compile(raw, "<pinned-v4-pacing>", "exec"), legacy.__dict__)
    assert legacy.POLICY == "regicide-engine-pacing-v4"
    assert current.POLICY == "regicide-engine-pacing-v5"
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
                records.append({"position": spec["id"], "seed": seed, "clock_ms": left,
                    "selected_uci": move.uci(), "v4_initial_target_ms": old_plan["target_total_ms"],
                    "v5_initial_target_ms": new_plan["target_total_ms"],
                    "v4_refined_target_ms": old["target_total_ms"], "v5_refined_target_ms": new["target_total_ms"],
                    "initial_reduction_ms": old_plan["target_total_ms"] - new_plan["target_total_ms"],
                    "refined_reduction_ms": old["target_total_ms"] - new["target_total_ms"],
                    "v4_pressure_factor": old["pressure_factor"], "v5_pressure_factor": new["pressure_factor"],
                    "v5_baseline_budget_ms": new_plan["allocation"]["baseline_ms"],
                    "v5_total_search_ms": new_plan["allocation"]["total_search_ms"],
                    "v4_profile": old["rhythm_profile"], "v5_profile": new["rhythm_profile"]})
        assert (board.fen(en_passant="fen"), list(board.move_stack), packets, selected) == original
    (output / "records.jsonl").write_text("\n".join(json.dumps(row) for row in records) + "\n", encoding="utf-8")
    summaries = []
    for left in CLOCKS:
        rows = [row for row in records if row["clock_ms"] == left]
        summaries.append({"clock_ms": left, **summarize(rows),
            "positions": [{"position": item["fixture"]["id"],
                **summarize([row for row in rows if row["position"] == item["fixture"]["id"]])} for item in inputs]})
    after = hashes()
    assert before == after
    result = {"created_utc": datetime.now(timezone.utc).isoformat(), "all_passed": True, "cases": len(records),
              "source_sha256": before, "source_sha256_after": after, "sources_unchanged": True,
              "input_records": DATA, "input_sha256": digest(ROOT / DATA), "baseline_commit": BASELINE,
              "baseline_pacing_sha256": hashlib.sha256(raw).hexdigest(),
              "diagnostic_driver_sha256": digest(__file__), "seeds": list(SEEDS),
              "percentiles": "nearest rank", "clock_summaries": summaries,
              "scope": __doc__, "interpretation": "Target calculations only. Changed automatic search budgets can change actual search results. No new native search, hold, strength or human-fit measurement. Long counts describe selected profiles, not completed human-visible pauses. Clock rows have zero elapsed time and no previous-turn cooldown history."}
    (output / "comparison.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"all_passed": True, "cases": len(records),
                      "clocks": [{key: value for key, value in row.items() if key != "positions"} for row in summaries]}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    run(parser.parse_args().output.resolve())
