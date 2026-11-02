"""Native convergence diagnostic for the sparring display evaluator.

Every development root receives one uninterrupted own-engine search. The old
three-second endpoint, adaptive policy endpoint, and longer own-engine reference
are reconstructed only from packets received by each cutoff. This measures
search convergence with additional resources, not chess accuracy or Elo.
"""

from __future__ import annotations

import argparse
import copy
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import threading
import time

import chess
import chess.engine

from check_evaluation_quality import load_positions
from runtime_engine import resolve_runtime_engine
import sparring_evaluation as policy


ROOT = Path(__file__).resolve().parents[1]
SOURCE_FILES = (
    "scripts/check_sparring_evaluation.py", "scripts/sparring_evaluation.py",
    "scripts/check_evaluation_quality.py", "scripts/runtime_engine.py",
    "tests/boss_trap_integration.py",
)


class SystemLoadSampler:
    """Total system CPU, including our search; never an isolation certificate."""
    def __init__(self):
        self.samples = []
        self.stopped = threading.Event()
        self.worker = None
        self.error = None

    @staticmethod
    def counters():
        if os.name != "nt":
            return None
        idle, kernel, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
        if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            raise OSError("GetSystemTimes failed")
        ticks = lambda value: (value.dwHighDateTime << 32) | value.dwLowDateTime
        return ticks(idle), ticks(kernel) + ticks(user)

    def start(self):
        def sample():
            try:
                previous = self.counters()
                while not self.stopped.wait(1.0):
                    current = self.counters()
                    if previous is not None and current is not None and current[1] > previous[1]:
                        busy = 100 * (1 - (current[0] - previous[0]) / (current[1] - previous[1]))
                        self.samples.append({"utc": datetime.now(timezone.utc).isoformat(),
                                             "total_system_cpu_percent": round(busy, 3)})
                    previous = current
            except Exception as error:
                self.error = f"{type(error).__name__}: {error}"
        self.worker = threading.Thread(target=sample, name="sparring-diagnostic-load", daemon=True)
        self.worker.start()

    def finish(self):
        self.stopped.set()
        if self.worker is not None:
            self.worker.join(timeout=2)
        values = [item["total_system_cpu_percent"] for item in self.samples]
        return {"scope": "Total system CPU across all logical processors, includes diagnostic engine; not external-process attribution or exclusive-CPU evidence",
                "method": "Windows GetSystemTimes; approximately 1-second intervals" if os.name == "nt" else "unavailable",
                "sample_count": len(values), "mean_percent": statistics.mean(values) if values else None,
                "max_percent": max(values) if values else None, "error": self.error, "samples": self.samples}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def json_packet(value):
    """Preserve all protocol fields, including bounds and partial packets."""
    if isinstance(value, chess.engine.PovScore):
        return {"pov": "white" if value.turn else "black",
                "cp_white": value.white().score(), "mate_white": value.white().mate(),
                "score_white_with_mate": value.white().score(mate_score=100000)}
    if isinstance(value, chess.Move):
        return value.uci()
    if isinstance(value, dict):
        return {str(key): json_packet(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_packet(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def score_iteration(info):
    return (isinstance(info, dict) and "score" in info
            and type(info.get("depth")) is int and info["depth"] >= 1)


def at_cutoff(board, events, cutoff_ms):
    """Only completed legal exact packets available at the cutoff may count."""
    packets, selected, rejected = {}, None, 0
    for received_ms, info in events:
        if received_ms > cutoff_ms:
            break
        if not score_iteration(info):
            continue
        depth = info["depth"]
        packets[depth] = info
        accepted = policy.validate_packet(board, info)
        if accepted is None:
            rejected += 1
        elif selected is None or depth >= selected["depth"]:
            selected = {**copy.deepcopy(accepted), "received_ms": received_ms,
                        "nodes": info.get("nodes"), "engine_time_ms": info.get("time", 0) * 1000}
    return {"cutoff_ms": cutoff_ms, "result": selected,
            "converged": policy.converged(board, packets),
            "rejected_score_packets": rejected}, packets


def endpoints(board, events, completed_ms, *, base_ms=None, max_ms=None):
    """Model the policy without looking beyond its stopping time.

    A convergence decision at 3000ms uses the state at 3000ms even when the
    next completed iteration arrives much later. No future packet is borrowed.
    Natural early completion uses that search's final available result.
    """
    base_ms = policy.BASE_BUDGET_MS if base_ms is None else base_ms
    max_ms = policy.MAX_BUDGET_MS if max_ms is None else max_ms
    if not 0 < base_ms <= max_ms or completed_ms < 0:
        raise ValueError("Invalid endpoint budgets")
    if any(events[i][0] > events[i + 1][0] for i in range(len(events) - 1)):
        raise ValueError("Events must be ordered by receipt time")
    baseline, _ = at_cutoff(board, events, min(base_ms, completed_ms))
    stop_ms = min(max_ms, completed_ms)
    stop_reason = "search_completed" if completed_ms <= max_ms else "maximum_budget"
    if completed_ms > base_ms:
        checkpoints = [base_ms] + [ms for ms, info in events
                                  if base_ms < ms <= stop_ms and score_iteration(info)]
        for elapsed in checkpoints:
            snapshot, _ = at_cutoff(board, events, elapsed)
            if snapshot["converged"]:
                stop_ms, stop_reason = elapsed, "converged"
                break
    adaptive, _ = at_cutoff(board, events, stop_ms)
    adaptive["stop_reason"] = stop_reason
    reference, _ = at_cutoff(board, events, completed_ms)
    return {"baseline_3s": baseline, "adaptive": adaptive, "own_reference": reference}


def compare_to_reference(endpoint, reference):
    """Same engine's finite later result is a convergence reference only."""
    own, later = endpoint.get("result"), reference.get("result")
    if own is None or later is None:
        return {"status": "unavailable"}
    result = {"status": "cp" if own.get("cp") is not None and later.get("cp") is not None else "mate_separate",
              "same_root": own["root"] == later["root"],
              "depth_gap": later["depth"] - own["depth"]}
    if result["status"] == "cp":
        result["absolute_own_cp_difference"] = abs(own["cp"] - later["cp"])
        # +/-100cp are display bands, not winning-probability estimates.
        band = lambda cp: "white" if cp > 100 else "black" if cp < -100 else "balanced"
        result["same_display_band"] = band(own["cp"]) == band(later["cp"])
    else:
        result.update(endpoint_mate=own.get("mate"), reference_mate=later.get("mate"),
                      same_mate_winner=own.get("mate") is not None and later.get("mate") is not None
                      and own.get("winner") == later.get("winner"))
    return result


def native_query(binary, board, milliseconds):
    """One fresh hidden process, full history, bounded watchdog, all packets."""
    extra = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    events, expired = [], threading.Event()
    with chess.engine.SimpleEngine.popen_uci([str(binary), "uci"], timeout=10, **extra) as engine:
        options = {"Hash": 16, "UsePieceActivity": True,
                   "UseNeuralEvaluation": False, "UseNNUEEvaluation": False}
        if "Threads" in engine.options:
            options["Threads"] = 1
        if "Move Overhead" in engine.options:
            options["Move Overhead"] = 20
        engine.configure(options)
        engine.configure({"Clear Hash": None})
        metadata = {"engine_id": dict(engine.id), "configured_options": options,
                    "search_threads": 1, "threads_uci_option": "Threads" in engine.options,
                    "default_options": {key: value.default for key, value in engine.options.items()}}
        started = time.monotonic()
        def timeout():
            expired.set()
            engine.close()
        guard = threading.Timer(milliseconds / 1000 + 5, timeout)
        guard.daemon = True
        guard.start()
        try:
            with engine.analysis(board.copy(stack=True), chess.engine.Limit(time=milliseconds / 1000),
                                 game=object(), info=chess.engine.INFO_ALL) as analysis:
                for info in analysis:
                    events.append(((time.monotonic() - started) * 1000, dict(info)))
                reply = analysis.wait()
            completed_ms = (time.monotonic() - started) * 1000
            if expired.is_set():
                raise TimeoutError("Native search exceeded bounded watchdog")
            if reply.move is None or reply.move not in board.legal_moves:
                raise ValueError("Native result is not a legal root move")
            result = {"requested_ms": milliseconds, "completed_ms": completed_ms,
                      "returned_uci": reply.move.uci(), "engine": metadata,
                      "events": [{"received_ms": ms, "info": json_packet(info)} for ms, info in events]}
            return result, events
        finally:
            guard.cancel()
            guard.join(timeout=.5)


def summarize(rows):
    summary = {"positions": len(rows), "complete": sum(row.get("status") == "ok" for row in rows),
               "failed_ids": [row["position"]["id"] for row in rows if row.get("status") != "ok"],
               "comparison_scope": "Same-search, same-engine convergence toward a later finite result; not accuracy or Elo"}
    for label in ("baseline_3s", "adaptive"):
        comparisons = [row["comparisons"][label] for row in rows if row.get("status") == "ok"]
        cp = [item["absolute_own_cp_difference"] for item in comparisons if item["status"] == "cp"]
        times = [row["endpoints"][label]["cutoff_ms"] for row in rows if row.get("status") == "ok"]
        summary[label] = {"root_matches": sum(item.get("same_root", False) for item in comparisons),
                          "cp_pairs": len(cp),
                          "mean_absolute_own_cp_difference": statistics.mean(cp) if cp else None,
                          "median_absolute_own_cp_difference": statistics.median(cp) if cp else None,
                          "max_absolute_own_cp_difference": max(cp) if cp else None,
                          "same_display_band": sum(item.get("same_display_band", False) for item in comparisons),
                          "mean_cutoff_ms": statistics.mean(times) if times else None}
    pairs = [(row["comparisons"]["baseline_3s"], row["comparisons"]["adaptive"])
             for row in rows if row.get("status") == "ok"]
    differences = [old["absolute_own_cp_difference"] - new["absolute_own_cp_difference"]
                   for old, new in pairs if old["status"] == new["status"] == "cp"]
    summary["cp_convergence_counts"] = {"closer": sum(value > 0 for value in differences),
                                         "unchanged": sum(value == 0 for value in differences),
                                         "farther": sum(value < 0 for value in differences)}
    summary["adaptive_stops"] = {reason: sum(row.get("endpoints", {}).get("adaptive", {}).get("stop_reason") == reason
                                             for row in rows)
                                   for reason in ("converged", "maximum_budget", "search_completed")}
    return summary


def run(args):
    if not policy.MAX_BUDGET_MS < args.reference_ms <= 30000:
        raise ValueError("Reference must be longer than the adaptive maximum and at most 30 seconds")
    output = args.output.resolve()
    if output.exists():
        raise ValueError("Use a fresh output directory; failed cases must remain recorded")
    document, roots = load_positions(args.positions.resolve(strict=True))
    binary = (args.binary or resolve_runtime_engine(ROOT)).resolve(strict=True)
    source_hashes = {name: digest(ROOT / name) for name in SOURCE_FILES}
    manifest = {"schema": "regicide.sparring-evaluation.diagnostic.v1",
                "created_utc": datetime.now(timezone.utc).isoformat(),
                "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                "binary": str(binary), "binary_sha256": digest(binary),
                "input_path": str(args.positions.resolve()), "input_sha256": digest(args.positions),
                "source_sha256": source_hashes, "policy": policy.POLICY,
                "base_budget_ms": policy.BASE_BUDGET_MS, "maximum_budget_ms": policy.MAX_BUDGET_MS,
                "own_reference_ms": args.reference_ms,
                "python": platform.python_version(), "python_chess": chess.__version__,
                "platform": platform.platform(), "logical_cpus": os.cpu_count(),
                "execution": "Fresh hidden process per position, sequential uninterrupted search; full provided history; Hash16; one native search thread; activity enabled",
                "limitations": ["Every supplied previously authored development position is preserved (default bank: 22); this is not a held-out strength sample.",
                                "All endpoints come from one search, not three independent searches; same-path convergence is intentionally favored.",
                                "Baseline is the last eligible packet received by 3000ms, not a separate go movetime 3000 invocation.",
                                "Time cutoffs include UCI request handling; engine Move Overhead remains 20ms.",
                                "More time means more resources, not an equal-budget strength improvement.",
                                "The 12-second own-engine reference can itself be wrong; absolute CP differences measure convergence only.",
                                "No external engine, tablebase, downloaded puzzle, user game or private label is used.",
                                "Shared machine; sampled total-system CPU includes the diagnostic engine and does not establish exclusive CPU or per-process external load."]}
    output.mkdir(parents=True)
    write_json(output / "manifest.json", manifest)
    write_json(output / "positions.json", document)
    logger = logging.getLogger("chess.engine")
    old_level = logger.level
    handler = logging.FileHandler(output / "uci-protocol.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    rows, oracle_rows = [], []
    load = SystemLoadSampler()
    load.start()
    try:
        with (output / "records.jsonl").open("x", encoding="utf-8") as stream:
            for index, (fixture, board) in enumerate(roots):
                row = {"position": fixture}
                try:
                    trace, events = native_query(binary, board, args.reference_ms)
                    row["trace"] = trace
                    points = endpoints(board, events, trace["completed_ms"])
                    row["endpoints"] = points
                    if any(points[key]["result"] is None for key in points):
                        raise ValueError("An endpoint has no validated exact packet")
                    if points["own_reference"]["result"]["root"] != trace["returned_uci"]:
                        raise ValueError("Final exact score root differs from returned move")
                    row.update(status="ok", trace=trace, endpoints=points,
                               comparisons={key: compare_to_reference(points[key], points["own_reference"])
                                            for key in ("baseline_3s", "adaptive")})
                except Exception as error:
                    row.update(status="error", error=f"{type(error).__name__}: {error}")
                rows.append(row)
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                stream.flush()
                print(json.dumps({"completed": index + 1, "total": len(roots), "id": fixture["id"],
                                  "status": row["status"], "stop": row.get("endpoints", {}).get("adaptive", {}).get("stop_reason")}), flush=True)
        sys.path.insert(0, str(ROOT / "tests"))
        from boss_trap_integration import fixtures as mate_fixtures
        for board, fixture in mate_fixtures():
            row = dict(fixture)
            try:
                trace, events = native_query(binary, board, policy.BASE_BUDGET_MS)
                value, _ = at_cutoff(board, events, trace["completed_ms"])
                result = value["result"]
                selected_pass = trace["returned_uci"] in fixture["expected_uci"]
                if fixture["criterion"].startswith("mate_in_"):
                    winner = "white" if board.turn else "black"
                    score_pass = bool(result and result.get("mate") is not None and result.get("winner") == winner)
                else:
                    score_pass = bool(result)
                row.update(trace=trace, evaluation=value, selected_pass=selected_pass,
                           score_pass=score_pass, passed=selected_pass and score_pass)
            except Exception as error:
                row.update(passed=False, error=f"{type(error).__name__}: {error}")
            oracle_rows.append(row)
            write_json(output / "rule-oracles.json", oracle_rows)
            print(json.dumps({"oracle": fixture["id"], "passed": row["passed"]}), flush=True)
    finally:
        load_report = load.finish()
        write_json(output / "system-load.json", load_report)
        logger.removeHandler(handler)
        handler.close()
        logger.setLevel(old_level)
    unchanged = (digest(binary) == manifest["binary_sha256"] and digest(args.positions) == manifest["input_sha256"]
                 and all(digest(ROOT / name) == value for name, value in source_hashes.items()))
    summary = summarize(rows)
    summary.update(inputs_unchanged=unchanged, rule_oracle_cases=len(oracle_rows),
                   rule_oracle_passed=sum(row["passed"] for row in oracle_rows),
                   finished_utc=datetime.now(timezone.utc).isoformat())
    write_json(output / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 0 if unchanged and not summary["failed_ids"] and all(row["passed"] for row in oracle_rows) else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--positions", type=Path, default=ROOT / "tests/data/evaluation_quality_development_v1.json")
    parser.add_argument("--reference-ms", type=int, default=12000)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
