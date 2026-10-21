"""Bounded offline Stockfish labels; Regicide remains the playing engine.

The input bank and its partitions are never changed or replenished. Mate and
bound scores are excluded rather than converted into invented centipawns.
"""
from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import time

import chess
import chess.engine

from neural_search_data.schema import board_from, sha256, write_json


OPTIONS = {"Threads": 1, "Hash": 16, "Skill Level": 20,
           "UCI_LimitStrength": False, "SyzygyPath": ""}
POLICIES = {"history": "Replay source initial_fen/history and require final FEN equality",
            "hash": "Clear Hash then a distinct game object (ucinewgame/isready) per query",
            "score": "Last score-bearing info packet; white POV; exact CP only",
            "budget": "go nodes; actual completed search nodes are recorded (not a hard cap)",
            "rejections": "No replacement rows, no mate-to-CP conversion, no score clipping"}


def source_board(row: dict) -> chess.Board:
    """Keep repetition history when supplied; reject inconsistent source rows."""
    if not isinstance(row, dict) or not isinstance(row.get("fen"), str):
        raise ValueError("A source row requires its full FEN")
    if "history" in row or "initial_fen" in row:
        board = board_from(row.get("initial_fen"), row.get("history"))
    else:
        board = board_from(row["fen"], [])
    declared = chess.Board(row["fen"])
    if board.fen(en_passant="fen") != declared.fen(en_passant="fen"):
        raise ValueError("Source history does not match declared FEN")
    return board


def _json_info(value):
    """Retain every structured info field without serializing opaque objects."""
    if isinstance(value, chess.Move):
        return value.uci()
    if isinstance(value, chess.engine.PovScore):
        return {"pov": "white" if value.turn else "black", "relative": str(value.relative),
                "white_cp": value.white().score(), "white_mate": value.white().mate()}
    if isinstance(value, chess.engine.PovWdl):
        return {"pov": "white" if value.turn else "black",
                "relative": list(value.relative), "white": list(value.white())}
    if isinstance(value, dict):
        return {str(key): _json_info(entry) for key, entry in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_info(entry) for entry in value]
    if value is None or type(value) in (bool, int, float, str):
        return value
    raise TypeError("Unsupported structured engine info: " + type(value).__name__)


def assess_info(board: chess.Board, info: dict, bestmove: chess.Move | None,
                *, max_abs_cp: int = 1500) -> tuple[dict, list[str]]:
    """Validate the score-bearing packet; do not infer CP from a mate score."""
    reasons, score = [], info.get("score")
    values = {"kind": "missing", "stm_cp": None, "white_cp": None,
              "mate_stm": None, "mate_white": None, "pov": None}
    if isinstance(score, chess.engine.PovScore):
        values = {"kind": "mate" if score.is_mate() else "cp",
                  "stm_cp": score.pov(board.turn).score(), "white_cp": score.white().score(),
                  "mate_stm": score.pov(board.turn).mate(), "mate_white": score.white().mate(),
                  "pov": "white" if score.turn else "black"}
        if score.turn != board.turn:
            reasons.append("score_pov")
        if values["kind"] != "cp":
            reasons.append("mate")
        elif type(values["white_cp"]) is not int:
            reasons.append("score_value")
        elif abs(values["white_cp"]) > max_abs_cp:
            reasons.append("score_range")
    else:
        reasons.append("missing_score")
    if info.get("lowerbound", False) or info.get("upperbound", False):
        reasons.append("bound")
    for name in ("depth", "nodes"):
        if type(info.get(name)) is not int or info[name] < 1:
            reasons.append(name)
    pv = info.get("pv")
    if not isinstance(pv, list) or not pv:
        reasons.append("missing_pv")
    else:
        replay = board.copy(stack=True)
        for move in pv:
            if not isinstance(move, chess.Move) or move not in replay.legal_moves:
                reasons.append("illegal_pv")
                break
            replay.push(move)
        if bestmove != pv[0]:
            reasons.append("bestmove_pv_mismatch")
    if bestmove is None or bestmove not in board.legal_moves:
        reasons.append("illegal_bestmove")
    if board.outcome(claim_draw=True) is not None:
        reasons.append("terminal")
    return values, reasons


def label_rows(engine_path, rows: list[dict], output_dir, *, nodes: int = 10000,
               max_abs_cp: int = 1500, engine_factory=chess.engine.SimpleEngine.popen_uci,
               progress=None) -> tuple[list[dict], dict]:
    """Label one frozen small bank with one process, preserving original rows.

    Writes a receipt for every attempted row and publishes only accepted labels.
    Engine failures propagate, leaving partial receipts for diagnosis, rather
    than silently accepting an interrupted training bank.
    """
    if type(nodes) is not int or nodes < 1 or type(max_abs_cp) is not int or max_abs_cp < 1:
        raise ValueError("Require positive integer node and CP bounds")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Require a nonempty source row list")
    # Validate every history before launching any search or creating artifacts.
    boards = [source_board(row) for row in rows]
    engine_path, output = Path(engine_path).resolve(), Path(output_dir)
    binary_hash = sha256(engine_path)
    output.mkdir(parents=True, exist_ok=False)
    accepted, exclusions = [], Counter()
    started = time.perf_counter()
    with (output / "teacher-queries.jsonl").open("x", encoding="utf-8", newline="\n") as receipts:
        with engine_factory(str(engine_path), timeout=30.0) as engine:
            if "Clear Hash" not in engine.options or "MultiPV" not in engine.options:
                raise ValueError("Teacher lacks required Clear Hash/MultiPV controls")
            engine.configure(OPTIONS)
            identity = dict(engine.id)
            effective_options = {**OPTIONS, "MultiPV": 1, "UCI_AnalyseMode": True}
            for index, (row, board) in enumerate(zip(rows, boards)):
                query = {"query_id": f"sf-{index + 1:06d}", "index": index,
                         "source_query_id": row.get("query_id"), "fen": row["fen"],
                         "partition": row.get("partition"), "family_id": row.get("family_id"),
                         "binary_sha256": binary_hash, "engine_id": identity,
                         "options": effective_options, "requested_nodes": nodes,
                         "history_plies": len(board.move_stack)}
                if board.outcome(claim_draw=True) is not None:
                    reasons = ["terminal"]
                else:
                    engine.configure({"Clear Hash": None})
                    last_score = {}
                    with engine.analysis(board, chess.engine.Limit(nodes=nodes), game=object(),
                                         info=chess.engine.INFO_ALL) as analysis:
                        for packet in analysis:
                            if "score" in packet:
                                last_score = packet.copy()
                        bestmove = analysis.wait().move
                        aggregate = analysis.info
                    values, reasons = assess_info(board, last_score, bestmove, max_abs_cp=max_abs_cp)
                    query.update({"score": values, "depth": last_score.get("depth"),
                                  "nodes": aggregate.get("nodes"), "score_nodes": last_score.get("nodes"),
                                  "pv_uci": [move.uci() for move in last_score.get("pv", [])],
                                  "bestmove_uci": bestmove.uci() if bestmove else None,
                                  "raw_info": _json_info(aggregate),
                                  "raw_score_info_packet": _json_info(last_score)})
                query.update(status="excluded" if reasons else "accepted", reasons=reasons)
                receipts.write(json.dumps(query, sort_keys=True, allow_nan=False) + "\n")
                receipts.flush()
                if reasons:
                    exclusions.update(reasons)
                else:
                    accepted.append({**row, "source_target_white_cp": row.get("target_white_cp"),
                                     "target_white_cp": values["white_cp"],
                                     "teacher_white_cp": values["white_cp"],
                                     "teacher_stm_cp": values["stm_cp"],
                                     "teacher_query_id": query["query_id"],
                                     "teacher_source": "Stockfish offline root evaluation"})
                if progress and ((index + 1) % 32 == 0 or index + 1 == len(rows)):
                    progress({"completed": index + 1, "requested": len(rows), "accepted": len(accepted)})
    labels_path = output / "labels.jsonl"
    with labels_path.open("x", encoding="utf-8", newline="\n") as stream:
        for row in accepted:
            stream.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
    report = {"schema_version": 1, "engine_path": str(engine_path), "binary_sha256": binary_hash,
              "engine_id": identity, "options": effective_options, "policies": POLICIES,
              "requested_nodes": nodes, "max_abs_cp": max_abs_cp, "requested_rows": len(rows),
              "accepted_rows": len(accepted), "excluded_rows": len(rows) - len(accepted),
              "exclusion_reasons": dict(exclusions), "elapsed_seconds": time.perf_counter() - started,
              "accepted_by_partition": dict(Counter(row.get("partition") for row in accepted)),
              "files": {name: sha256(output / name) for name in ("teacher-queries.jsonl", "labels.jsonl")}}
    write_json(output / "teacher-report.json", report)
    return accepted, report
