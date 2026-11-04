"""Diagnose known gambit failures with continued versus equal-total search.

This is a known-failure development set, never an independent strength test.
No production policy or active binary is changed. A 1000ms search followed by
750ms on the same engine/position/game token is compared with a fresh single
1750ms search. The first move remains available when continuation fails.
Stockfish, when requested, audits only moves already selected by Regicide.
The current Regicide reuses the TT buffer but invalidates its entries on each
search; the second call is a session restart, not a resumed or warm search.
"""
import argparse
import gzip
import importlib.util
import json
import logging
import math
import os
from pathlib import Path
import platform
import time

import chess
import chess.engine

from boss_strength_match import native_engine, sha, utc, write_json
from runtime_engine import resolve_runtime_engine
from play_game import source_version

ROOT = Path(__file__).resolve().parents[1]
PROOF = ROOT / "docs/checks/2026-11-04-gambit-operations"
DEFAULT_REPORT = PROOF / "native-after-v2/report.json"
DEFAULT_FIXTURES = PROOF / "predeclared/fixtures.json"
AUDIT_HELPER = PROOF / "audit_operations_choices.py"


def load_audit():
    spec = importlib.util.spec_from_file_location("gambit_budget_audit", AUDIT_HELPER)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    return helper


def known_failures(report, threshold=150):
    """Take every qualifying saved turn; no hand selection by new results."""
    rows = []
    for case in report["results"]:
        for turn in case["own_turns"]:
            loss = turn.get("audit", {}).get("loss_cp")
            if type(loss) not in (int, float) or not math.isfinite(loss) or loss <= threshold:
                continue
            history = turn["receipt"]["history_uci"]
            row = {"id": f"{case['case']['id']}:{turn['ply']}", "case": case["case"]["id"],
                   "ply": turn["ply"], "history_uci": history,
                   "before_fen": turn["before_fen"], "saved_move_uci": turn["uci"],
                   "saved_loss_cp": loss, "saved_audit_depth": turn["audit"].get("depth")}
            board_for(row)
            rows.append(row)
    if not rows:
        raise ValueError("No qualifying known failures in input report")
    if len({row["id"] for row in rows}) != len(rows):
        raise ValueError("Duplicate saved case/ply")
    return rows


def board_for(row):
    board = chess.Board()
    for uci in row["history_uci"]:
        board.push_uci(uci)
    if (not board.is_valid() or board.fen(en_passant="fen") != row["before_fen"]
            or row["ply"] != len(board.move_stack) + 1
            or (row.get("saved_move_uci") is not None
                and chess.Move.from_uci(row["saved_move_uci"]) not in board.legal_moves)):
        raise ValueError("Saved position, full history, ply or move disagrees")
    return board


def declared_controls(fixtures, failures, count):
    """First distinct predeclared roots outside failure histories, no scores."""
    seen = {tuple(row["history_uci"]) for row in failures}
    controls = []
    for case in fixtures["cases"]:
        history = tuple(case["history_uci"])
        if history in seen or len(controls) >= count:
            continue
        row = {"id": "control:" + case["id"], "case": case["id"],
               "ply": len(history) + 1, "history_uci": list(history),
               "before_fen": case["initial_fen"], "set": "predeclared_control"}
        board_for(row)
        seen.add(history)
        controls.append(row)
    return controls


def exact_packet(board, packet, root=None):
    """Accept only exact, legal single-PV evidence in the current root POV."""
    if not isinstance(packet, dict):
        return False
    score, depth, pv = packet.get("score"), packet.get("depth"), packet.get("pv")
    if (not isinstance(score, chess.engine.PovScore) or score.turn != board.turn
            or type(depth) is not int or depth < 1 or packet.get("multipv", 1) != 1
            or packet.get("lowerbound") or packet.get("upperbound")
            or not isinstance(pv, (list, tuple)) or not pv or (root is not None and pv[0] != root)):
        return False
    replay = board.copy(stack=True)
    for move in pv:
        if not isinstance(move, chess.Move) or move not in replay.legal_moves:
            return False
        replay.push(move)
    return True


def native_query(engine, board, milliseconds, token, safe):
    """Keep the session/token; the engine still invalidates old TT entries."""
    before = board.fen(en_passant="fen"), list(board.move_stack)
    packets = []
    completed = []
    started = time.monotonic()
    result = {"requested_ms": milliseconds, "root_fen": before[0],
              "history_uci": [move.uci() for move in before[1]], "root_moves": None,
              "fresh": False, "packets": packets, "returned_uci": None, "error": None}
    try:
        with engine.analysis(board.copy(stack=True), chess.engine.Limit(time=milliseconds / 1000),
                             game=token, info=chess.engine.INFO_ALL) as stream:
            for packet in stream:
                packets.append(safe(packet))
                if exact_packet(board, packet):
                    completed.append(packet)
            answer = stream.wait()
        if answer.move not in board.legal_moves:
            raise ValueError("Native search returned no legal root move")
        matching = [packet for packet in completed if packet["pv"][0] == answer.move]
        if not matching:
            raise ValueError("Native returned root has no exact completed legal PV evidence")
        line = max(matching, key=lambda packet: packet["depth"])
        value = line["score"].pov(board.turn)
        result["completed_line"] = {"depth": line["depth"], "cp_own": value.score(),
            "mate_own": value.mate(), "root_uci": answer.move.uci(), "packet": safe(line)}
        result["returned_uci"] = answer.move.uci()
    except Exception as error:
        result["error"] = f"{type(error).__name__}: {error}"
    result["elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
    result["position_unchanged"] = before == (board.fen(en_passant="fen"), list(board.move_stack))
    return result


def compare_searches(factory, board, baseline_ms, continuation_ms, safe):
    result = {"queries": [], "selected": {}, "engines": [], "all_owned_engines_closed": True}
    owned = []
    try:
        engine = factory()
        owned.append(engine)
        result["engines"].append({"instance": 1, "id": dict(engine.id)})
        token = object()
        first = native_query(engine, board, baseline_ms, token, safe)
        first.update(label="baseline", engine_instance=1, game_token_group=1, fresh=True)
        result["queries"].append(first)
        result["selected"]["baseline"] = first["returned_uci"]
        if first["returned_uci"] is not None:
            continued = native_query(engine, board, continuation_ms, token, safe)
            continued.update(label="continuation", engine_instance=1, game_token_group=1)
            result["queries"].append(continued)
            result["selected"]["continued"] = continued["returned_uci"] or first["returned_uci"]
            result["continuation_fallback_to_baseline"] = continued["returned_uci"] is None
        else:
            result["selected"]["continued"] = None
            result["continuation_skipped"] = "baseline_failed"
        # A separate process prevents TT carryover from the two-stage arm.
        control = factory()
        owned.append(control)
        result["engines"].append({"instance": 2, "id": dict(control.id)})
        single = native_query(control, board, baseline_ms + continuation_ms, object(), safe)
        single.update(label="single_equal_total", engine_instance=2, game_token_group=2, fresh=True)
        result["queries"].append(single)
        result["selected"]["single_equal_total"] = single["returned_uci"]
    finally:
        for engine in owned:
            try:
                engine.quit()
            except Exception as error:
                result.setdefault("close_errors", []).append(f"{type(error).__name__}: {error}")
                engine.close()
            result["all_owned_engines_closed"] &= engine.returncode.done()
    return result


def audit_selected(engine, board, selected, helper, seconds=.5, depth=14):
    """Compare every arm and free search at one exact common depth."""
    if not selected or any(move is None for move in selected.values()):
        return {"status": "incomplete_native_selection"}
    queries = {"free": helper.query(engine, board, None, seconds, depth)}
    label_keys = {}
    for label, uci in selected.items():
        move = chess.Move.from_uci(uci)
        if move not in board.legal_moves:
            raise ValueError("Audit candidate is illegal")
        label_keys[label] = uci
        if uci not in queries:
            queries[uci] = helper.query(engine, board, move, seconds, depth)
    valid = {}
    for key, query in queries.items():
        root = None if key == "free" else chess.Move.from_uci(key)
        answer_valid = root is None or query.get("bestmove") == root.uci()
        valid[key] = {level: packet for level, packet in query["packets"].items()
                      if answer_valid and type(level) is int and packet.get("depth") == level
                      and exact_packet(board, packet, root)}
    common = set.intersection(*(set(packets) for packets in valid.values()))
    result = {"status": "no_common_depth", "queries": helper.safe(queries), "label_query_keys": label_keys,
              "selection_use": "post-selection independent audit only"}
    if not common:
        return result
    level = max(common)
    values = {"free": valid["free"][level]["score"].pov(board.turn)}
    values.update({label: valid[key][level]["score"].pov(board.turn) for label, key in label_keys.items()})
    scores = {label: {"cp_own": score.score(), "mate_own": score.mate()}
              for label, score in values.items()}
    cp_free = scores["free"]["cp_own"]
    result.update(status="exact_common_depth", depth=level, scores=scores,
                  loss_vs_free_cp={label: max(0, cp_free - value["cp_own"])
                      if cp_free is not None and value["cp_own"] is not None else None
                      for label, value in scores.items() if label != "free"})
    return result


def completion_status(result, expected_cases, *, require_audit=False):
    rows = result["results"]
    native_complete = (len(rows) == expected_cases and result.get("sources_unchanged") is True
        and result.get("all_owned_engines_closed") is True
        and all(all(row["selected"].values())
            and len(row["queries"]) == 3
            and all(query["position_unchanged"] and not query["error"] for query in row["queries"])
            for row in rows))
    audit_complete = (len(rows) == expected_cases and all(
        row.get("audit", {}).get("status") == "exact_common_depth" for row in rows)) if require_audit else None
    return {"native_completed": native_complete, "audit_completed": audit_complete,
            "passed": native_complete and (audit_complete is True if require_audit else True)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--stockfish", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline-ms", type=int, default=1000)
    parser.add_argument("--continuation-ms", type=int, default=750)
    parser.add_argument("--audit-seconds", type=float, default=.5)
    parser.add_argument("--audit-depth", type=int, default=14)
    parser.add_argument("--max-controls", type=int, default=0)
    parser.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    args = parser.parse_args(argv)
    if (args.max_controls < 0 or min(args.baseline_ms, args.continuation_ms, args.audit_depth) <= 0
            or not math.isfinite(args.audit_seconds) or args.audit_seconds <= 0):
        parser.error("Search limits must be positive and finite")
    binary = (args.binary or resolve_runtime_engine()).resolve(strict=True)
    report_path = args.report.resolve(strict=True)
    helper = load_audit()
    cases = known_failures(json.loads(report_path.read_text(encoding="utf-8")))
    for case in cases:
        case["set"] = "known_failure_development"
    paths = [Path(__file__), AUDIT_HELPER, report_path, binary,
             ROOT / "scripts/boss_strength_match.py", ROOT / "scripts/runtime_engine.py"]
    if args.stockfish:
        args.stockfish = args.stockfish.resolve(strict=True)
        paths.append(args.stockfish)
    if args.max_controls:
        args.fixtures = args.fixtures.resolve(strict=True)
        controls = declared_controls(json.loads(args.fixtures.read_text(encoding="utf-8")), cases, args.max_controls)
        cases.extend(controls)
        paths.append(args.fixtures)
    hashes = {str(path): sha(path) for path in paths}
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "input-report-snapshot.json.gz").write_bytes(gzip.compress(report_path.read_bytes(), mtime=0))
    logging.basicConfig(filename=args.output / "raw-uci.log", level=logging.DEBUG,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    result = {"schema": "regicide.gambit-search-budget.v1", "started_utc": utc(),
        "scope": "Known-failure development set; no independent strength, Elo or full-game claim",
        "selection": "All saved external loss_cp >150; controls are first distinct predeclared fixture roots excluding failure histories; no selection using this run's results",
        "source_sha256": hashes, "python": platform.python_version(), "platform": platform.platform(),
        "source": source_version(), "processor": platform.processor(), "logical_cpus": os.cpu_count(),
        "python_chess": chess.__version__, "config": {"baseline_ms": args.baseline_ms,
            "continuation_ms": args.continuation_ms, "equal_total_ms": args.baseline_ms + args.continuation_ms,
            "repeats": 1, "max_controls": args.max_controls, "threads": 1, "hash_mb": 16, "use_piece_activity": True,
            "audit_seconds": args.audit_seconds, "audit_depth": args.audit_depth},
        "limitations": ["Timed searches are not bitwise deterministic or an equal CPU measurement",
            "Controls are previously declared development fixtures, not unseen holdout evidence",
            "The same session retains the TT buffer; current Regicide invalidates old entries at each search",
            "Restarting search does not resume an interrupted depth iteration",
            "Known failures are development evidence; future unseen roots and paired games are required",
            "This diagnostic does not run or change production gambit move policy"],
        "results": []}
    audit_engine = None
    try:
        def factory():
            engine = native_engine(binary, boss=True)
            try:
                if not {"Hash", "UsePieceActivity"}.issubset(engine.options):
                    raise ValueError("Native engine requires Hash and UsePieceActivity")
                engine.configure({key: value for key, value in {"Threads": 1, "Hash": 16,
                    "UsePieceActivity": True}.items() if key in engine.options})
                return engine
            except BaseException:
                engine.close()
                raise
        for case in cases:
            board = board_for(case)
            row = {"case": case, **compare_searches(factory, board, args.baseline_ms,
                                                    args.continuation_ms, helper.safe)}
            result["results"].append(row)
            write_json(args.output / "report.json", result)
            print(json.dumps({"case": case["id"], "selected": row["selected"]}), flush=True)
        # All Regicide decisions precede every independent audit.
        if args.stockfish:
            audit_engine = native_engine(args.stockfish, boss=False)
            audit_engine.configure({key: value for key, value in {"Threads": 1, "Hash": 32,
                "Skill Level": 20, "UCI_LimitStrength": False}.items() if key in audit_engine.options})
            result["audit_engine_id"] = dict(audit_engine.id)
            for row in result["results"]:
                row["audit"] = audit_selected(audit_engine, board_for(row["case"]), row["selected"],
                                                helper, args.audit_seconds, args.audit_depth)
                write_json(args.output / "report.json", result)
    finally:
        if audit_engine is not None:
            audit_engine.quit()
            result["audit_engine_closed"] = audit_engine.returncode.done()
        result.update(finished_utc=utc(), sources_unchanged=all(sha(path) == value for path, value in hashes.items()))
        result["all_owned_engines_closed"] = (all(row["all_owned_engines_closed"] for row in result["results"])
                                                and result.get("audit_engine_closed", True))
        result.update(completion_status(result, len(cases), require_audit=args.stockfish is not None))
        write_json(args.output / "report.json", result)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
