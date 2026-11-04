"""Serial native perpetual-check diagnostics, with no external move selector.

The actual-game roots and authored synthetic positions are development cases.
Both own engines select every reported move. Changed choices or own scores
are observations, never a general improvement, calibrated Elo or exact-draw
claim. Claimable roots are searched rather than pre-adjudicated by this tool.
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
import threading
import time

import chess
import chess.engine

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/data/repetition_cycle_development_2026_11_04.json"
GAME_ROOT = "r4k2/2p5/1pqp4/5r2/p3R2R/2B3P1/PP3P1P/6K1 b - - 3 31"
LATE_HISTORY = "a4a3 h4h8 f8f7 h8h7 f7f8 h7h8 f8f7 h8h7".split()


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def safe(value):
    if isinstance(value, chess.Move):
        return value.uci()
    if isinstance(value, chess.engine.PovScore):
        return {"turn": "white" if value.turn else "black", "cp_white": value.white().score(),
                "mate_white": value.white().mate()}
    if isinstance(value, chess.engine.PovWdl):
        return {"turn": "white" if value.turn else "black", "white": list(value.white())}
    if isinstance(value, dict):
        return {str(key): safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe(item) for item in value]
    return value if value is None or isinstance(value, (str, int, float, bool)) else repr(value)


def replay(case):
    board = chess.Board(case["initial_fen"])
    if not board.is_valid():
        raise ValueError("Invalid declared root: " + case["id"])
    for uci in case["history_uci"]:
        move = chess.Move.from_uci(uci)
        if move not in board.legal_moves or board.outcome(claim_draw=False) is not None:
            raise ValueError("Illegal or post-terminal declared history: " + case["id"])
        board.push(move)
    return board


def mirrored(case):
    history = []
    for uci in case["history_uci"]:
        move = chess.Move.from_uci(uci)
        history.append(chess.Move(chess.square_mirror(move.from_square), chess.square_mirror(move.to_square),
                                  promotion=move.promotion).uci())
    return {**case, "id": case["id"] + "-color-mirror",
            "initial_fen": chess.Board(case["initial_fen"]).mirror().fen(en_passant="fen"),
            "history_uci": history, "color_mirror": True}


def mirror_uci(uci):
    move = chess.Move.from_uci(uci)
    return chess.Move(chess.square_mirror(move.from_square), chess.square_mirror(move.to_square),
                      promotion=move.promotion).uci()


def rule_assertions(case, fixture=None):
    board = replay(case)
    before = board.fen(en_passant="fen"), list(board.move_stack)
    checks = {"legal_history": True, "fen_matches_declared": board.fen(en_passant="fen") == case["before_fen"]}
    result = {"checks": checks, "oracle_mate_in_one_uci": None}
    if fixture and "all_mate_in_one_moves_uci" in fixture:
        actual = []
        for move in list(board.legal_moves):
            board.push(move)
            if board.is_checkmate():
                actual.append(move.uci())
            board.pop()
        expected = fixture["all_mate_in_one_moves_uci"]
        if case.get("color_mirror"):
            expected = [mirror_uci(uci) for uci in expected]
        checks["independent_mate_one_set_matches"] = sorted(actual) == sorted(expected)
        checks["intended_claim_available_but_not_completed_threefold"] = (
            board.can_claim_threefold_repetition() and not board.is_repetition(3))
        result["oracle_mate_in_one_uci"] = sorted(actual)
    if fixture and "declared_strategy_uci" in fixture:
        strategy = fixture["declared_strategy_uci"]
        if case.get("color_mirror"):
            strategy = [mirror_uci(uci) for uci in strategy]
        line = board.copy(stack=True)
        evasions, unique = 0, True
        for uci in strategy * fixture["repeat_strategy_times"]:
            if line.is_check():
                evasions += 1
                unique = unique and [move.uci() for move in line.legal_moves] == [uci]
            line.push_uci(uci)
        checks.update(all_authored_check_evasions_forced=unique and evasions == 4,
                      authored_strategy_reaches_completed_third=line.is_repetition(3))
    checks["oracle_preserves_root_and_history"] = before == (board.fen(en_passant="fen"), list(board.move_stack))
    if not all(checks.values()):
        raise ValueError("Independent declared-rule assertion failed: " + case["id"])
    return result


def declared_cases(fixtures):
    original = [
        {"id": "actual-before-a3", "initial_fen": GAME_ROOT, "history_uci": [],
         "source": "User-game development root, before the a-pawn advance; no expected move supplied"},
        {"id": "actual-late-repetition-resource", "initial_fen": GAME_ROOT, "history_uci": LATE_HISTORY,
         "source": "User-game legal continuation; available repetition is recorded, not forbidden"}]
    for fixture in fixtures["cases"]:
        original.append({"id": fixture["id"], "initial_fen": fixture["initial_fen"],
                         "history_uci": fixture["history_uci"], "source": "Project-authored independent synthetic fixture"})
    cases = []
    for case in original:
        cases.extend([case, mirrored(case)])
    cases.append({"id": "standard-start-control", "initial_fen": chess.STARTING_FEN, "history_uci": [],
                  "source": "Standard initial position control"})
    for case in cases:
        board = replay(case)
        case["before_fen"] = board.fen(en_passant="fen")
        source_id = case["id"].removesuffix("-color-mirror")
        fixture = next((row for row in fixtures["cases"] if row["id"] == source_id), None)
        case["rule_assertions"] = rule_assertions(case, fixture)
    return cases


def draw_state(board):
    outcome = board.outcome(claim_draw=False)
    return {"current_threefold": board.is_repetition(3), "can_claim_threefold": board.can_claim_threefold_repetition(),
            "current_fifty_moves": board.is_fifty_moves(), "can_claim_fifty_moves": board.can_claim_fifty_moves(),
            "repetition_twofold": board.is_repetition(2), "halfmove_clock": board.halfmove_clock,
            "compulsory_result": outcome.result() if outcome else None,
            "compulsory_termination": outcome.termination.name.lower() if outcome else None}


def legal_line(board, packet, required=None):
    depth, score, pv = packet.get("depth"), packet.get("score"), packet.get("pv")
    if (type(depth) is not int or depth < 1 or not isinstance(score, chess.engine.PovScore)
            or score.turn != board.turn or packet.get("multipv", 1) != 1
            or packet.get("lowerbound") or packet.get("upperbound")
            or not isinstance(pv, (list, tuple)) or not pv or len(pv) > 128
            or required is not None and pv[0] != required):
        return None
    line = board.copy(stack=True)
    for move in pv:
        if (not isinstance(move, chess.Move) or line.outcome(claim_draw=False) is not None
                or move not in line.legal_moves):
            return None
        line.push(move)
    value = score.pov(board.turn)
    return {"depth": depth, "cp_root": value.score(), "mate_root": value.mate(),
            "pv_uci": [move.uci() for move in pv], "pv_end_fen": line.fen(en_passant="fen"),
            "nodes": packet.get("nodes"), "qnodes": packet.get("qnodes"), "seldepth": packet.get("seldepth"),
            "engine_time_seconds": packet.get("time"), "packet": safe(packet)}


def native_query(binary, board, milliseconds, candidate_option=None):
    original = board.fen(en_passant="fen"), list(board.move_stack)
    result = {"binary": str(binary), "requested_movetime_ms": milliseconds, "candidate_option": candidate_option,
              "status": "failed", "selected_uci": None, "packets": [], "error": None,
              "engine_closed": False, "process_exit_code": None}
    engine = watchdog = None
    expired = threading.Event()
    started = time.monotonic()
    try:
        flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        engine = chess.engine.SimpleEngine.popen_uci([str(binary), "uci"], timeout=10, **flags)
        def expire():
            expired.set()
            engine.close()
        watchdog = threading.Timer(milliseconds / 1000 + 3, expire)
        watchdog.daemon = True
        watchdog.start()
        result["engine_id"] = dict(engine.id)
        result["advertised_options"] = {name: {"type": option.type, "default": option.default,
            "min": option.min, "max": option.max} for name, option in engine.options.items()}
        if "UsePieceActivity" not in engine.options:
            raise ValueError("UsePieceActivity is required for the declared comparison")
        options = {"UsePieceActivity": True}
        common = {"Hash": 16, "Threads": 1, "UsePositionalEvaluation": True,
                  "UseNeuralEvaluation": False, "UseNNUEEvaluation": False}
        options.update({name: value for name, value in common.items() if name in engine.options})
        if candidate_option:
            if candidate_option not in engine.options or engine.options[candidate_option].type != "check":
                raise ValueError("Candidate must advertise the requested check option")
            options[candidate_option] = True
        for name, value in options.items():
            engine.options[name].parse(value)
        engine.configure(options)
        engine.ping()
        result["configured_options"] = options
        completed = []
        search_started = time.monotonic()
        with engine.analysis(board.copy(stack=True), chess.engine.Limit(time=milliseconds / 1000),
                             game=object(), info=chess.engine.INFO_ALL) as stream:
            for packet in stream:
                result["packets"].append(safe(packet))
                line = legal_line(board, packet)
                if line:
                    completed.append(line)
            answer = stream.wait()
        result["search_elapsed_ms"] = round((time.monotonic() - search_started) * 1000, 3)
        if answer.move not in board.legal_moves:
            raise ValueError("Native returned no legal move")
        result["selected_uci"], result["selected_san"] = answer.move.uci(), board.san(answer.move)
        matching = [line for line in completed if line["pv_uci"][0] == answer.move.uci()]
        result["completed_line"] = max(matching, key=lambda line: line["depth"]) if matching else None
        result["evidence_status"] = "completed_exact" if matching else "legal_move_without_completed_exact_line"
        # The selected line is an actual completed packet, not a fabricated
        # combination of depth/score/PV from different iterative updates.
        result["selected_pv_legal"] = bool(matching)
        result["pv_legal"] = bool(matching)
        after = board.copy(stack=True)
        after.push(answer.move)
        result["after_fen"], result["after_draw_state"] = after.fen(en_passant="fen"), draw_state(after)
        if not matching:
            raise ValueError("No completed exact legal PV packet for the returned root")
        result["status"] = "recorded"
    except Exception as error:
        result["error"] = f"{type(error).__name__}: {error}"
    finally:
        if watchdog is not None:
            watchdog.cancel()
            watchdog.join()
        if engine is not None:
            try:
                engine.quit()
            except Exception as error:
                result["close_error"] = f"{type(error).__name__}: {error}"
                engine.close()
            try:
                result["process_exit_code"] = engine.returncode.result(timeout=3)
                result["engine_closed"] = True
            except Exception as error:
                result["close_error"] = f"{type(error).__name__}: {error}"
        result["safety_timeout_expired"] = expired.is_set()
        result["position_unchanged"] = original == (board.fen(en_passant="fen"), list(board.move_stack))
        result["elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
        if (expired.is_set() or not result["engine_closed"] or type(result["process_exit_code"]) is not int
                or result["process_exit_code"] != 0 or not result["position_unchanged"]):
            result["status"] = "failed"
            result["error"] = result["error"] or "Timeout, process exit or board-preservation check failed"
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, help="Optional comparison binary; defaults to baseline")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--movetime-ms", type=int, default=2085)
    parser.add_argument("--candidate-option", help="Candidate-only advertised check option to set true")
    args = parser.parse_args(argv)
    if args.movetime_ms < 1:
        parser.error("movetime-ms must be positive")
    compare = args.candidate is not None or args.candidate_option is not None
    try:
        args.baseline = args.baseline.resolve(strict=True)
        args.candidate = (args.candidate or args.baseline).resolve(strict=True)
        if not args.baseline.is_file() or not args.candidate.is_file():
            raise ValueError("Baseline and candidate must be executable files")
        args.output = args.output.resolve()
        raw_log = args.output.with_suffix(".uci.log")
        if args.output.exists() or raw_log.exists():
            raise ValueError("Use fresh report and raw-log paths")
        cases = declared_cases(json.loads(FIXTURES.read_text(encoding="utf-8")))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        reserved = args.output.open("x", encoding="utf-8", newline="\n")
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.error(str(error))
    paths = [Path(__file__).resolve(), FIXTURES.resolve(), args.baseline, args.candidate]
    hashes = {str(path): sha(path) for path in paths}
    report = {"schema": "regicide.perpetual-native-diagnostic.v1", "started_utc": datetime.now(timezone.utc).isoformat(),
        "source_sha256": hashes, "python": platform.python_version(), "python_chess": chess.__version__,
        "platform": platform.platform(), "processor": platform.processor(), "logical_cpus": os.cpu_count(),
        "config": {"baseline": str(args.baseline), "candidate": str(args.candidate),
            "comparison_enabled": compare,
            "candidate_option": args.candidate_option, "movetime_ms": args.movetime_ms,
            "execution": "Sequential fresh native processes; baseline" + (" then candidate" if compare else " only") + " for each root; no external selector",
            "evaluation": "classical with UsePieceActivity=true; optional extra common options recorded per query"},
        "limitations": ["Declared development roots, not an unseen strength or equal-CPU benchmark",
            "Changed native move/score is an observation, not a proof of improvement or an exact draw",
            "Timed searches may vary; all legal history, options, raw UCI and packets remain recorded",
            "Draw resources are not prohibited; only compulsory endings are pre-recorded, never pre-selected"],
        "cases": [], "errors": [], "rule_regressions": []}
    def checkpoint():
        reserved.seek(0)
        reserved.write(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
        reserved.truncate()
        reserved.flush()
    handler = logging.FileHandler(raw_log, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger = logging.getLogger("chess.engine")
    prior_level = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    checkpoint()
    arms = [("baseline", args.baseline, None)]
    if compare:
        arms.append(("candidate", args.candidate, args.candidate_option))
    try:
        for case in cases:
            board = replay(case)
            entry = {"case": case, "root_draw_state": draw_state(board), "queries": {}}
            report["cases"].append(entry)
            for arm, binary, option in arms:
                logger.info("BEGIN case=%s arm=%s", case["id"], arm)
                query = native_query(binary, board, args.movetime_ms, option)
                entry["queries"][arm] = query
                if query["status"] != "recorded":
                    report["errors"].append(f"{case['id']} {arm}: {query['error']}")
                oracle_moves = case["rule_assertions"]["oracle_mate_in_one_uci"]
                if oracle_moves is not None:
                    query["independent_mate_one_oracle_match"] = query["selected_uci"] in oracle_moves
                    if not query["independent_mate_one_oracle_match"]:
                        report["rule_regressions"].append(f"{case['id']} {arm}: available mate in one was not selected")
                checkpoint()
            entry["selected_move_changed"] = ((entry["queries"]["baseline"]["selected_uci"] !=
                                               entry["queries"]["candidate"]["selected_uci"]) if compare else None)
            print(json.dumps({"case": case["id"], "selected": {arm: value["selected_uci"]
                for arm, value in entry["queries"].items()}, "evidence_status": {
                arm: value.get("evidence_status") for arm, value in entry["queries"].items()}}), flush=True)
    except Exception as error:
        report["errors"].append(f"{type(error).__name__}: {error}")
    finally:
        report.update(finished_utc=datetime.now(timezone.utc).isoformat(),
                      sources_unchanged=all(sha(path) == value for path, value in hashes.items()))
        queries = [query for case in report["cases"] for query in case["queries"].values()]
        report["queries_recorded"] = len(queries)
        report["all_owned_engines_closed"] = all(query["engine_closed"] for query in queries)
        report["execution_valid"] = (len(queries) == len(arms) * len(cases) and not report["errors"]
            and report["sources_unchanged"] and all(query["status"] == "recorded" for query in queries))
        report["independent_rule_checks_passed"] = not report["rule_regressions"]
        report["improvement_claim"] = "none; inspect actual moves and independent evidence before any promotion"
        checkpoint()
        logger.removeHandler(handler)
        logger.setLevel(prior_level)
        handler.close()
        reserved.close()
    return 0 if report["execution_valid"] and report["independent_rule_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
