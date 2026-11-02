"""Native evidence for authored poisoned-piece motifs, including a counterexample.

This diagnostic searches only after a specified greedy capture. It does not
claim the offer is sound, selected by game policy, or a novel composition.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time

import chess
import chess.engine

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from poisoned_piece import validate_capture_branch
from runtime_engine import resolve_runtime_engine

CASES = (
    ("knight-pawn-bishop-rook", "k5r1/5p2/8/8/5N2/1B6/8/7K w - - 0 1", True),
    ("bishop-can-be-recaptured", "6rk/5p2/8/8/5N2/1B6/8/7K w - - 0 1", False),
    ("material-recovery-while-behind", "k5r1/b4p2/8/8/5N2/1B6/8/7K w - - 0 1", True),
)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--movetime-ms", type=int, default=1000)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() or args.movetime_ms < 1:
        parser.error("use a fresh output path and positive search time")
    binary = (args.binary or resolve_runtime_engine(ROOT)).resolve(strict=True)
    report = {"schema": "regicide.poisoned-piece.native.v1",
              "started_utc": datetime.now(timezone.utc).isoformat(),
              "binary": str(binary), "binary_sha256": sha(binary),
              "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
              "driver_sha256": sha(__file__), "helper_sha256": sha(ROOT / "scripts/poisoned_piece.py"),
              "python": platform.python_version(), "python_chess": chess.__version__,
              "platform": platform.platform(), "movetime_ms": args.movetime_ms,
              "authorship": "project-authored synthetic legal positions; no external position database",
              "limitations": ["Conditional consequence of the specified greedy capture only.",
                              "Material motifs have immediate payoff; no delayed-combination claim.",
                              "A material gain does not prove a winning game or Elo improvement."],
              "cases": []}
    creation = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    for case_id, fen, expected in CASES:
        before = chess.Board(fen)
        after = before.copy(stack=True)
        after.push_uci("f4g6")
        after.push_uci("f7g6")
        row = {"id": case_id, "before_fen": fen, "offer_uci": "f4g6", "capture_uci": "f7g6",
               "after_capture_fen": after.fen(en_passant="fen"), "expected_verified": expected}
        try:
            if not before.is_valid() or not after.is_valid():
                raise AssertionError("Invalid authored position")
            with chess.engine.SimpleEngine.popen_uci([str(binary), "uci"], timeout=10, **creation) as engine:
                options = {"UsePieceActivity": True} if "UsePieceActivity" in engine.options else {}
                engine.configure(options)
                row["engine_id"], row["configured_options"] = dict(engine.id), options
                row["default_options"] = {name: opt.default for name, opt in engine.options.items()}
                began = time.monotonic()
                reply = engine.play(after, chess.engine.Limit(time=args.movetime_ms / 1000),
                                    game=object(), info=chess.engine.INFO_ALL)
                row["search_wall_ms"] = round((time.monotonic() - began) * 1000, 3)
                row["selected_uci"] = reply.move.uci() if reply.move else None
                row["depth"], row["nodes"] = reply.info.get("depth"), reply.info.get("nodes")
                score = reply.info.get("score")
                row["cp"] = score.pov(chess.WHITE).score() if score else None
                row["mate"] = score.pov(chess.WHITE).mate() if score else None
                row["native_pv_uci"] = [move.uci() for move in reply.info.get("pv", [])]
                began = time.monotonic()
                row["evidence"] = validate_capture_branch(before, "f4g6", "f7g6", reply.info)
                row["proof_wall_ms"] = round((time.monotonic() - began) * 1000, 3)
                row["passed"] = (row["evidence"]["verified"] is expected
                                 and (not expected or row["selected_uci"] == "b3g8"))
        except Exception as exc:
            row.update(passed=False, error=f"{type(exc).__name__}: {exc}")
        report["cases"].append(row)
        print(json.dumps({"id": case_id, "passed": row["passed"],
                          "selected": row.get("selected_uci"), "cp": row.get("cp"),
                          "evidence": row.get("evidence", {}).get("reason")}), flush=True)
    report["passed"] = sum(row["passed"] for row in report["cases"])
    report["failed"] = len(report["cases"]) - report["passed"]
    report["finished_utc"] = datetime.now(timezone.utc).isoformat()
    if sha(binary) != report["binary_sha256"]:
        raise AssertionError("Runtime binary changed during diagnostic")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return 1 if report["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
