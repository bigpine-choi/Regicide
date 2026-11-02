"""Native evidence / deterministic clock-grid diagnostic for move timing.

Each authored board is searched once by the local Regicide binary. Those same
raw packets then drive timing calculations at multiple fictional clock values.
This does not measure waiting latency, human fit, move strength, or the cost of
searching under those hypothetical clock allocations.
"""
from __future__ import annotations

import argparse
from collections import Counter
import copy
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import platform
import statistics
import subprocess
import types

import chess

from check_sparring_evaluation import native_query
import decision_support
import engine_pacing
from runtime_engine import resolve_runtime_engine
import sparring_evaluation

ROOT = Path(__file__).resolve().parents[1]
V2_COMMIT = "49ff4ccec87051e0df3417a4ea6980902855a97b"
SOURCES = ("scripts/check_human_rhythm.py", "scripts/check_sparring_evaluation.py",
           "scripts/engine_pacing.py", "scripts/decision_support.py",
           "scripts/evaluation_stability.py", "scripts/sparring_evaluation.py",
           "scripts/runtime_engine.py")
CLOCKS_MS = (600000, 180000, 120000, 60000, 10000)
SEEDS = tuple(f"human-rhythm-diagnostic-{index:03d}" for index in range(32))


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def fixtures():
    """Repository-authored examples, not user games or an empirical dataset."""
    specifications = [
        {"id": "familiar_opening", "fen": chess.STARTING_FEN,
         "source": "tests/decision_clarity_tests.py initial-board fixture"},
        {"id": "only_legal_reply", "fen": "8/8/8/8/8/k7/8/Kr6 w - - 0 1",
         "source": "tests/decision_clarity_tests.py single-reply fixture"},
        {"id": "clear_queen_gain", "fen": "6k1/5ppp/8/8/3q4/8/3R4/5K2 w - - 0 30",
         "source": "tests/decision_clarity_tests.py FREE_QUEEN"},
        {"id": "tactical_choices", "fen": "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 12",
         "source": "tests/decision_support_tests.py TACTICAL"},
        {"id": "developed_position", "san": "e4 e5 Nf3 Nc6 Bb5 a6 Ba4 Nf6 O-O Be7 Re1 b5 Bb3 d6 c3 O-O h3 Nb8 d4 Nbd7 c4 c6 Nc3 Qc7",
         "source": "short legal demonstration line authored in this diagnostic; no game database"},
    ]
    result = []
    for item in specifications:
        board = chess.Board(item.get("fen", chess.STARTING_FEN))
        for san in item.get("san", "").split():
            board.push_san(san)
        assert board.is_valid() and not board.is_game_over(claim_draw=True), item["id"]
        result.append(({**item, "root_fen": board.root().fen(en_passant="fen"),
                        "fen": board.fen(en_passant="fen"),
                        "history_uci": [move.uci() for move in board.move_stack]}, board))
    return result


def clock(board, left, *, elapsed=0):
    color = "white" if board.turn else "black"
    return {"enabled": True, "status": "running", "running_color": color,
            "white_ms": left if board.turn else 600000,
            "black_ms": 600000 if board.turn else left,
            "turn_elapsed_ms": elapsed, "increment_ms": 0,
            "initial_ms": 600000, "control_id": "10+0"}


def legacy_module():
    source = subprocess.check_output(["git", "show", f"{V2_COMMIT}:scripts/engine_pacing.py"], cwd=ROOT)
    module = types.ModuleType("regicide_pacing_v2_diagnostic_reference")
    # This is pinned, local, version-controlled project code, not downloaded
    # content. Both timing versions use the same current recommendation code.
    exec(compile(source, "<pinned-v2-engine-pacing>", "exec"), module.__dict__)
    assert module.POLICY == "regicide-engine-pacing-v2"
    return module, hashlib.sha256(source).hexdigest()


def packet_evidence(board, events, returned_uci):
    packets, selected = {}, None
    for _elapsed, info in events:
        if isinstance(info, dict) and type(info.get("depth")) is int and "score" in info:
            packets[info["depth"]] = copy.deepcopy(info)
    # Match the host: a later bound is useful instability evidence, but does
    # not erase an earlier completed exact line for the returned move.
    for depth in sorted(packets, reverse=True):
        sample = sparring_evaluation.validate_packet(board, packets[depth])
        if sample is not None and sample["root"] == returned_uci:
            selected = copy.deepcopy(packets[depth])
            break
    return packets, selected


def grid(board, packets, selected_info, move, elapsed_ms, old_module, *, seeds=SEEDS, clocks=CLOCKS_MS):
    """Check functional invariants without sleeping or issuing engine calls."""
    original_board = (board.fen(en_passant="fen"), list(board.move_stack))
    original_packets, original_info = copy.deepcopy(packets), copy.deepcopy(selected_info)
    rows = []
    for seed in seeds:
        targets = []
        for left in clocks:
            initial_clock = clock(board, left)
            # Replay the measured diagnostic search cost against fictional
            # clocks. The native search itself did not use these allocations.
            spent = min(elapsed_ms, left)
            fresh_clock = clock(board, max(0, left - spent), elapsed=spent)
            original_clock = copy.deepcopy(fresh_clock)
            planned = engine_pacing.plan(board, initial_clock, seed=seed, history={})
            planned["allocation"] = engine_pacing.search_budget(planned, initial_clock, 1000, automatic=True)
            original_plan = copy.deepcopy(planned)
            refined = engine_pacing.refine(board, planned, fresh_clock, selected_move=move,
                baseline_packets=packets, selected_info=selected_info, history={}, seed=seed)
            repeated = engine_pacing.refine(board, planned, fresh_clock, selected_move=move,
                baseline_packets=packets, selected_info=selected_info, history={}, seed=seed)
            assert refined == repeated, "Same state/seed is not deterministic"
            assert planned == original_plan and fresh_clock == original_clock, "Input mutated"
            assert refined["allocation"] == original_plan["allocation"], "Postsearch allocation changed"
            assert 0 <= refined["target_total_ms"] <= 45000, "Global target cap violated"
            assert refined["target_total_ms"] <= spent + max(0, left - spent), "Future clock borrowed"
            wait_ms = engine_pacing.remaining_wait_ms(refined, fresh_clock)
            own_remaining = fresh_clock[refined["color"] + "_ms"]
            assert 0 <= wait_ms <= max(0, own_remaining - refined["reserve_ms"]), "Clock reserve violated"
            if own_remaining <= 10000:
                assert wait_ms == 0, "Low-clock extra hold"
            old = old_module.plan(board, initial_clock, seed=seed)
            rows.append({"seed": seed, "initial_clock_ms": left,
                "simulated_postsearch_clock_ms": own_remaining, "diagnostic_search_elapsed_ms": elapsed_ms,
                "v2_initial_target_ms": old["target_total_ms"],
                "v3_initial_target_ms": planned["target_total_ms"],
                "v3_refined_target_ms": refined["target_total_ms"], "remaining_hold_ms": wait_ms,
                "allocated_baseline_ms": planned["allocation"]["baseline_ms"],
                "rhythm_profile": refined["rhythm_profile"], "refinement": refined.get("refinement"),
                "game_tempo_factor": refined.get("game_tempo_factor"),
                "local_beat_factor": refined.get("local_beat_factor"),
                "selected_evidence_available": selected_info is not None,
                "checks": {"deterministic": True, "allocation_unchanged": True,
                           "clock_reserve": True, "target_cap": True, "inputs_unchanged": True}})
            targets.append(refined["target_total_ms"])
        if tuple(clocks) == tuple(sorted(clocks, reverse=True)):
            assert targets == sorted(targets, reverse=True), "Less time produced a longer target"
    assert (board.fen(en_passant="fen"), list(board.move_stack)) == original_board
    assert packets == original_packets and selected_info == original_info
    return rows


def summarize(rows):
    result = []
    for item in rows:
        full = [row for row in item["timings"] if row["initial_clock_ms"] == 600000]
        result.append({"id": item["fixture"]["id"], "native_depth": item["selected_sample"]["depth"] if item["selected_sample"] else None,
            "clarity": item["complexity"]["clarity"]["kind"], "complexity": item["complexity"]["score"],
            "grid_cases": len(item["timings"]), "profiles_at_600s": dict(Counter(row["rhythm_profile"] for row in full)),
            "v2_mean_target_ms_at_600s": round(statistics.mean(row["v2_initial_target_ms"] for row in full), 3),
            "v3_mean_initial_ms_at_600s": round(statistics.mean(row["v3_initial_target_ms"] for row in full), 3),
            "v3_mean_refined_ms_at_600s": round(statistics.mean(row["v3_refined_target_ms"] for row in full), 3),
            "v3_min_refined_ms_at_600s": min(row["v3_refined_target_ms"] for row in full),
            "v3_max_refined_ms_at_600s": max(row["v3_refined_target_ms"] for row in full)})
    return {"positions": len(rows), "grid_cases": sum(len(row["timings"]) for row in rows),
            "all_invariants_passed": True, "cases": result,
            "scope": "Model targets from actual engine evidence; no artificial wait was executed; no human-data fit, Elo, or subjective naturalness claim"}


def run(args):
    if not 100 <= args.search_ms <= 5000:
        raise ValueError("Use a bounded 100..5000ms diagnostic search")
    output = args.output.resolve()
    if output.exists():
        raise ValueError("Use a new output directory")
    binary = (args.binary or resolve_runtime_engine(ROOT)).resolve(strict=True)
    old, old_hash = legacy_module()
    sources = {name: digest(ROOT / name) for name in SOURCES}
    native_binary_hash = digest(binary)
    inputs = fixtures()
    input_document = [item for item, _board in inputs]
    input_bytes = (json.dumps(input_document, ensure_ascii=False, indent=2) + "\n").encode()
    output.mkdir(parents=True)
    (output / "positions.json").write_bytes(input_bytes)
    manifest = {"schema": "regicide.human-rhythm-diagnostic.v1", "created_utc": datetime.now(timezone.utc).isoformat(),
        "binary": str(binary), "binary_sha256": native_binary_hash, "source_sha256": sources,
        "positions_sha256": hashlib.sha256(input_bytes).hexdigest(), "search_ms": args.search_ms,
        "seed_count": len(SEEDS), "seeds": list(SEEDS), "clocks_ms": list(CLOCKS_MS),
        "v2_reference_commit": V2_COMMIT, "v2_reference_source_sha256": old_hash,
        "python": platform.python_version(), "platform": platform.platform(),
        "scope": __doc__, "limitations": [
            "Five authored examples are development checks, not held-out human behavior data.",
            "Each engine search is performed once at the diagnostic time; fictional clock grids reuse its result.",
            "Both v2 and v3 timing use the current decision_support recommendation code; only pacing policy differs.",
            "The diagnostic has no authenticated past-turn receipts, so forecast surprise/continuation/cooldown are tested separately in unit and host tests.",
            "No actual artificial hold is executed or wall-clock human cadence claimed.",
            "Extra timing does not select a move, prove accuracy, or demonstrate strength."]}
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    logger = logging.getLogger("chess.engine")
    handler = logging.FileHandler(output / "uci-protocol.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    rows = []
    try:
        with (output / "records.jsonl").open("x", encoding="utf-8") as stream:
            for item, board in inputs:
                trace, events = native_query(binary, board, args.search_ms)
                packets, selected = packet_evidence(board, events, trace["returned_uci"])
                move = chess.Move.from_uci(trace["returned_uci"])
                timings = grid(board, packets, selected, move, trace["completed_ms"], old)
                row = {"fixture": item, "native": trace, "selected_sample": sparring_evaluation.validate_packet(board, selected),
                       "complexity": decision_support.assess(board, board.turn, clock(board, 600000))["complexity"],
                       "timings": timings}
                rows.append(row)
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                stream.flush()
                print(json.dumps({"id": item["id"], "checks": len(timings), "passed": True}), flush=True)
    finally:
        logger.removeHandler(handler)
        handler.close()
        logger.setLevel(old_level)
    unchanged = digest(binary) == native_binary_hash and all(digest(ROOT / name) == value for name, value in sources.items())
    summary = summarize(rows)
    summary.update(inputs_unchanged=unchanged, finished_utc=datetime.now(timezone.utc).isoformat())
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert unchanged, "Inputs changed during native diagnostic"
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--search-ms", type=int, default=1000)
    parser.add_argument("--output", type=Path, required=True)
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
