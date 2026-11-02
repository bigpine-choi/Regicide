"""Authored-position native evidence diagnostic, separate from rating accuracy.

The production 500ms observation budget is the default. A larger requested
budget is diagnostic only and is recorded explicitly. This never opens user
game records or the private rating store.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import logging
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
import boss_play
from runtime_engine import resolve_runtime_engine

SOURCES = ("scripts/boss_play.py", "scripts/rating_evidence.py", "scripts/sparring_evaluation.py",
           "scripts/opening_variety.py", "scripts/runtime_engine.py", "tests/rating_evidence_integration.py")


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def serial(value):
    if isinstance(value, chess.engine.PovScore):
        return {"pov": "white" if value.turn else "black",
                "cp_white": value.white().score(), "mate_white": value.white().mate()}
    if isinstance(value, chess.Move):
        return value.uci()
    if isinstance(value, dict):
        return {str(key): serial(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [serial(item) for item in value]
    return value


def observe(binary, history, budget_ms):
    board = chess.Board()
    for move in history:
        board.push_uci(move)
    original = board.fen(en_passant="fen"), list(board.move_stack)
    queries, pv_count = [], 0
    extra = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    with chess.engine.SimpleEngine.popen_uci([str(binary), "uci"], timeout=3, **extra) as engine:
        options = {"Hash": 16, "UsePieceActivity": True,
                   "UseNeuralEvaluation": False, "UseNNUEEvaluation": False}
        if "Threads" in engine.options:
            options["Threads"] = 1
        if "Move Overhead" in engine.options:
            options["Move Overhead"] = 20
        engine.configure(options)
        engine.configure({"Clear Hash": None})
        engine_metadata = {"id": dict(engine.id), "configured_options": options,
                           "search_threads": 1, "threads_uci_option": "Threads" in engine.options}
        def search(budget, *, root_move=None, position=None, publish=True, fresh=False, depth=None):
            nonlocal pv_count
            assert position is not None and fresh and not publish
            started = time.monotonic()
            kwargs = {"game": object(), "info": chess.engine.INFO_ALL}
            if root_move is not None:
                kwargs["root_moves"] = [root_move]
            events, packets, latest = [], {}, {}
            with engine.analysis(position.copy(stack=True), chess.engine.Limit(time=budget / 1000, depth=depth),
                                 **kwargs) as analysis:
                for info in analysis:
                    raw = dict(info)
                    events.append({"received_ms": round((time.monotonic() - started) * 1000, 3), "info": serial(raw)})
                    if "pv" in raw:
                        replay = position.copy(stack=True)
                        for move in raw["pv"]:
                            assert move in replay.legal_moves, (position.fen(), move, raw)
                            replay.push(move)
                        pv_count += 1
                    if type(raw.get("depth")) is int and "score" in raw:
                        packets[raw["depth"]] = raw
                        latest = raw
                reply = analysis.wait()
            assert reply.move is not None and reply.move in position.legal_moves
            if root_move is not None:
                assert reply.move == root_move
            queries.append({"before_fen": position.fen(en_passant="fen"),
                            "history_uci": [move.uci() for move in position.move_stack],
                            "requested_ms": budget, "requested_depth": depth,
                            "root_move": root_move.uci() if root_move else None,
                            "completed_ms": round((time.monotonic() - started) * 1000, 3),
                            "returned_uci": reply.move.uci(), "events": events})
            return reply, latest, packets
        result, discarded = boss_play.observe_human_move(board, search, engine.close, budget_ms=budget_ms)
    assert (board.fen(en_passant="fen"), list(board.move_stack)) == original
    return {"history_uci": history, "current_fen": original[0], "observation_budget_ms": budget_ms,
            "observation": result, "discarded_engine": discarded, "queries": queries,
            "engine": engine_metadata, "raw_pv_replayed": pv_count, "board_unchanged": True,
            "engine_closed": True}


def run(args):
    if not 100 <= args.budget_ms <= 2000:
        raise ValueError("Diagnostic observation budget must be 100..2000ms")
    output = args.output.resolve()
    if output.exists():
        raise ValueError("Use a fresh output path")
    binary = (args.binary or resolve_runtime_engine(ROOT)).resolve(strict=True)
    before = {name: digest(ROOT / name) for name in SOURCES}
    binary_hash = digest(binary)
    output.mkdir(parents=True)
    logger = logging.getLogger("chess.engine")
    handler = logging.FileHandler(output / "uci-protocol.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    rows = []
    try:
        for name, moves, expected in (("allowed_fools_mate", ["f2f3", "e7e5", "g2g4"], "allowed_mate"),
                                      ("ordinary_opening", ["e2e4"], "cp_loss")):
            row = {"id": name, "expected_kind": expected, **observe(binary, moves, args.budget_ms)}
            observed = row["observation"]["rating_evidence"]
            row["expected_event_observed"] = observed["valid"] and observed["kind"] == expected
            rows.append(row)
            (output / (name + ".json")).write_text(json.dumps(serial(row), indent=2), encoding="utf-8")
            print(json.dumps({"id": name, "evidence": observed, "elapsed_ms": row["observation"]["elapsed_ms"]}), flush=True)
    finally:
        logger.removeHandler(handler)
        handler.close()
        logger.setLevel(old_level)
    after = {name: digest(ROOT / name) for name in SOURCES}
    unchanged = before == after and digest(binary) == binary_hash
    result = {"created_utc": datetime.now(timezone.utc).isoformat(), "binary": str(binary),
              "binary_sha256": binary_hash, "source_sha256": before, "source_sha256_after": after,
              "inputs_unchanged": unchanged, "budget_ms": args.budget_ms,
              "production_500ms_budget": args.budget_ms == 500,
              "python": platform.python_version(), "platform": platform.platform(),
              "processor": platform.processor(), "all_expected_observed": all(row["expected_event_observed"] for row in rows),
              "raw_pv_replayed": sum(row["raw_pv_replayed"] for row in rows),
              "all_engines_closed": all(row["engine_closed"] for row in rows),
              "cases": [{"id": row["id"], "observed": row["expected_event_observed"],
                         "rating_evidence": row["observation"]["rating_evidence"],
                         "elapsed_ms": row["observation"]["elapsed_ms"]} for row in rows],
              "scope": "Two authored positions, local own-engine raw packets; finite budget evidence-path check, not empirical user rating accuracy or playing strength. No personal games or labels accessed."}
    (output / "validation.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    assert unchanged, "Sources changed during native observation"
    return 0 if result["all_expected_observed"] else 2


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--budget-ms", type=int, default=500)
    parser.add_argument("--output", type=Path, required=True)
    raise SystemExit(run(parser.parse_args()))
