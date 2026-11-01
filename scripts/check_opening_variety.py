"""Reproduce opening variety, own-engine score checks and latency separately.

Examples (run with .venv-tests/Scripts/python.exe):
  scripts/check_opening_variety.py --phase old
  scripts/check_opening_variety.py --phase new
  scripts/check_opening_variety.py --phase quality
  scripts/check_opening_variety.py --phase quality --retry-incomplete --quality-seconds 15
  scripts/check_opening_variety.py --phase summary

Only disposable HumanGames and owned hidden engine processes are used. The old
play host and variety policy are copied verbatim from Git into ignored output;
the new pair is snapshotted before it runs. Screen-only evaluation/hint helpers
are disabled equally for both policies to avoid unrelated background load.
No external engine, personal games, GUI or strength/Elo claims are involved.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

import chess
import chess.engine

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from opening_guide import _catalog, position_key

CASES = (
    ("white-first", ""),
    ("black-after-e4", "e2e4"),
    ("black-after-d4", "d2d4"),
    ("black-after-c4", "c2c4"),
    ("black-after-Nf3", "g1f3"),
    ("qgd-canonical", "d2d4 d7d5 c2c4 e7e6"),
    ("qgd-via-english", "c2c4 e7e6 d2d4 d7d5"),
    ("ruy-lopez-after-six-plies", "e2e4 e7e5 g1f3 b8c6 f1b5 a7a6"),
)
SEEDS = tuple(f"{number:032x}" for number in range(16))
TOTAL_MS = 1000
QUALITY_DEPTH = 8
QUALITY_LIMIT_SECONDS = 5


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def read_rows(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def append_row(path, row):
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        stream.flush()


def board_for(history):
    board = chess.Board()
    for uci in history.split():
        board.push_uci(uci)
    return board


def start_engine(binary):
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    return chess.engine.SimpleEngine.popen_uci([str(binary), "uci"], timeout=7, **options)


class QuietScreenHints:
    """No screen side searches; never replaces the opponent engine."""
    def __init__(self, *_args, **_kwargs):
        self.value = {"enabled": False, "status": "disabled", "candidates": []}

    def snapshot(self):
        return dict(self.value)

    def snapshot_with_evaluation(self):
        return dict(self.value), {}

    def set_enabled(self, _enabled):
        return self.snapshot()

    def update_position(self, **_kwargs):
        pass

    def close(self):
        pass


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def snapshot_host(output, label, baseline_ref=None):
    folder = output / (label + "-source")
    folder.mkdir(parents=True, exist_ok=True)
    files = {}
    for filename in ("opening_variety.py", "play_human.py"):
        relative = "scripts/" + filename
        content = (subprocess.check_output(["git", "show", f"{baseline_ref}:{relative}"], cwd=ROOT)
                   if baseline_ref else (ROOT / relative).read_bytes())
        path = folder / filename
        if path.exists() and path.read_bytes() != content:
            raise RuntimeError(f"Source changed for {label}; choose a new --new-label/output to preserve prior evidence")
        path.write_bytes(content)
        files[relative] = hashlib.sha256(content).hexdigest()
    variety = load_module("validation_" + label.replace("-", "_") + "_variety", folder / "opening_variety.py")
    host = load_module("validation_" + label.replace("-", "_") + "_host", folder / "play_human.py")
    # Each loaded class resolves its own globals. Never replace canonical
    # sys.modules['opening_variety'] or the running user's host module.
    host.opening_variety = variety
    host.MoveHints = QuietScreenHints
    host.ROOT = ROOT
    return host, variety, files


def manifest(args, label, variety, source_hashes, cold_catalog_ms):
    catalog = _catalog()
    return {
        "label": label, "policy": variety.POLICY,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "old_ref": args.baseline_ref if label == "old" else None,
        "source_sha256": source_hashes, "binary_sha256": digest(args.binary),
        "model": "classical; no neural model supplied", "python": platform.python_version(),
        "python_chess": chess.__version__, "os": platform.platform(), "logical_cpus": os.cpu_count(),
        "time_budget_ms": TOTAL_MS, "seeds": list(SEEDS),
        "catalog_commit": catalog.source["commit"], "catalog_rows": len(catalog.openings),
        "catalog_cold_load_ms": round(cold_catalog_ms, 3),
        "cases": [{"name": name, "history_uci": history.split(),
                   "fen": board_for(history).fen(en_passant="fen"),
                   "position_key": position_key(board_for(history))} for name, history in CASES],
        "conditions": "One sequential move per fresh disposable HumanGame; same 16 seeds per position. UI-only hints/evaluation disabled for both versions. Catalog preloaded. Each opponent engine is a fresh hidden owned process; no game archive or UI access.",
        "latency_scope": "engine_move wall clock including searches, state construction and private save; excludes engine launch, initial catalog load and fixture construction",
        "limitations": "Sequential old/new runs on a shared computer, without exclusive CPU isolation. Time-limited search depths can vary with system load. No claim of equal-time strength improvement or Elo.",
    }


def run_policy(args, label, baseline_ref=None):
    began = time.perf_counter()
    _catalog()
    cold_catalog_ms = (time.perf_counter() - began) * 1000
    host, variety, hashes = snapshot_host(args.output, label, baseline_ref)
    manifest_path = args.output / (label + "-manifest.json")
    record_path = args.output / (label + "-records.jsonl")
    metadata = manifest(args, label, variety, hashes, cold_catalog_ms)
    if manifest_path.exists():
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        for key in ("source_sha256", "binary_sha256", "model", "time_budget_ms", "seeds", "cases"):
            if previous[key] != metadata[key]:
                raise RuntimeError("Cannot append incompatible evidence: " + key)
    else:
        write_json(manifest_path, metadata)
    existing = read_rows(record_path)
    done = {(row["case"], row["seed"]) for row in existing}
    progress = time.monotonic()
    total = len(CASES) * len(SEEDS)
    print(json.dumps({"phase": label, "status": "started", "already_complete": len(done), "total": total}), flush=True)
    with tempfile.TemporaryDirectory(prefix=label + "-disposable-games-", dir=args.output) as temporary:
        for name, history in CASES:
            for seed in SEEDS:
                if (name, seed) in done:
                    continue
                before = board_for(history)
                game = host.HumanGame(args.binary, temporary, engine_factory=start_engine)
                try:
                    game.new_game("black" if before.turn == chess.WHITE else "white", TOTAL_MS, game.revision)
                    with game._lock:
                        game.board = before.copy(stack=True)
                        game._opening_seed = seed
                        game.revision += 1
                        game._sync_evaluation()
                    proposal = variety.plan(before.copy(stack=True), TOTAL_MS, seed)
                    started = time.perf_counter()
                    state = game.engine_move(game.revision)
                    elapsed = (time.perf_counter() - started) * 1000
                    if state.get("error"):
                        raise RuntimeError(state["error"])
                    actual = game.board.peek()
                    if actual not in before.legal_moves or len(game.board.move_stack) != len(before.move_stack) + 1:
                        raise AssertionError("Host did not apply exactly one legal move")
                    decision = game._opening_decisions.get(len(game.board.move_stack))
                    if decision and decision.get("selected_uci") != actual.uci():
                        raise AssertionError("Receipt and applied move disagree")
                    row = {"label": label, "case": name, "seed": seed, "history_uci": history.split(),
                           "before_fen": before.fen(en_passant="fen"), "selected_uci": actual.uci(),
                           "selected_san": before.san(actual), "wall_ms": round(elapsed, 3),
                           "plan": proposal, "decision": decision,
                           "reason": decision.get("reason") if decision else proposal["reason"]}
                    append_row(record_path, row)
                    done.add((name, seed))
                finally:
                    game.close()
                if time.monotonic() - progress >= 25 or seed == SEEDS[-1]:
                    print(json.dumps({"phase": label, "complete": len(done), "total": total,
                                      "case": name, "last_move": row["selected_san"],
                                      "last_reason": row["reason"], "last_wall_ms": row["wall_ms"]}), flush=True)
                    progress = time.monotonic()
    summarize(args)


def quality_packet(board, info, required=None):
    if (info.get("depth") != QUALITY_DEPTH or info.get("lowerbound") or info.get("upperbound")
            or not isinstance(info.get("score"), chess.engine.PovScore)):
        return None
    pv = info.get("pv")
    if not isinstance(pv, (list, tuple)) or not pv or len(pv) > 128 or (required and pv[0] != required):
        return None
    replay = board.copy(stack=True)
    line, san = [], []
    for move in pv:
        if not isinstance(move, chess.Move) or move not in replay.legal_moves:
            return None
        san.append(replay.san(move)); line.append(move.uci()); replay.push(move)
    score = info["score"].pov(board.turn)
    return {"depth": QUALITY_DEPTH, "cp_mover": score.score(), "mate_mover": score.mate(),
            "cp_white": info["score"].pov(chess.WHITE).score(), "pv_uci": line, "pv_san": san}


def run_quality(args):
    rows = read_rows(args.output / "old-records.jsonl") + read_rows(args.output / (args.new_label + "-records.jsonl"))
    if len(rows) != 2 * len(CASES) * len(SEEDS):
        raise RuntimeError("Quality audit requires complete old and new samples")
    output = args.output / "quality-records.jsonl"
    binary_hash = digest(args.binary)
    existing = read_rows(output)
    latest = {(row["case"], row["requested_move"]): row for row in existing}
    retries = {key for key, row in latest.items() if row["status"] != "complete"}
    done = {key for key, row in latest.items() if row["status"] == "complete" or not args.retry_incomplete}
    attempts = Counter((row["case"], row["requested_move"]) for row in existing)
    for row in existing:
        if row["binary_sha256"] != binary_hash or row["requested_depth"] != QUALITY_DEPTH:
            raise RuntimeError("Quality cache does not match this engine/depth")
    progress = time.monotonic()
    for name, history in CASES:
        board = board_for(history)
        moves = [None] + sorted({row["selected_uci"] for row in rows if row["case"] == name})
        moves = [uci for uci in moves if (name, uci) not in done
                 and (not args.retry_incomplete or (name, uci) in retries)]
        if not moves:
            continue
        engine = start_engine(args.binary)
        try:
            for uci in moves:
                key = (name, uci)
                options = {"game": object(), "info": chess.engine.INFO_ALL}
                move = chess.Move.from_uci(uci) if uci else None
                if move:
                    options["root_moves"] = [move]
                exact, last_depth, last_raw, error = None, None, None, None
                started = time.perf_counter()
                try:
                    with engine.analysis(board.copy(stack=True), chess.engine.Limit(depth=QUALITY_DEPTH, time=args.quality_seconds), **options) as analysis:
                        for info in analysis:
                            if isinstance(info.get("depth"), int):
                                last_depth = info["depth"]
                            if "score" in info:
                                value = info["score"].pov(board.turn)
                                last_raw = {"depth": info.get("depth"), "cp_mover": value.score(),
                                            "mate_mover": value.mate(), "lowerbound": bool(info.get("lowerbound")),
                                            "upperbound": bool(info.get("upperbound"))}
                            packet = quality_packet(board, info, move)
                            if packet is not None:
                                exact = packet
                        reply = analysis.wait()
                        if exact is not None and (reply.move is None or reply.move.uci() != exact["pv_uci"][0]):
                            exact = None
                except Exception as exc:
                    error = type(exc).__name__ + ": " + str(exc)
                record = {"case": name, "requested_move": uci, "history_uci": history.split(),
                          "before_fen": board.fen(en_passant="fen"), "binary_sha256": binary_hash,
                          "requested_depth": QUALITY_DEPTH, "max_seconds": args.quality_seconds,
                          "attempt": attempts[key] + 1,
                          "retry_of_incomplete": key in retries,
                          "status": "complete" if exact else "incomplete", "exact": exact,
                          "last_depth": last_depth, "last_raw": last_raw, "error": error,
                          "wall_ms": round(1000 * (time.perf_counter() - started), 3)}
                append_row(output, record); done.add(key)
                if time.monotonic() - progress >= 25:
                    print(json.dumps({"phase": "quality", "completed_probes": len(done), "case": name}), flush=True)
                    progress = time.monotonic()
        finally:
            engine.close()
            try:
                engine.returncode.result(timeout=2)
            except Exception:
                pass
        print(json.dumps({"phase": "quality", "case_complete": name, "completed_probes": len(done)}), flush=True)
    summarize(args)


def distribution(values):
    if not values:
        return None
    ordered = sorted(values)
    return {"count": len(values), "mean": statistics.mean(values), "p50": statistics.median(values),
            "p95": ordered[max(0, math.ceil(.95 * len(ordered)) - 1)], "max": max(values)}


def summarize(args):
    quality_attempts = read_rows(args.output / "quality-records.jsonl")
    quality = {(row["case"], row["requested_move"]): row for row in quality_attempts}
    summary = {"budget_ms": TOTAL_MS, "seeds": list(SEEDS), "quality_depth": QUALITY_DEPTH,
               "limits": "Diversity frequencies over 16 seeds per fixed position. Independent scores are Regicide's own estimates at fixed depth, not strength/Elo evidence. Differences are signed unrestricted-reference minus restricted-move scores; negative values can occur because selective searches differ. Old total budget and new full-baseline-plus-extra budget differ; latency is reported explicitly.",
               "policies": {}}
    for label in ("old", args.new_label):
        rows = read_rows(args.output / (label + "-records.jsonl"))
        if not rows:
            continue
        policy = {"records": len(rows), "wall_ms": distribution([row["wall_ms"] for row in rows]), "cases": {}}
        for name, _history in CASES:
            selected = [row for row in rows if row["case"] == name]
            if not selected:
                continue
            frequency = Counter(row["selected_san"] for row in selected)
            decisions = [row["decision"] for row in selected if row["decision"]]
            probes = [probe for receipt in decisions for probe in receipt.get("probes", [])]
            baseline = quality.get((name, None), {}).get("exact")
            score_diffs, excluded = [], 0
            quality_by_move = {}
            for row in selected:
                scored = quality.get((name, row["selected_uci"]), {}).get("exact")
                if baseline and scored and baseline["cp_mover"] is not None and scored["cp_mover"] is not None:
                    score_diffs.append(baseline["cp_mover"] - scored["cp_mover"])
                else:
                    excluded += 1
                if row["selected_uci"] not in quality_by_move:
                    quality_by_move[row["selected_uci"]] = {
                        "san": row["selected_san"], "count": frequency[row["selected_san"]],
                        "reference": baseline, "selected": scored,
                        "difference_cp": (baseline["cp_mover"] - scored["cp_mover"]
                                          if baseline and scored and baseline["cp_mover"] is not None
                                          and scored["cp_mover"] is not None else None),
                    }
            policy["cases"][name] = {
                "n": len(selected), "frequency": dict(sorted(frequency.items())), "unique": len(frequency),
                "top_share": max(frequency.values()) / len(selected),
                "reasons": dict(Counter(row["reason"] for row in selected)),
                "probe_reasons": dict(Counter(probe["reason"] for probe in probes)),
                "qualified_pool_sizes": (dict(Counter(str(len(receipt["selection_pool"])) for receipt in decisions))
                                         if decisions and all("selection_pool" in receipt for receipt in decisions) else None),
                "baseline_depths": dict(Counter(str((receipt.get("baseline") or {}).get("depth")) for receipt in decisions)),
                "wall_ms": distribution([row["wall_ms"] for row in selected]),
                "independent_score_difference_cp": distribution(score_diffs),
                "quality_cp_above_25_count": sum(value > 25 for value in score_diffs),
                "quality_cp_above_100_count": sum(value > 100 for value in score_diffs),
                "quality_excluded_samples": excluded,
                "quality_by_move": quality_by_move,
            }
        differences = [move["difference_cp"] for case in policy["cases"].values()
                       for move in case["quality_by_move"].values()
                       if move["difference_cp"] is not None for _ in range(move["count"])]
        policy["distinct_position_move_pairs"] = sum(case["unique"] for case in policy["cases"].values())
        policy["independent_score_difference_cp"] = distribution(differences)
        policy["quality_excluded_samples"] = sum(case["quality_excluded_samples"] for case in policy["cases"].values())
        policy["quality_cp_above_25_count"] = sum(value > 25 for value in differences)
        policy["quality_cp_above_100_count"] = sum(value > 100 for value in differences)
        policy["quality_negative_difference_samples"] = sum(value < 0 for value in differences)
        summary["policies"][label] = policy
    if quality:
        summary["quality_probes"] = {"count": len(quality),
                                     "attempts": len(quality_attempts),
                                     "incomplete_attempts": sum(row["status"] != "complete" for row in quality_attempts),
                                     "caps_seconds_used": sorted({row["max_seconds"] for row in quality_attempts}),
                                     "complete": sum(row["status"] == "complete" for row in quality.values()),
                                     "incomplete": sum(row["status"] != "complete" for row in quality.values()),
                                     "mate_scored": sum(row.get("exact") is not None and row["exact"]["mate_mover"] is not None for row in quality.values())}
    write_json(args.output / (args.new_label + "-comparison.json"), summary)
    print(json.dumps({"phase": "summary", "records": {key: value["records"] for key, value in summary["policies"].items()},
                      "summary": str(args.output / (args.new_label + "-comparison.json"))}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("old", "new", "quality", "summary"), required=True)
    parser.add_argument("--baseline-ref", default="e82f149")
    parser.add_argument("--quality-seconds", type=float, default=QUALITY_LIMIT_SECONDS)
    parser.add_argument("--retry-incomplete", action="store_true",
                        help="Retry only previously incomplete quality keys; retain all attempts")
    parser.add_argument("--new-label", default="new")
    parser.add_argument("--binary", type=Path, default=ROOT / "build/regicide.exe")
    parser.add_argument("--output", type=Path, default=ROOT / "build/opening-variety-v2-validation")
    args = parser.parse_args()
    if not math.isfinite(args.quality_seconds) or not 0 < args.quality_seconds <= 120:
        parser.error("--quality-seconds must be finite and between 0 and 120")
    if not args.new_label.replace("-", "").replace("_", "").isalnum() or args.new_label == "old":
        parser.error("--new-label must be a short alphanumeric label distinct from old")
    args.binary = args.binary.resolve(strict=True)
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    if args.phase == "old":
        run_policy(args, "old", args.baseline_ref)
    elif args.phase == "new":
        run_policy(args, args.new_label)
    elif args.phase == "quality":
        run_quality(args)
    else:
        summarize(args)


if __name__ == "__main__":
    main()
