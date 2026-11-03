"""Same-parent audit of EVERY changed Regicide proposal in the first 40 plies.

Select own, non-forced moves where actual UCI differs from
decision.regicide_proposal.selected_uci, regardless of result or referee status.
One independent full-strength Stockfish MultiPV search evaluates both roots at
the same completed depth. This diagnoses a same-position choice effect, not
whole-game causality, an infallible answer, independent Regicide Elo or strength.
"""

import argparse
from collections import Counter
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time

import chess
import chess.engine

ROOT = Path(__file__).resolve().parents[1]
PIPELINE = "regicide.sparring-counterfactual.v1"
WINDOW_PLIES = 40
MIN_DEPTH = 16


def utc():
    return datetime.now(timezone.utc).isoformat()


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def final_sha(path):
    try:
        return sha(path)
    except OSError:
        return None


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")


def input_files(paths):
    discovered = []
    for value in paths:
        path = Path(value).expanduser().resolve(strict=True)
        if path.is_file():
            discovered.append(path)
        elif path.is_dir():
            discovered.extend(sorted(candidate.resolve() for candidate in path.rglob("*.json")
                                     if re.fullmatch(r"game-\d+\.json", candidate.name)))
        else:
            raise ValueError("Input is not a game JSON file or directory")
    return list(dict.fromkeys(discovered))


def is_target(row):
    if (not isinstance(row, dict) or type(row.get("ply")) is not int or not 1 <= row["ply"] <= WINDOW_PLIES
            or row.get("actor") != "regicide" or row.get("forced_opening") is True):
        return False
    decision = row.get("decision")
    proposal = decision.get("regicide_proposal") if isinstance(decision, dict) else None
    original = proposal.get("selected_uci") if isinstance(proposal, dict) else None
    return isinstance(original, str) and isinstance(row.get("uci"), str) and row["uci"] != original


def collect_targets(paths):
    inputs, cases, errors = [], [], []
    for path in input_files(paths):
        digest = sha(path)
        item = {"path": str(path), "sha256_before": digest, "selected_cases": 0}
        inputs.append(item)
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(record, dict) or not isinstance(record.get("moves"), list):
                raise ValueError("Not a comparison-driver game record")
            rows = record["moves"]
        except (OSError, ValueError, TypeError) as error:
            errors.append({"input": str(path), "stage": "input", "type": type(error).__name__, "message": str(error)})
            continue
        parents, replay_error = {}, None
        try:
            board = chess.Board(record.get("initial_fen", chess.STARTING_FEN))
            if not board.is_valid() or board.chess960:
                raise ValueError("Invalid initial standard-chess board")
            for index, row in enumerate(rows, 1):
                if not isinstance(row, dict) or row.get("ply") != index:
                    raise ValueError("Nonconsecutive move identity")
                if row.get("before_fen") != board.fen(en_passant="fen"):
                    raise ValueError("Parent FEN differs from complete-history replay")
                parents[index] = board.copy(stack=True)
                move = chess.Move.from_uci(row["uci"])
                if move not in board.legal_moves or row.get("san") != board.san(move):
                    raise ValueError("Illegal move or mismatched SAN")
                board.push(move)
                if row.get("after_fen") != board.fen(en_passant="fen"):
                    raise ValueError("Post-move FEN differs from replay")
            if record.get("uci_moves") != [row["uci"] for row in rows] or record.get("final_fen") != board.fen(en_passant="fen"):
                raise ValueError("Final record and full history disagree")
        except (ValueError, KeyError, TypeError, IndexError) as error:
            replay_error = str(error)
            errors.append({"input": str(path), "stage": "replay", "type": type(error).__name__, "message": replay_error})
        for row in rows:
            if not is_target(row):
                continue
            proposal = row["decision"]["regicide_proposal"]["selected_uci"]
            case = {"case": len(cases) + 1, "input_path": str(path), "input_sha256": digest,
                    "variant": record.get("variant"), "game": record.get("game"), "ply": row["ply"],
                    "sparring_color": record.get("sparring_color"), "opponent_elo": record.get("opponent_elo"),
                    "actual_uci": row["uci"], "native_proposal_uci": proposal,
                    "source_result": record.get("result"), "source_termination": record.get("termination"),
                    "source_game_errors": copy.deepcopy(record.get("errors", [])), "source_move_late": bool(row.get("late")),
                    "referee_reason": row["decision"].get("referee", {}).get("reason") if isinstance(row["decision"].get("referee"), dict) else None,
                    "status": "pending", "reason": None}
            parent = parents.get(row["ply"])
            if replay_error is not None:
                case.update(status="unknown", reason="input_history_validation_failed", input_error=replay_error)
            elif parent is None:
                case.update(status="unknown", reason="parent_history_unavailable")
            else:
                try:
                    actual, original = chess.Move.from_uci(row["uci"]), chess.Move.from_uci(proposal)
                    if actual not in parent.legal_moves or original not in parent.legal_moves:
                        raise ValueError("Actual or original proposal is not legal at this parent")
                    if parent.is_game_over(claim_draw=True):
                        raise ValueError("Parent is already terminal under local rules")
                    expected_color = "white" if parent.turn else "black"
                    if case["sparring_color"] != expected_color:
                        raise ValueError("Own move's color differs from full-history side to move")
                    case.update(parent_fen=parent.fen(en_passant="fen"),
                                root_fen=parent.root().fen(en_passant="fen"),
                                history_uci=[move.uci() for move in parent.move_stack])
                except (ValueError, TypeError):
                    case.update(status="unknown", reason="illegal_or_inconsistent_candidate")
            cases.append(case)
            item["selected_cases"] += 1
    return inputs, cases, errors


def packet(board, info, allowed):
    if (not isinstance(info, dict) or type(info.get("depth")) is not int or not 1 <= info["depth"] <= 256
            or info.get("lowerbound") or info.get("upperbound")
            or type(info.get("multipv")) is not int or info["multipv"] not in (1, 2)):
        return None
    score, pv = info.get("score"), info.get("pv")
    if (not isinstance(score, chess.engine.PovScore) or score.turn != board.turn
            or not isinstance(pv, (list, tuple)) or not 0 < len(pv) <= 256
            or not isinstance(pv[0], chess.Move) or pv[0] not in allowed):
        return None
    replay = board.copy(stack=True)
    for move in pv:
        if not isinstance(move, chess.Move) or move not in replay.legal_moves:
            return None
        replay.push(move)
    cp, mate = score.white().score(), score.white().mate()
    if cp is not None:
        if type(cp) is not int or abs(cp) > 100000 or mate is not None or replay.is_checkmate():
            return None
    elif type(mate) is not int or mate == 0 or abs(mate) > 10000:
        return None
    elif replay.is_checkmate() and replay.turn == (chess.WHITE if mate > 0 else chess.BLACK):
        return None
    return {"uci": pv[0].uci(), "depth": info["depth"], "multipv": info["multipv"], "bound": "exact",
            "score_pov": "white", "cp_white": cp, "mate_white": mate,
            "nodes": info.get("nodes") if type(info.get("nodes")) is int else None,
            "pv_uci": [move.uci() for move in pv]}


def compare_packets(actual, original):
    if actual["mate_white"] is not None or original["mate_white"] is not None:
        return {"status": "mate", "actual_mate_white": actual["mate_white"],
                "native_mate_white": original["mate_white"],
                "scope": "Mate scores are kept separate; no centipawn or closeness surrogate is assigned"}
    a, n = actual["cp_white"], original["cp_white"]
    absolute_delta = abs(a) - abs(n)
    actual_d, native_d = max(abs(a) - 100, 0), max(abs(n) - 100, 0)
    delta_d = actual_d - native_d
    effect = lambda delta: "improved" if delta < 0 else "worsened" if delta > 0 else "tied"
    return {"status": "finite", "actual_abs_cp": abs(a), "native_abs_cp": abs(n),
            "absolute_cp_delta_actual_minus_native": absolute_delta,
            "actual_within_100": abs(a) <= 100, "native_within_100": abs(n) <= 100,
            "actual_within_200": abs(a) <= 200, "native_within_200": abs(n) <= 200,
            "actual_D": actual_d, "native_D": native_d, "D_delta_actual_minus_native": delta_d,
            "effect_by_D": effect(delta_d), "effect_by_absolute_cp": effect(absolute_delta)}


def native_engine(binary):
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    return chess.engine.SimpleEngine.popen_uci([str(binary)], timeout=3, **flags)


def analyse_pair(engine, case, depth=16, timeout=20):
    board = chess.Board(case["root_fen"])
    for uci in case["history_uci"]:
        board.push_uci(uci)
    if board.fen(en_passant="fen") != case["parent_fen"]:
        raise ValueError("Rebuilt counterfactual parent differs")
    allowed = [chess.Move.from_uci(case["actual_uci"]), chess.Move.from_uci(case["native_proposal_uci"])]
    rounds, invalid = {}, set()
    expired = threading.Event()
    def stop():
        expired.set()
        engine.close()
    guard = threading.Timer(timeout, stop)
    guard.daemon = True
    started = time.monotonic()
    guard.start()
    try:
        if "Clear Hash" not in engine.options:
            raise ValueError("Independent Stockfish must expose Clear Hash")
        engine.configure({"Clear Hash": None})
        with engine.analysis(board.copy(stack=True), chess.engine.Limit(depth=depth),
                             root_moves=allowed, multipv=2, game=object(), info=chess.engine.INFO_ALL) as analysis:
            for info in analysis:
                value = packet(board, info, allowed)
                if value is None:
                    if isinstance(info, dict) and type(info.get("depth")) is int and "score" in info:
                        invalid.add(info["depth"])
                    continue
                rounds.setdefault(value["depth"], {})[value["uci"]] = value
                for old in sorted(rounds)[:-8]:
                    del rounds[old]
                invalid = {item for item in invalid if item >= max(rounds) - 8}
            returned = analysis.wait().move
        if expired.is_set():
            raise TimeoutError("Counterfactual comparison exceeded its wall limit")
        if returned not in allowed:
            raise ValueError("Stockfish returned a move outside the two supplied roots")
        required = {move.uci() for move in allowed}
        complete = [(level, rows) for level, rows in rounds.items() if level >= depth and level not in invalid
                    and required <= rows.keys() and {rows[key]["multipv"] for key in required} == {1, 2}]
        if not complete:
            return {"status": "unknown", "reason": "no_same_depth_exact_completed_pair",
                    "elapsed_ms": round((time.monotonic() - started) * 1000, 3)}
        level, rows = max(complete, key=lambda item: item[0])
        actual, original = rows[case["actual_uci"]], rows[case["native_proposal_uci"]]
        return {"status": "ready", "depth": level, "actual": actual, "native_original": original,
                "comparison": compare_packets(actual, original), "elapsed_ms": round((time.monotonic() - started) * 1000, 3)}
    except Exception as error:
        if expired.is_set():
            raise TimeoutError("Counterfactual comparison exceeded its wall limit") from error
        raise
    finally:
        guard.cancel()
        guard.join(.2)


def summarize(cases):
    finite = [case for case in cases if case.get("comparison", {}).get("status") == "finite"]
    mate = [case for case in cases if case.get("comparison", {}).get("status") == "mate"]
    counts = lambda key: {name: sum(case["comparison"].get(key) == name for case in finite)
                         for name in ("improved", "worsened", "tied")}
    return {"all_selected_cases": len(cases), "exact_finite_pairs": len(finite), "mate_pairs_separate": len(mate),
            "unknown_or_error_pairs": len(cases) - len(finite) - len(mate),
            "effect_by_D": counts("effect_by_D"), "effect_by_absolute_cp": counts("effect_by_absolute_cp"),
            "actual_within_100": sum(case["comparison"]["actual_within_100"] for case in finite),
            "native_within_100": sum(case["comparison"]["native_within_100"] for case in finite),
            "actual_within_200": sum(case["comparison"]["actual_within_200"] for case in finite),
            "native_within_200": sum(case["comparison"]["native_within_200"] for case in finite),
            "sum_D_delta_actual_minus_native": sum(case["comparison"]["D_delta_actual_minus_native"] for case in finite),
            "sum_abs_cp_delta_actual_minus_native": sum(case["comparison"]["absolute_cp_delta_actual_minus_native"] for case in finite),
            "D_improved_fraction_of_all_selected": sum(case["comparison"]["effect_by_D"] == "improved" for case in finite) / len(cases) if cases else None}


def run(inputs, binary, output, *, depth=16, timeout=20, factory=native_engine):
    if type(depth) is not int or not 16 <= depth <= 256 or timeout <= 0:
        raise ValueError("Use a fixed depth >=16 and a positive timeout")
    output, binary = Path(output).resolve(), Path(binary).resolve(strict=True)
    output.mkdir(parents=True, exist_ok=False)
    source_before, binary_before = sha(Path(__file__)), sha(binary)
    input_rows, cases, input_errors = collect_targets(inputs)
    try:
        from referee_engine import resolve_referee_engine
        provenance = resolve_referee_engine(ROOT, binary)
    except (ImportError, OSError):
        provenance = {"binary": str(binary), "sha256": binary_before, "verified_official": False}
    report = {"pipeline": PIPELINE, "started_at": utc(), "filter": "ply1..40;actor=regicide;forced_opening!=true;actual_uci!=decision.regicide_proposal.selected_uci;no outcome/referee-status filter",
              "inputs": input_rows, "input_errors": input_errors, "cases": cases,
              "script_sha256_before": source_before, "binary_sha256_before": binary_before,
              "engine_provenance": provenance, "engine_processes": [],
              "settings": {"depth": depth, "multipv": 2, "threads": 1, "hash_mb": 32,
                           "skill_level": 20, "limit_strength": False, "fresh_game_each_parent": True,
                           "clear_hash_each_parent": True, "timeout_seconds": timeout},
              "scope": "same-parent choice-effect diagnostic; not whole-game causal proof, an infallible engine answer, Elo or independent Regicide strength",
              "status": "running"}
    target = output / "audit.json"
    write_json(target, report)
    engine, owned = None, []
    try:
        for case in cases:
            if case["status"] != "pending":
                write_json(output / f"case-{case['case']:03d}.json", case)
                continue
            try:
                if engine is None:
                    engine = factory(binary)
                    details = {"id": copy.deepcopy(engine.id), "configured_options": {}, "exited": False}
                    try:
                        details["pid"] = engine.transport.get_pid()
                    except AttributeError:
                        details["pid"] = None
                    owned.append((engine, details))
                    report["engine_processes"].append(details)
                    name = engine.id.get("name", "")
                    if not isinstance(name, str) or "stockfish" not in name.lower():
                        raise ValueError("Selected engine did not identify as Stockfish")
                    settings = {"Threads": 1, "Hash": 32, "Skill Level": 20, "UCI_LimitStrength": False}
                    if any(key not in engine.options for key in (*settings, "Clear Hash")):
                        raise ValueError("Stockfish lacks required full-strength or hash-reset options")
                    engine.configure(settings)
                    details["configured_options"] = settings
                result = analyse_pair(engine, case, depth, timeout)
                case.update(result)
            except Exception as error:
                case.update(status="unknown", reason="reference_error", error_type=type(error).__name__, error=str(error))
                if engine is not None:
                    engine.close()
                    engine = None
            write_json(output / f"case-{case['case']:03d}.json", case)
            write_json(target, report)
            print(json.dumps({"event": "counterfactual", "case": case["case"], "status": case["status"],
                              "depth": case.get("depth"), "comparison": case.get("comparison")}), flush=True)
    finally:
        for peer, details in owned:
            try:
                peer.close()
                details["closed_by_this_audit"] = True
                try:
                    details["exit_code"] = peer.returncode.result(timeout=2)
                    details["exited"] = True
                except AttributeError:
                    details["exited"] = bool(getattr(peer, "closed", False))
            except Exception as error:
                details["cleanup_error"] = type(error).__name__
        for item in input_rows:
            item["sha256_after"] = final_sha(item["path"])
        report.update(finished_at=utc(), summary=summarize(cases), script_sha256_after=final_sha(Path(__file__)),
                      binary_sha256_after=final_sha(binary),
                      inputs_unchanged=all(item["sha256_before"] == item["sha256_after"] for item in input_rows))
        report["script_unchanged"] = report["script_sha256_after"] == source_before
        report["binary_unchanged"] = report["binary_sha256_after"] == binary_before
        report["all_owned_processes_exited"] = all(item["exited"] for item in report["engine_processes"])
        for case in cases:
            if case["status"] == "pending":
                case.update(status="unknown", reason="interrupted_before_evaluation")
        report["summary"] = summarize(cases)
        report["status"] = "completed" if (not input_errors and not report["summary"]["unknown_or_error_pairs"]
            and all(report[key] for key in ("inputs_unchanged", "script_unchanged", "binary_unchanged", "all_owned_processes_exited"))) else "completed_with_unknowns"
        write_json(target, report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, nargs="+", required=True)
    parser.add_argument("--stockfish", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--depth", type=int, default=16)
    parser.add_argument("--timeout", type=float, default=20)
    args = parser.parse_args(argv)
    report = run(args.inputs, args.stockfish, args.output, depth=args.depth, timeout=args.timeout)
    print(json.dumps({"status": report["status"], "summary": report["summary"]}), flush=True)
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
