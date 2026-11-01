"""Bounded, offline evaluation diagnostics against a pinned Stockfish reference.

Use public authored development roots, not personal games. Raw centipawns from
different engines are never subtracted as an accuracy/Elo measure. Changed
choices are compared using two constrained searches by the same reference.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import threading
import time

import chess
import chess.engine

from engine_profile import load_profile


ROOT = Path(__file__).resolve().parents[1]
SOURCE_FILES = ("src/evaluation.cpp", "include/regicide/evaluation.hpp", "src/search.cpp",
                "src/main.cpp", "src/uci.cpp", "include/regicide/search.hpp", "scripts/classical_evaluation.py")


def comparison_pairs(refinement_ms):
    pairs = {"core_short": ("baseline", "candidate")}
    if refinement_ms:
        pairs.update(core_refined=("baseline_refined", "candidate_refined"),
                     baseline_refinement=("baseline", "baseline_refined"),
                     candidate_refinement=("candidate", "candidate_refined"))
    return pairs


def validate_budgets(time_ms, reference_ms, refinement_ms):
    if not 100 <= time_ms <= 5000 or not 100 <= reference_ms <= 5000 or not 0 <= refinement_ms <= 5000:
        raise ValueError("Use 100..5000ms queries and optional 0..5000ms refinement")
    if refinement_ms and refinement_ms <= time_ms:
        raise ValueError("Refinement must be disabled (0) or strictly longer than the base search")


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_positions(path):
    document = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(document, dict) or not isinstance(document.get("positions"), list):
        raise ValueError("Expected an object containing positions")
    if not 1 <= len(document["positions"]) <= 64:
        raise ValueError("Use a bounded 1..64 position suite")
    seen, rows = set(), []
    for row in document["positions"]:
        key = row.get("id")
        if not isinstance(key, str) or not key or key in seen:
            raise ValueError("Every position needs a unique nonempty id")
        seen.add(key)
        moves = row.get("history_uci")
        if not isinstance(moves, list) or len(moves) > 512:
            raise ValueError("Each position needs bounded explicit history_uci")
        board = chess.Board(row["initial_fen"])
        if not board.is_valid():
            raise ValueError("Invalid initial board: " + key)
        for uci in moves:
            move = chess.Move.from_uci(uci)
            if move not in board.legal_moves:
                raise ValueError("Illegal source history: " + key)
            board.push(move)
        if board.fen(en_passant="fen") != chess.Board(row["fen"]).fen(en_passant="fen"):
            raise ValueError("Source final FEN does not match full history: " + key)
        if board.is_game_over(claim_draw=True):
            raise ValueError("Terminal roots need a separate outcome oracle: " + key)
        rows.append((dict(row), board))
    return document, rows


def exact_packet(board, info, required=None):
    """Require score/depth/PV in one exact packet; never merge partial updates."""
    if type(info.get("depth")) is not int or info["depth"] < 1:
        return None
    score = info.get("score")
    if not isinstance(score, chess.engine.PovScore) or score.turn != board.turn:
        return None
    if info.get("lowerbound") or info.get("upperbound") or info.get("multipv", 1) != 1:
        return None
    pv = info.get("pv")
    if not isinstance(pv, (list, tuple)) or not 1 <= len(pv) <= 256:
        return None
    if required is not None and pv[0] != required:
        return None
    replay, san = board.copy(stack=True), []
    for move in pv:
        if not isinstance(move, chess.Move) or move not in replay.legal_moves:
            return None
        san.append(replay.san(move))
        replay.push(move)
    return {"depth": info["depth"], "nodes": info.get("nodes"),
            "cp_white": score.white().score(), "mate_white": score.white().mate(),
            "cp_mover": score.pov(board.turn).score(), "mate_mover": score.pov(board.turn).mate(),
            "bound": "exact", "best_uci": pv[0].uci(),
            "pv_uci": [move.uci() for move in pv], "pv_san": san,
            "engine_time_ms": round(info.get("time", 0) * 1000, 3)}


def packet_diagnostic(info):
    score = info.get("score")
    valid = isinstance(score, chess.engine.PovScore)
    return {"depth": info.get("depth"), "nodes": info.get("nodes"),
            "cp_white": score.white().score() if valid else None,
            "mate_white": score.white().mate() if valid else None,
            "score_turn": ("white" if score.turn else "black") if valid else None,
            "lowerbound": bool(info.get("lowerbound")), "upperbound": bool(info.get("upperbound")),
            "pv_uci": [move.uci() for move in info.get("pv", [])]}


def query(engine, board, milliseconds, root_uci=None):
    required = chess.Move.from_uci(root_uci) if root_uci else None
    if required is not None and required not in board.legal_moves:
        raise ValueError("Illegal constrained root move")
    engine.configure({"Clear Hash": None})
    started, expired = time.monotonic(), threading.Event()
    def timeout():
        expired.set()
        engine.close()
    guard = threading.Timer(milliseconds / 1000 + 5, timeout)
    guard.daemon = True
    guard.start()
    packets, chosen, completed = [], None, 0
    try:
        kwargs = {"game": object(), "info": chess.engine.INFO_ALL}
        if required is not None:
            kwargs["root_moves"] = [required]
        with engine.analysis(board.copy(stack=True), chess.engine.Limit(time=milliseconds / 1000), **kwargs) as analysis:
            for info in analysis:
                if "score" in info:
                    packets.append(packet_diagnostic(info))
                    parsed = exact_packet(board, info, required)
                    if parsed is not None:
                        chosen = parsed
                        completed += 1
            result = analysis.wait()
        best = result.move.uci() if result.move is not None else None
        status = "ok" if chosen is not None and chosen["best_uci"] == best else "no_exact_matching_final_packet"
        if expired.is_set():
            raise TimeoutError("Owned engine exceeded bounded query")
        return {"status": status, "requested_ms": milliseconds,
                "wall_ms": round((time.monotonic() - started) * 1000, 3),
                "root_uci": root_uci, "returned_best_uci": best, "result": chosen,
                "completed_exact_packets": completed, "score_packets": packets}
    finally:
        guard.cancel()
        guard.join(timeout=.2)


def advantage(result, threshold=0):
    if result is None:
        return "unavailable"
    mate = result.get("mate_white")
    if mate is not None:
        if mate == 0:
            return "mate_zero"  # Do not invent a winner from a signed zero.
        return "white" if mate > 0 else "black"
    cp = result.get("cp_white")
    if type(cp) is not int:
        return "unavailable"
    if abs(cp) < threshold or cp == 0:
        return "neutral"
    return "white" if cp > 0 else "black"


def same_reference_choice_delta(board, old_query, new_query):
    """Positive means the reference prefers new; no cross-engine score scale."""
    if old_query["status"] != "ok" or new_query["status"] != "ok":
        return {"status": "unavailable"}
    old, new = old_query["result"], new_query["result"]
    if old["mate_white"] is not None or new["mate_white"] is not None:
        return {"status": "mate_separate", "old_mate_white": old["mate_white"],
                "new_mate_white": new["mate_white"]}
    sign = 1 if board.turn else -1
    return {"status": "finite", "new_minus_old_cp_mover": sign * (new["cp_white"] - old["cp_white"]),
            "old_depth": old["depth"], "new_depth": new["depth"]}


def summarize(records, confirmations, refinement_ms):
    result = {"positions": len(records), "reference_threshold_cp": 100,
              "score_units": "No cross-engine CP error or Elo computed", "engines": {}}
    labels = ["baseline", "candidate"] + (["baseline_refined", "candidate_refined"] if refinement_ms else [])
    for label in labels:
        categories, matches, mate_pairs, complete = Counter(), 0, Counter(), 0
        for row in records:
            own_query, ref_query = row[label], row["reference"]
            if own_query["status"] != "ok" or ref_query["status"] != "ok":
                categories["incomplete"] += 1
                continue
            own, ref = own_query["result"], ref_query["result"]
            complete += 1
            matches += own["best_uci"] == ref["best_uci"]
            expected, observed = advantage(ref, 100), advantage(own)
            if expected in ("white", "black"):
                categories["same_direction" if observed == expected else "neutral" if observed == "neutral" else "opposed_or_unavailable"] += 1
            else:
                categories["reference_not_clear"] += 1
            mate_pairs[f'{ref["mate_white"] is not None}/{own["mate_white"] is not None}'] += 1
        result["engines"][label] = {"complete": complete, "same_reference_first_move": matches,
            "direction_categories": dict(categories), "mate_visibility_reference_own": dict(mate_pairs),
            "mean_wall_ms": statistics.mean(row[label]["wall_ms"] for row in records),
            "mean_completed_depth": statistics.mean(row[label]["result"]["depth"] for row in records if row[label]["status"] == "ok") if complete else None,
            "resource_comparison": "Longer search; compare core at the same budget and time refinement separately" if label.endswith("_refined") else "equal requested baseline/candidate time"}
    result["changed_choice_confirmations"] = {}
    for name, (old_label, new_label) in comparison_pairs(refinement_ms).items():
        compared = [item["comparisons"][name] for item in confirmations if name in item["comparisons"]]
        finite = [item["delta"]["new_minus_old_cp_mover"] for item in compared if item["delta"]["status"] == "finite"]
        result["changed_choice_confirmations"][name] = {"old": old_label, "new": new_label,
            "changed_positions": len(compared), "finite": len(finite),
            "reference_prefers_new_over_20cp": sum(v > 20 for v in finite),
            "reference_prefers_old_over_20cp": sum(v < -20 for v in finite),
            "within_20cp": sum(abs(v) <= 20 for v in finite),
            "mean_new_minus_old_cp_mover": statistics.mean(finite) if finite else None,
            "worst_new_minus_old_cp_mover": min(finite) if finite else None,
            "resource_comparison": "equal requested time" if name.startswith("core_") else "unequal: longer search budget",
            "limits": "Separate finite same-reference restricted searches, potentially different completed depths; not proven regret or playing strength."}
    result["constrained_reference_queries"] = sum(len(item["evaluations"]) for item in confirmations)
    return result


def changed_pairs(row, refinement_ms):
    pairs = {}
    for name, (old_label, new_label) in comparison_pairs(refinement_ms).items():
        if row[old_label]["status"] != "ok" or row[new_label]["status"] != "ok":
            continue
        old, new = row[old_label]["result"]["best_uci"], row[new_label]["result"]["best_uci"]
        if old != new:
            pairs[name] = (old, new)
    return pairs


def run(args):
    validate_budgets(args.time_ms, args.reference_ms, args.refinement_ms)
    out = args.output.resolve()
    if out.exists():
        raise ValueError("Use a fresh output directory")
    document, roots = load_positions(args.positions.resolve(strict=True))
    reference = load_profile(args.reference_profile)
    profile = json.loads(args.classical_profile.read_text(encoding="utf-8-sig"))
    common = dict(profile["options"])
    common.update(Hash=16, UseNeuralEvaluation=False, UseNNUEEvaluation=False)
    common.pop("UsePieceActivity", None)
    binaries = {"baseline": args.baseline.resolve(strict=True), "candidate": args.candidate.resolve(strict=True),
                "reference": Path(reference["binary"]["path"])}
    hashes = {key: digest(path) for key, path in binaries.items()}
    source_hashes = {name: digest(ROOT / name) for name in SOURCE_FILES if (ROOT / name).exists()}
    sources = {"positions": digest(args.positions), "driver": digest(__file__),
               "classical_profile": digest(args.classical_profile), "reference_profile": digest(args.reference_profile),
               "current_source": source_hashes}
    out.mkdir(parents=True)
    write_json(out / "positions.json", document)
    manifest = {"created_at_utc": datetime.now(timezone.utc).isoformat(), "python": platform.python_version(),
        "python_chess": chess.__version__, "platform": platform.platform(), "processor": platform.processor(), "logical_cpus": os.cpu_count(),
        "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "source_hashes": sources, "binaries": {key: {"path": str(path), "sha256": hashes[key]} for key, path in binaries.items()},
        "reference_profile": reference, "queries": {"baseline_ms": args.time_ms, "candidate_ms": args.time_ms,
            "reference_ms": args.reference_ms, "baseline_refined_ms": args.refinement_ms, "candidate_refined_ms": args.refinement_ms,
            "confirmation_ms": args.reference_ms, "hash_mb": 16, "threads": 1,
            "order": "Rotate all three or five query variants per root; all sequential; confirmations after roots, one query per unique changed move",
            "hash_history": "Clear hash and distinct game token per query; preserve full source history"},
        "limitations": ["Reused authored development roots, not independent holdout or representative chess strength.",
                        "Shared computer, no exclusive CPU isolation or sampled external-load claim.",
                        "Centipawns from different engines are not one calibrated scale.",
                        "The longer refinement is explicitly unequal-resource and is not a playing-strength test.",
                        "Private user games are excluded; the corpus is frozen before measurements.",
                        "A finite reference search and constrained comparison are not proof of best move or exact regret."]}
    records, confirmations, engine_metadata = [], [], {}
    with ExitStack() as stack:
        engines = {}
        for label in ("baseline", "candidate", "reference"):
            argv = [str(binaries[label])] + (reference["args"] if label == "reference" else ["uci"])
            extra = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
            engine = stack.enter_context(chess.engine.SimpleEngine.popen_uci(argv, timeout=20, **extra))
            options = dict(reference["options"] if label == "reference" else common)
            if label == "candidate":
                if "UsePieceActivity" not in engine.options:
                    raise ValueError("Candidate does not advertise UsePieceActivity")
                options["UsePieceActivity"] = True
            elif label == "baseline" and "UsePieceActivity" in engine.options:
                options["UsePieceActivity"] = False
            options["Hash"] = 16
            if "Threads" in engine.options:
                options["Threads"] = 1
            if "Move Overhead" in engine.options:
                options["Move Overhead"] = 20
            engine.configure(options)
            engine_metadata[label] = {"observed_id": dict(engine.id), "options": options}
            engines[label] = engine
        manifest["engines"] = engine_metadata
        write_json(out / "manifest.json", manifest)
        with (out / "records.jsonl").open("x", encoding="utf-8") as stream:
            for index, (source, board) in enumerate(roots):
                row = {"position": source}
                labels = ["baseline", "candidate", "reference"]
                if args.refinement_ms:
                    labels += ["baseline_refined", "candidate_refined"]
                offset = index % len(labels)
                labels = labels[offset:] + labels[:offset]
                for label in labels:
                    budget = args.refinement_ms if label.endswith("_refined") else args.reference_ms if label == "reference" else args.time_ms
                    row[label] = query(engines[label.removesuffix("_refined")], board, budget)
                records.append(row)
                stream.write(json.dumps(row, ensure_ascii=False) + "\n"); stream.flush()
                print(json.dumps({"completed": index + 1, "total": len(roots), "position": source["id"]}), flush=True)
        with (out / "confirmations.jsonl").open("x", encoding="utf-8") as stream:
            for row, (_source, board) in zip(records, roots):
                pairs = changed_pairs(row, args.refinement_ms)
                if not pairs:
                    continue
                moves = sorted({move for pair in pairs.values() for move in pair})
                values = {move: query(engines["reference"], board, args.reference_ms, move) for move in moves}
                comparisons = {name: {"old_uci": old, "new_uci": new,
                    "delta": same_reference_choice_delta(board, values[old], values[new])}
                    for name, (old, new) in pairs.items()}
                item = {"id": row["position"]["id"], "evaluations": values, "comparisons": comparisons}
                confirmations.append(item)
                stream.write(json.dumps(item, ensure_ascii=False) + "\n"); stream.flush()
                print(json.dumps({"confirmed_position": row["position"]["id"], "unique_moves": len(moves)}), flush=True)
    unchanged = all(digest(path) == hashes[label] for label, path in binaries.items())
    unchanged = unchanged and digest(args.positions) == sources["positions"]
    unchanged = unchanged and digest(__file__) == sources["driver"] and digest(args.classical_profile) == sources["classical_profile"]
    unchanged = unchanged and digest(args.reference_profile) == sources["reference_profile"]
    unchanged = unchanged and all(digest(ROOT / name) == value for name, value in source_hashes.items())
    if not unchanged:
        raise ValueError("Source, position bank or binary changed during measurement")
    summary = summarize(records, confirmations, args.refinement_ms)
    summary["inputs_unchanged"] = True
    write_json(out / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--positions", type=Path, default=ROOT / "tests/data/evaluation_quality_development_v1.json")
    parser.add_argument("--baseline", type=Path, default=ROOT / "build/evaluation-quality-v1/baseline/regicide.exe")
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--classical-profile", type=Path, default=ROOT / "config/engine-profiles/classical.json")
    parser.add_argument("--reference-profile", type=Path, default=ROOT / "docs/checks/2026-10-21-m2-foundation/opponents/stockfish18-profile.json")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--time-ms", type=int, default=1000)
    parser.add_argument("--reference-ms", type=int, default=2000)
    parser.add_argument("--refinement-ms", type=int, default=3000)
    args = parser.parse_args()
    try:
        validate_budgets(args.time_ms, args.reference_ms, args.refinement_ms)
    except ValueError as error:
        parser.error(str(error))
    run(args)


if __name__ == "__main__":
    main()
