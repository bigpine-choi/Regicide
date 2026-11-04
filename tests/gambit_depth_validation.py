"""Record deeper Gambit search under the same planned visible turn allowance.

This is a development diagnostic, not an equal-CPU or playing-strength test.
Run with --binary PATH --output PATH; each run requires fresh report paths.
"""
from datetime import datetime, timezone
import argparse
import copy
import json
import logging
import os
from pathlib import Path
import platform
import sys

import chess

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import engine_pacing
from game_clock import GameClock
from perpetual_check_validation import native_query, sha


SEED = "gambit-deeper-search-v1"
CASES = (
    ("reported-perpetual-root-minimal", "r4k2/2p5/1pqp4/5r2/p3R2R/2B3P1/PP3P1P/6K1 b - - 3 31"),
    ("kiwipete-control", "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1"),
    ("rook-ending-control", "8/6kp/5p2/3r4/3P2P1/2R4P/5PK1/8 w - - 0 40"),
)


def allocation(fen):
    board = chess.Board(fen)
    if not board.is_valid() or board.is_game_over(claim_draw=False):
        raise ValueError("A declared root must be valid and nonterminal")
    clock = GameClock("10+0", monotonic=lambda: 100.0)
    clock.restore_ply(0, "white" if board.turn else "black")
    snapshot = clock.snapshot()
    pacing = engine_pacing.plan(board, snapshot, seed=SEED)
    original = copy.deepcopy((snapshot, pacing))
    old = engine_pacing.search_budget(pacing, snapshot, 1000, automatic=True, deepen=False)
    new = engine_pacing.search_budget(pacing, snapshot, 1000, automatic=True, deepen=True)
    if original != (snapshot, pacing):
        raise ValueError("Allocating search mutated the shared turn plan")
    if old["total_search_ms"] != new["total_search_ms"]:
        raise ValueError("Visible turn comparison unexpectedly changed the total allowance")
    if not (0 < old["baseline_ms"] < new["baseline_ms"] <= 30_000):
        raise ValueError("Declared case does not yield a bounded deeper allocation")
    visible = engine_pacing.wait_target_ms(pacing)
    if new["baseline_ms"] + new["optional_ms"] + new["completion_margin_ms"] > visible:
        raise ValueError("New search exceeds the pre-existing visible turn target")
    return board, {"clock": snapshot, "pacing": pacing, "visible_turn_target_ms": visible,
                   "allocations": {"old": old, "new": new}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        binary = args.binary.resolve(strict=True)
        if not binary.is_file():
            raise ValueError("Binary must be a file")
        output = args.output.resolve()
        raw_log = output.with_suffix(".uci.log")
        if output.exists() or raw_log.exists():
            raise ValueError("Use fresh report and raw-log paths")
        prepared = [(case_id, *allocation(fen)) for case_id, fen in CASES]
        output.parent.mkdir(parents=True, exist_ok=True)
    except (OSError, ValueError, KeyError) as error:
        parser.error(str(error))
    sources = [Path(__file__).resolve(), ROOT / "tests/perpetual_check_validation.py",
               ROOT / "scripts/engine_pacing.py", ROOT / "scripts/play_human.py",
               ROOT / "scripts/game_clock.py", ROOT / "scripts/decision_support.py",
               ROOT / "src/search.cpp", binary]
    hashes = {str(path): sha(path) for path in sources}
    report = {"schema": "regicide.gambit-deeper-search-diagnostic.v1",
              "started_utc": datetime.now(timezone.utc).isoformat(), "source_sha256": hashes,
              "python": platform.python_version(), "python_chess": chess.__version__,
              "platform": platform.platform(), "processor": platform.processor(),
              "logical_cpus": os.cpu_count(), "raw_uci_log": str(raw_log),
              "config": {"binary": str(binary), "seed": SEED, "time_control": "10+0",
                         "initial_clock_each_ms": 600_000, "evaluation": "classical_activity_v1",
                         "configured_movetime_ms": 1000, "automatic": True,
                         "execution": "Sequential fresh native processes; old/new, new/old, old/new",
                         "comparison": "Same planned visible turn time; unequal native search time and CPU",
                         "optional_analysis": "No optional reserve in these direct pacing-only roots"},
              "limitations": ["Three predeclared development roots are not a playing-strength benchmark",
                              "Increased depth or changed score/move does not establish better play",
                              "The reported game root uses its minimal FEN, without private game history",
                              "The existing visible turn target is held fixed; old/new CPU time is not equal",
                              "Host integration and actual visible wait are separate from these direct queries"],
              "cases": [], "errors": [], "status": "running"}
    logger = logging.getLogger("chess.engine")
    previous_level = logger.level
    handler = logging.FileHandler(raw_log, mode="x", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        def checkpoint():
            stream.seek(0)
            stream.write(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
            stream.truncate()
            stream.flush()
        checkpoint()
        try:
            for index, (case_id, board, details) in enumerate(prepared):
                order = ["old", "new"] if index % 2 == 0 else ["new", "old"]
                case = {"id": case_id, "fen": board.fen(en_passant="fen"), "history_uci": [],
                        **details, "query_order": order, "queries": {}}
                report["cases"].append(case)
                checkpoint()
                for version in order:
                    duration = details["allocations"][version]["baseline_ms"]
                    query = native_query(binary, board, duration)
                    case["queries"][version] = query
                    if query["status"] != "recorded":
                        report["errors"].append({"case": case_id, "version": version, "error": query["error"]})
                    checkpoint()
                    line = query.get("completed_line") or {}
                    print(json.dumps({"case": case_id, "version": version, "allocated_ms": duration,
                                      "status": query["status"], "move": query["selected_uci"],
                                      "depth": line.get("depth"), "nodes": line.get("nodes"),
                                      "cp_root": line.get("cp_root"), "elapsed_ms": query["elapsed_ms"]}), flush=True)
                old = case["queries"]["old"].get("completed_line")
                new = case["queries"]["new"].get("completed_line")
                case["observations"] = ({"completed_depth_delta": new["depth"] - old["depth"],
                                          "move_changed": case["queries"]["old"]["selected_uci"] != case["queries"]["new"]["selected_uci"]}
                                         if old and new else None)
                checkpoint()
        except Exception as error:
            report["errors"].append({"error": f"{type(error).__name__}: {error}"})
        finally:
            report["source_sha256_after"] = {str(path): sha(path) for path in sources}
            report["sources_unchanged"] = report["source_sha256_after"] == hashes
            if not report["sources_unchanged"]:
                report["errors"].append({"error": "Source or binary changed during the experiment"})
            report["finished_utc"] = datetime.now(timezone.utc).isoformat()
            report["status"] = "recorded" if not report["errors"] and len(report["cases"]) == len(CASES) else "failed"
            checkpoint()
            logger.removeHandler(handler)
            logger.setLevel(previous_level)
            handler.close()
    return 0 if report["status"] == "recorded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
