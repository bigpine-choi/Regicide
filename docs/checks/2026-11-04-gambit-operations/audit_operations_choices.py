"""Independent same-root audit of actual new operation-policy substitutions.

Run only after play has completed. No answer is sent to either player. Three
fresh queries compare free Stockfish, the saved native baseline, and the saved
actual move at one shared completed exact depth.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import subprocess
import time

import chess
import chess.engine


def safe(value):
    if isinstance(value, chess.Move):
        return value.uci()
    if isinstance(value, chess.engine.PovScore):
        return {"turn": "white" if value.turn else "black",
                "cp_white": value.white().score(), "mate_white": value.white().mate()}
    if isinstance(value, chess.engine.PovWdl):
        return {"turn": "white" if value.turn else "black", "white": list(value.white())}
    if isinstance(value, dict):
        return {str(k): safe(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [safe(v) for v in value]
    return value if isinstance(value, (str, int, float, bool)) or value is None else repr(value)


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                    encoding="utf-8", newline="\n")


def query(engine, board, move, seconds, depth):
    if "Clear Hash" in engine.options:
        engine.configure({"Clear Hash": None})
    packets = {}
    started = time.monotonic()
    with engine.analysis(board, chess.engine.Limit(time=seconds, depth=depth),
                         root_moves=[move] if move else None, game=object(), info=chess.engine.INFO_ALL) as stream:
        for packet in stream:
            score, level = packet.get("score"), packet.get("depth")
            if (isinstance(score, chess.engine.PovScore) and type(level) is int
                    and not packet.get("lowerbound") and not packet.get("upperbound")):
                packets[level] = packet
        answer = stream.wait()
    return {"root_move": move.uci() if move else None, "bestmove": safe(answer.move),
            "elapsed_ms": round((time.monotonic() - started) * 1000, 3), "packets": packets}


def audit(engine, row, seconds, depth):
    board = chess.Board()
    for uci in row["history_uci"]:
        board.push_uci(uci)
    if board.fen(en_passant="fen") != row["before_fen"]:
        raise ValueError("Saved root disagrees with legal history")
    baseline, selected = chess.Move.from_uci(row["baseline_uci"]), chess.Move.from_uci(row["selected_uci"])
    if baseline not in board.legal_moves or selected not in board.legal_moves or baseline == selected:
        raise ValueError("Substitution is not two distinct legal root moves")
    queries = [query(engine, board, move, seconds, depth) for move in (None, baseline, selected)]
    common = sorted(set(queries[0]["packets"]) & set(queries[1]["packets"]) & set(queries[2]["packets"]))
    result = {**row, "queries": safe(queries), "status": "no_common_depth", "selection_use": "post-play independent audit only"}
    if not common:
        return result
    level = common[-1]
    values = [value["packets"][level]["score"].pov(board.turn) for value in queries]
    cp = [value.score() for value in values]
    mates = [value.mate() for value in values]
    result.update(status="exact_common_depth", depth=level,
        free_cp_own=cp[0], native_baseline_cp_own=cp[1], actual_cp_own=cp[2],
        free_mate_own=mates[0], native_baseline_mate_own=mates[1], actual_mate_own=mates[2],
        actual_loss_vs_free_cp=max(0, cp[0] - cp[2]) if cp[0] is not None and cp[2] is not None else None,
        actual_loss_vs_native_baseline_cp=cp[1] - cp[2] if cp[1] is not None and cp[2] is not None else None)
    result["critical_cp_regression"] = (result["actual_loss_vs_free_cp"] is not None
        and result["actual_loss_vs_free_cp"] > 150 and result["actual_loss_vs_native_baseline_cp"] is not None
        and result["actual_loss_vs_native_baseline_cp"] >= 100)
    result["new_negative_mate_vs_baseline"] = (mates[2] is not None and mates[2] < 0
        and not (mates[1] is not None and mates[1] < 0))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--stockfish", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seconds", type=float, default=.5)
    parser.add_argument("--depth", type=int, default=14)
    parser.add_argument("--target", action="append", default=[], help="Audit an irrevocably completed CASE:PLY only")
    args = parser.parse_args()
    report_bytes = args.report.read_bytes()
    report = json.loads(report_bytes)
    if "finished_utc" not in report and not args.target:
        raise ValueError("Actual practice has not finished")
    rows = []
    for case in report["results"]:
        if "finished_utc" not in report and not case.get("passed"):
            continue
        for turn in case["own_turns"]:
            receipt = turn["receipt"]
            operation = receipt.get("operations") or {}
            if operation.get("applied") is True and receipt["selected_uci"] != receipt["baseline_uci"]:
                if args.target and f"{case['case']['id']}:{turn['ply']}" not in args.target:
                    continue
                rows.append({"case": case["case"]["id"], "family_id": case["case"]["family_id"],
                    "ply": turn["ply"], "before_fen": turn["before_fen"], "history_uci": receipt["history_uci"],
                    "baseline_uci": receipt["baseline_uci"], "selected_uci": receipt["selected_uci"],
                    "operations_context": receipt.get("operations_context"),
                    "source": (operation.get("evidence") or {}).get("source"),
                    "live_native_loss_cp": operation.get("loss_cp"), "original_audit": turn["audit"]})
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "input-report-snapshot.json").write_bytes(report_bytes)
    logger = logging.getLogger("chess.engine")
    logger.setLevel(logging.DEBUG)
    handler = logging.FileHandler(args.output / "raw-uci.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    engine = chess.engine.SimpleEngine.popen_uci([str(args.stockfish.resolve(strict=True))], timeout=20, **flags)
    engine.configure({k: v for k, v in {"Threads": 1, "Hash": 32, "Skill Level": 20,
                                      "UCI_LimitStrength": False}.items() if k in engine.options})
    result = {"schema": "regicide.gambit-operations-substitution-audit.v1",
        "started_utc": datetime.now(timezone.utc).isoformat(), "report_sha256": hashlib.sha256(report_bytes).hexdigest(),
        "stockfish_sha256": hashlib.sha256(args.stockfish.read_bytes()).hexdigest(), "engine_id": dict(engine.id),
        "seconds_per_query": args.seconds, "max_depth": args.depth,
        "parent_report_finished": "finished_utc" in report, "requested_targets": args.target,
        "scope": "Independent post-play audit at shared completed exact depth; not fed to move selection", "results": []}
    try:
        for row in rows:
            value = audit(engine, row, args.seconds, args.depth)
            result["results"].append(value)
            write(args.output / "report.json", result)
            print(json.dumps({k: value.get(k) for k in ("case", "ply", "status", "depth",
                "actual_loss_vs_free_cp", "actual_loss_vs_native_baseline_cp", "critical_cp_regression",
                "new_negative_mate_vs_baseline")}), flush=True)
    finally:
        engine.quit()
        result["audit_engine_closed"] = engine.returncode.done()
        handler.close()
        logger.removeHandler(handler)
    result.update(finished_utc=datetime.now(timezone.utc).isoformat(), substitutions=len(rows),
        critical_cp_regressions=sum(row.get("critical_cp_regression", False) for row in result["results"]),
        new_negative_mates=sum(row.get("new_negative_mate_vs_baseline", False) for row in result["results"]),
        exact_common_depth_queries=sum(row["status"] == "exact_common_depth" for row in result["results"]))
    write(args.output / "report.json", result)
    return 0 if result["audit_engine_closed"] and result["exact_common_depth_queries"] == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
