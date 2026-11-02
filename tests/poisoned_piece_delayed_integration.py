"""Two fixed delayed-tactic diagnostics: keep the failed natural trap as well.

The synthetic smothered-mate fixture verifies a 3-4 move punishment horizon;
its bishop bait does not create the pre-existing mating net. It is not evidence
of novel artistic-combination generation or of a trap selected in a live game.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import chess
import chess.engine

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from poisoned_piece import validate_capture_branch
from runtime_engine import resolve_runtime_engine


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def fixtures():
    albin = chess.Board()
    for san in "d4 d5 c4 e5 dxe5 d4 e3 Bb4+ Bd2".split():
        albin.push_san(san)
    return [
        ("albin-lasker-short-horizon", albin, "d4e3", "d2b4",
         "Project-authored legal continuation from the pinned Albin opening; no assumed later human errors."),
        ("synthetic-bishop-bait-smothered-mate", chess.Board("5r1k/6pp/4Q3/1p4N1/8/1B6/8/K7 w - - 0 1"),
         "b3c4", "b5c4",
         "Project-authored synthetic position using the familiar smothered-mate motif; mating net already existed."),
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--binary", type=Path)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("use a fresh output path; unsuccessful cases must remain recorded")
    binary = (args.binary or resolve_runtime_engine(ROOT)).resolve(strict=True)
    report = {"schema": "regicide.poisoned-piece.delayed-diagnostic.v1",
              "started_utc": datetime.now(timezone.utc).isoformat(),
              "binary": str(binary), "binary_sha256": sha(binary),
              "driver_sha256": sha(__file__), "helper_sha256": sha(ROOT / "scripts/poisoned_piece.py"),
              "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
              "movetime_ms": 1000, "proof_max_nodes": 4000, "proof_default_seconds": .15,
              "proof_max_plies": 8,
              "limitations": ["These are two development fixtures, not a strength benchmark.",
                              "The natural Albin case remains recorded even if the short-horizon proof fails.",
                              "The synthetic fixture tests delayed conversion, not causal bait creation or original artistic discovery.",
                              "Minimum/maximum move counts are for the proved strategy, not globally shortest mates."],
              "cases": []}
    creation = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    for case_id, before, offer, capture, authorship in fixtures():
        after = before.copy(stack=True)
        after.push_uci(offer)
        after.push_uci(capture)
        row = {"id": case_id, "authorship": authorship,
               "before_fen": before.fen(en_passant="fen"),
               "history_uci": [move.uci() for move in before.move_stack],
               "offer_uci": offer, "capture_uci": capture,
               "after_capture_fen": after.fen(en_passant="fen")}
        try:
            if not before.is_valid() or not after.is_valid():
                raise AssertionError("Invalid authored fixture")
            with chess.engine.SimpleEngine.popen_uci([str(binary), "uci"], timeout=10, **creation) as engine:
                options = {"UsePieceActivity": True} if "UsePieceActivity" in engine.options else {}
                engine.configure(options)
                row["engine_id"], row["configured_options"] = dict(engine.id), options
                row["default_options"] = {name: opt.default for name, opt in engine.options.items()}
                began = time.monotonic()
                reply = engine.play(after, chess.engine.Limit(time=1), game=object(), info=chess.engine.INFO_ALL)
                row["search_wall_ms"] = round((time.monotonic() - began) * 1000, 3)
                row["selected_uci"] = reply.move.uci() if reply.move else None
                row["depth"], row["nodes"] = reply.info.get("depth"), reply.info.get("nodes")
                score = reply.info.get("score")
                row["cp"] = score.pov(after.turn).score() if score else None
                row["mate"] = score.pov(after.turn).mate() if score else None
                row["native_pv_uci"] = [move.uci() for move in reply.info.get("pv", [])]
                began = time.monotonic()
                evidence = validate_capture_branch(before, offer, capture, reply.info)
                row["proof_wall_ms"] = round((time.monotonic() - began) * 1000, 3)
                row["evidence"] = evidence
                row["delayed_verified"] = bool(evidence.get("verified") and evidence.get("minimum_payoff_moves", 0) >= 3)
        except Exception as exc:
            row.update(delayed_verified=False, error=f"{type(exc).__name__}: {exc}")
        report["cases"].append(row)
        print(json.dumps({"id": case_id, "delayed_verified": row["delayed_verified"],
                          "reason": row.get("evidence", {}).get("reason"),
                          "minimum_moves": row.get("evidence", {}).get("minimum_payoff_moves"),
                          "maximum_moves": row.get("evidence", {}).get("maximum_payoff_moves")}), flush=True)
    report["delayed_verified"] = sum(row["delayed_verified"] for row in report["cases"])
    report["not_verified"] = len(report["cases"]) - report["delayed_verified"]
    report["finished_utc"] = datetime.now(timezone.utc).isoformat()
    if sha(binary) != report["binary_sha256"]:
        raise AssertionError("Runtime binary changed during diagnostic")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    # This is a diagnostic that records limitations, not an all-pass test.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
