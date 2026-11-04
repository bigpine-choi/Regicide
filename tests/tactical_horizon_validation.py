"""Small cold own-engine A/B screen, followed only then by optional audits.

Nodes, time, policy contracts and independent audit scores are separate
evidence. The fixture holdout is reported separately. Nothing here activates
an engine, changes a user game or asserts that a changed move is stronger.
"""
import argparse
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import threading
import time
from unittest.mock import patch

import chess
import chess.engine

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
from perpetual_check_validation import draw_state, legal_line, replay, safe, sha

FIXTURES = ROOT / "tests/data/tactical_horizon_2026_11_04.json"


def utc():
    return datetime.now(timezone.utc).isoformat()


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def configure(engine, option=None, enabled=None):
    required = {"UsePieceActivity": True, "Hash": 16,
                "UseNeuralEvaluation": False, "UseNNUEEvaluation": False}
    if not set(required).issubset(engine.options):
        raise ValueError("Native engine must advertise activity, hash and evaluator controls")
    options = {**required, **({"Threads": 1} if "Threads" in engine.options else {})}
    if "UsePositionalEvaluation" in engine.options:
        options["UsePositionalEvaluation"] = True
    if option:
        if option not in engine.options or engine.options[option].type != "check":
            raise ValueError("Requested candidate check option unavailable: " + option)
        options[option] = enabled
    for name, value in options.items():
        engine.options[name].parse(value)
    engine.configure(options)
    engine.ping()
    return options


def query(binary, board, limit, option=None, enabled=None, roots=None):
    before = board.fen(en_passant="fen"), list(board.move_stack)
    row = {"requested": {"nodes": limit.nodes, "time": limit.time, "depth": limit.depth},
           "root_moves": safe(roots), "packets": [], "selected_uci": None, "completed_line": None,
           "status": "failed", "error": None, "engine_closed": False, "process_exit_code": None}
    engine = watchdog = None
    expired = threading.Event()
    started = time.monotonic()
    try:
        flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        engine = chess.engine.SimpleEngine.popen_uci([str(binary), "uci"], timeout=10, **flags)
        def expire():
            expired.set()
            engine.close()
        watchdog = threading.Timer(max(15, (limit.time or 0) + 3), expire)
        watchdog.daemon = True
        watchdog.start()
        row["engine_id"] = dict(engine.id)
        row["advertised_options"] = {name: {"type": value.type, "default": value.default}
                                     for name, value in engine.options.items()}
        row["configured_options"] = configure(engine, option, enabled)
        exact = []
        with engine.analysis(board.copy(stack=True), limit, root_moves=roots,
                             game=object(), info=chess.engine.INFO_ALL) as stream:
            for packet in stream:
                row["packets"].append(safe(packet))
                if limit.nodes and type(packet.get("nodes")) is int and packet["nodes"] > limit.nodes:
                    raise ValueError("Native query exceeded its fixed node budget")
                line = legal_line(board, packet)
                if line and (roots is None or chess.Move.from_uci(line["pv_uci"][0]) in roots):
                    exact.append(line)
            answer = stream.wait()
        terminal = board.outcome(claim_draw=False)
        row["returned_uci"] = answer.move.uci() if answer.move is not None else None
        if terminal is not None:
            if answer.move:
                raise ValueError("Compulsory terminal root returned a move")
            row.update(status="recorded_terminal", terminal_result=terminal.result(),
                       terminal_termination=terminal.termination.name.lower())
        else:
            if answer.move not in board.legal_moves or roots is not None and answer.move not in roots:
                raise ValueError("Native returned an illegal or excluded root move")
            row["selected_uci"] = answer.move.uci()
            matching = [line for line in exact if line["pv_uci"][0] == answer.move.uci()]
            if not matching:
                raise ValueError("Returned root has no exact completed legal PV")
            row["completed_line"] = max(enumerate(matching), key=lambda pair: (pair[1]["depth"], pair[0]))[1]
            after = board.copy(stack=True)
            after.push(answer.move)
            row.update(status="recorded", selected_san=board.san(answer.move),
                       after_fen=after.fen(en_passant="fen"), after_draw_state=draw_state(after))
        counter = [re.search(r"queen-defense qnodes (\d+)", str(packet.get("string", "")))
                   for packet in row["packets"]]
        row["queen_defense_qnodes"] = next((int(match.group(1)) for match in reversed(counter) if match), None)
    except Exception as error:
        row["error"] = f"{type(error).__name__}: {error}"
    finally:
        if watchdog:
            watchdog.cancel()
            watchdog.join()
        if engine:
            try:
                engine.quit()
            except Exception as error:
                row["close_error"] = type(error).__name__
                engine.close()
            try:
                row["process_exit_code"] = engine.returncode.result(timeout=3)
                row["engine_closed"] = True
            except Exception as error:
                row["close_error"] = type(error).__name__
        row.update(elapsed_ms=round((time.monotonic() - started) * 1000, 3),
                   timeout=expired.is_set(), position_unchanged=before == (board.fen(en_passant="fen"), list(board.move_stack)))
        totals = [packet["nodes"] for packet in row["packets"]
                  if type(packet.get("nodes")) is int and packet["nodes"] >= 0]
        row["final_total_nodes"] = max(totals) if totals else None
        row["execution_valid"] = (row["status"] in ("recorded", "recorded_terminal") and row["engine_closed"]
            and type(row["process_exit_code"]) is int and row["process_exit_code"] == 0
            and row["position_unchanged"] and not row["timeout"] and row["error"] is None)
    return row


def fingerprint(row):
    line = row["completed_line"] or {}
    return {"selected": row["selected_uci"], "terminal": row.get("terminal_result"),
            **{key: line.get(key) for key in ("depth", "cp_root", "mate_root", "pv_uci", "nodes", "seldepth")}}


def sf_audit(engine, board, moves, seconds=.5, depth=14):
    """Saved move audit only; caller owns the engine and must quit it explicitly."""
    rows = {}
    for uci in [None, *sorted(set(moves))]:
        if "Clear Hash" in engine.options:
            engine.configure({"Clear Hash": None})
        packets, raw = {}, []
        move = chess.Move.from_uci(uci) if uci else None
        with engine.analysis(board.copy(stack=True), chess.engine.Limit(time=seconds, depth=depth),
                             root_moves=[move] if move else None, game=object(), info=chess.engine.INFO_ALL) as stream:
            for packet in stream:
                raw.append(safe(packet))
                line = legal_line(board, packet, move)
                if line:
                    packets[line["depth"]] = line
            answer = stream.wait()
        if move and answer.move != move:
            packets = {}
        rows[uci or "free"] = {"returned_uci": answer.move.uci() if answer.move else None,
                               "packets": raw, "exact": packets}
    common = set.intersection(*(set(row["exact"]) for row in rows.values()))
    result = {"scope": "post-selection audit only; never supplied to native selection", "queries": rows,
              "status": "no_common_exact_depth"}
    if common:
        level = max(common)
        scores = {key: row["exact"][level] for key, row in rows.items()}
        cp = scores["free"]["cp_root"]
        result.update(status="common_exact_depth", depth=level, scores=scores,
            loss_cp={key: max(0, cp - value["cp_root"]) if cp is not None and value["cp_root"] is not None else None
                     for key, value in scores.items() if key != "free"})
    return result


def host_smoke(binary, output, option, enabled, milliseconds):
    # Reuse the production-host fixture/receipt/trace validator. No user save
    # directory or running process is accessed, and display peers never search.
    import gambit_character_validation as host
    original = host.native_engine
    requested = []
    def configured(path, *, boss):
        engine = original(path, boss=boss)
        try:
            requested.append(configure(engine, option, enabled))
            return engine
        except BaseException:
            engine.close()
            raise
    output.mkdir()
    rows = []
    with patch.object(host, "native_engine", side_effect=configured):
        for fixture in (host.CASES[0], host.CASES[3]):
            case = host.prepare(fixture, "tactical-horizon-policy-preservation", milliseconds)
            rows.append(host.run_case(case, binary, output, milliseconds))
    return {"requested_options": requested, "results": rows,
            "contracts_passed": all(row["passed"] for row in rows),
            "scope": "Actual HumanGame with UsePieceActivity=true; authored acceptance is not a played match"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--candidate-option", default="UseQuiescenceQueenDefense")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--fixtures", type=Path, default=FIXTURES)
    parser.add_argument("--nodes", type=int, default=100_000)
    parser.add_argument("--movetime-ms", type=int, default=100)
    parser.add_argument("--stockfish", type=Path)
    parser.add_argument("--host-smoke", action="store_true")
    parser.add_argument("--host-ms", type=int, default=500)
    parser.add_argument("--parity-only", action="store_true")
    args = parser.parse_args(argv)
    if min(args.nodes, args.movetime_ms) < 1 or args.host_ms < 500:
        parser.error("Positive node/time limits and host-ms >=500 are required")
    args.baseline = args.baseline.resolve(strict=True)
    args.candidate = args.candidate.resolve(strict=True)
    args.fixtures = args.fixtures.resolve(strict=True)
    document = json.loads(args.fixtures.read_text(encoding="utf-8"))
    for case in document["cases"]:
        board = replay(case)
        if board.fen(en_passant="fen") != case["before_fen"]:
            parser.error("Declared history/FEN mismatch: " + case["id"])
        if any(chess.Move.from_uci(uci) not in board.legal_moves for uci in case.get("root_moves", [])):
            parser.error("Illegal declared root restriction")
    args.output_dir.mkdir(parents=True, exist_ok=False)
    paths = [Path(__file__).resolve(), args.fixtures, args.baseline, args.candidate,
             ROOT / "tests/perpetual_check_validation.py"]
    if args.host_smoke:
        paths += [ROOT / "tests/gambit_character_validation.py", *sorted((ROOT / "scripts").glob("*.py"))]
    if args.stockfish:
        args.stockfish = args.stockfish.resolve(strict=True)
        paths.append(args.stockfish)
    hashes = {str(path): sha(path) for path in paths}
    write(args.output_dir / "input-fixtures.json", document)
    report = {"schema": "regicide.tactical-horizon-validation.v1", "started_utc": utc(),
        "source_sha256": hashes, "python": platform.python_version(), "python_chess": chess.__version__,
        "platform": platform.platform(), "processor": platform.processor(), "logical_cpus": os.cpu_count(),
        "config": {"nodes": args.nodes, "movetime_ms": args.movetime_ms, "candidate_option": args.candidate_option,
            "activity": True, "hash_mb": 16, "cold": "fresh serial native process per query",
            "host_ms": args.host_ms if args.host_smoke else None},
        "scope": "Development and declared holdout screening only; no playing-strength/Elo promotion criterion",
        "native_before_external_audit": True, "parity": [], "cases": [], "errors": []}
    handler = logging.FileHandler(args.output_dir / "raw-uci.log", encoding="utf-8")
    logger = logging.getLogger("chess.engine")
    level = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    def checkpoint():
        write(args.output_dir / "report.json", report)
    try:
        parity_cases = [case for case in document["cases"] if case.get("parity")]
        if len(parity_cases) != 2:
            raise ValueError("Exactly two predeclared parity roots are required")
        for case in parity_cases:
            board = replay(case)
            baseline = query(args.baseline, board, chess.engine.Limit(nodes=args.nodes))
            candidate_off = query(args.candidate, board, chess.engine.Limit(nodes=args.nodes), args.candidate_option, False)
            row = {"case_id": case["id"], "baseline": baseline, "candidate_off": candidate_off,
                   "equivalent": baseline["execution_valid"] and candidate_off["execution_valid"]
                       and fingerprint(baseline) == fingerprint(candidate_off)}
            report["parity"].append(row)
            checkpoint()
        if not all(row["equivalent"] for row in report["parity"]):
            raise ValueError("Option-off parity failed; broad candidate screen was not run")
        if not args.parity_only:
            for index, case in enumerate(document["cases"]):
                board = replay(case)
                roots = [chess.Move.from_uci(uci) for uci in case["root_moves"]] if case.get("root_moves") else None
                row = {"fixture": case, "root_draw_state": draw_state(board), "queries": {}}
                report["cases"].append(row)
                for mode, limit in (("nodes", chess.engine.Limit(nodes=args.nodes)),
                                     ("time", chess.engine.Limit(time=args.movetime_ms / 1000))):
                    pair = {}
                    # Alternate arm order while preserving serial CPU use.
                    arms = [("baseline", args.baseline, None), ("candidate", args.candidate, args.candidate_option)]
                    for arm, binary, option in arms[::1 if index % 2 == 0 else -1]:
                        pair[arm] = query(binary, board, limit, option, True if option else None, roots)
                        if not pair[arm]["execution_valid"]:
                            report["errors"].append(f"{case['id']} {mode} {arm}: {pair[arm]['error']}")
                    pair["selected_move_changed"] = pair["baseline"]["selected_uci"] != pair["candidate"]["selected_uci"]
                    row["queries"][mode] = pair
                    checkpoint()
                print(json.dumps({"case": case["id"], "split": case["split"], "moves": {mode: {
                    arm: pair[arm]["selected_uci"] for arm in ("baseline", "candidate")}
                    for mode, pair in row["queries"].items()}}), flush=True)
            if args.host_smoke:
                report["host_smoke"] = {"baseline": host_smoke(args.baseline, args.output_dir / "host-baseline", None, None, args.host_ms),
                    "candidate": host_smoke(args.candidate, args.output_dir / "host-candidate", args.candidate_option, True, args.host_ms)}
                checkpoint()
            report["native_selection_finished_utc"] = utc()
            checkpoint()
            if args.stockfish:
                flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
                audit = chess.engine.SimpleEngine.popen_uci([str(args.stockfish)], timeout=10, **flags)
                try:
                    settings = {key: value for key, value in {"Threads": 1, "Hash": 32,
                        "UCI_LimitStrength": False, "Skill Level": 20}.items() if key in audit.options}
                    audit.configure(settings)
                    report["sf_audit_execution"] = {"engine_id": dict(audit.id), "configured_options": settings}
                    for row in report["cases"]:
                        moves = [pair[arm]["selected_uci"] for pair in row["queries"].values()
                                 for arm in ("baseline", "candidate") if pair[arm]["selected_uci"]]
                        if moves:
                            row["post_selection_sf_audit"] = sf_audit(audit, replay(row["fixture"]), moves)
                            checkpoint()
                finally:
                    try:
                        audit.quit()
                    except Exception as error:
                        report.setdefault("sf_audit_execution", {})["shutdown_error"] = type(error).__name__
                        audit.close()
                    try:
                        code = audit.returncode.result(timeout=3)
                        report.setdefault("sf_audit_execution", {}).update(engine_closed=True,
                            process_exit_code=code, clean_shutdown=type(code) is int and code == 0)
                    except Exception as error:
                        report.setdefault("sf_audit_execution", {}).update(engine_closed=False,
                            clean_shutdown=False, shutdown_error=type(error).__name__)
    except Exception as error:
        report["errors"].append(f"{type(error).__name__}: {error}")
    finally:
        report.update(finished_utc=utc(), sources_unchanged=all(sha(path) == value for path, value in hashes.items()))
        native_rows = [value for row in report["parity"] for value in (row["baseline"], row["candidate_off"])]
        native_rows += [pair[arm] for row in report["cases"] for pair in row["queries"].values()
                        for arm in ("baseline", "candidate")]
        expected = 4 + (0 if args.parity_only else 4 * len(document["cases"]))
        report["execution_valid"] = (len(native_rows) == expected and all(value["execution_valid"] for value in native_rows)
            and report["sources_unchanged"] and len(report["parity"]) == 2
            and all(row["equivalent"] for row in report["parity"]))
        report["host_policy_contracts_passed"] = (all(value["contracts_passed"] for value in report["host_smoke"].values())
                                                   if "host_smoke" in report else None)
        audit = report.get("sf_audit_execution")
        report["audit_execution_valid"] = (audit.get("clean_shutdown") is True and all(
            row.get("post_selection_sf_audit", {}).get("status") == "common_exact_depth"
            for row in report["cases"] if any(pair[arm]["selected_uci"]
                for pair in row["queries"].values() for arm in ("baseline", "candidate")))
            if audit is not None else False if args.stockfish and not args.parity_only else None)
        report["promotion_decision"] = "not made by this tool"
        checkpoint()
        logger.removeHandler(handler)
        logger.setLevel(level)
        handler.close()
    return 0 if (report["execution_valid"] and report["host_policy_contracts_passed"] is not False
                 and report["audit_execution_valid"] is not False) else 1


if __name__ == "__main__":
    raise SystemExit(main())
