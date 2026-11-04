"""Same-root candidate research mode for probe_loss_search.py.

Fresh own-engine UCI searches compare LMR on/off with classical activity
evaluation. Static configuration observations are supplied positions, never
asserted searched leaves or an additive evaluation decomposition. No match,
external answer engine, production policy or active binary is changed.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import platform
import subprocess
import threading
import time

import chess
import chess.engine

import probe_loss_search as legacy

ROOT = Path(__file__).resolve().parents[1]


def utc():
    return datetime.now(timezone.utc).isoformat()


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


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def validate_manifest(document):
    if not isinstance(document, dict) or not isinstance(document.get("cases"), list) or not document["cases"]:
        raise ValueError("Manifest requires a nonempty cases list")
    seen = set()
    for case in document["cases"]:
        if not isinstance(case, dict) or not isinstance(case.get("id"), str) or not case["id"] or case["id"] in seen:
            raise ValueError("Case ids must be nonempty unique strings")
        seen.add(case["id"])
        board = legacy.replay_position(case["initial_fen"], case["history_uci"], case["before_fen"])
        if board.outcome(claim_draw=False) is not None:
            raise ValueError("Candidate comparison requires a nonterminal root")
        candidates = case.get("candidates")
        if (not isinstance(candidates, list) or not candidates or any(not isinstance(value, str) for value in candidates)
                or len(set(candidates)) != len(candidates)):
            raise ValueError("Candidates must be nonempty distinct UCI strings")
        for uci in candidates:
            if chess.Move.from_uci(uci) not in board.legal_moves:
                raise ValueError("Illegal candidate: " + uci)
    return document["cases"]


def validate_packet(board, packet, root_move=None, node_cap=None):
    score, depth, pv = packet.get("score"), packet.get("depth"), packet.get("pv")
    if (not isinstance(score, chess.engine.PovScore) or score.turn != board.turn
            or type(depth) is not int or depth < 1 or packet.get("multipv", 1) != 1
            or packet.get("lowerbound") or packet.get("upperbound")
            or not isinstance(pv, (list, tuple)) or not pv
            or (root_move is not None and pv[0] != root_move)):
        return None
    nodes = packet.get("nodes")
    if type(nodes) is not int or nodes < 0 or (node_cap is not None and nodes > node_cap):
        return None
    replay = board.copy(stack=True)
    for move in pv:
        if not isinstance(move, chess.Move) or replay.outcome(claim_draw=False) is not None or move not in replay.legal_moves:
            return None
        replay.push(move)
    value = score.pov(board.turn)
    return {"depth": depth, "cp_root": value.score(), "mate_root": value.mate(),
            "nodes": nodes, "pv_uci": [move.uci() for move in pv],
            "pv_end_fen": replay.fen(en_passant="fen"), "packet": safe(packet)}


def configure(engine, lmr):
    required = {"Hash", "UseLMR", "UsePositionalEvaluation", "UsePieceActivity",
                "UseNeuralEvaluation", "UseNNUEEvaluation", "PVS"}
    if not required.issubset(engine.options):
        raise ValueError("Required engine options missing: " + ", ".join(sorted(required - set(engine.options))))
    for name in required - {"Hash"}:
        if engine.options[name].type != "check":
            raise ValueError("Expected a check option: " + name)
    options = {"Hash": 16, "UseLMR": lmr, "UsePositionalEvaluation": True, "UsePieceActivity": True,
               "UseNeuralEvaluation": False, "UseNNUEEvaluation": False, "PVS": True}
    common = {"Threads": 1, "UseQuiescence": True, "UseTranspositionTable": True,
              "UseFastMoves": True, "UseQuietMoveOrdering": True, "UseQuiescenceChecks": True,
              "UseSEE": True, "UsePawnShield": True, "UsePawnStructure": True,
              "UseQuiescenceThreats": False, "UseQuiescenceThreatCache": False,
              "UseTacticalThreatOrdering": False, "UseQuiescenceReplyBudget": False}
    options.update({name: value for name, value in common.items() if name in engine.options})
    for name, value in options.items():
        engine.options[name].parse(value)
    engine.configure(options)
    engine.ping()
    return options


def open_engine(binary, timeout):
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    return chess.engine.SimpleEngine.popen_uci([str(binary), "uci"], timeout=timeout, **flags)


def query(binary, board, root_uci, depth, nodes, timeout, lmr, factory=open_engine):
    before = board.fen(en_passant="fen"), list(board.move_stack)
    result = {"root_uci": root_uci, "requested_depth": depth, "node_cap": nodes,
              "lmr": lmr, "fresh_process": True, "status": "failed", "packets": [],
              "exact_by_depth": {}, "returned_uci": None, "error": None, "engine_closed": False,
              "process_exit_code": None}
    engine = None
    watchdog = None
    timed_out = threading.Event()
    started = time.monotonic()
    try:
        engine = factory(binary, timeout)
        def expire():
            timed_out.set()
            engine.close()
        watchdog = threading.Timer(timeout, expire)
        watchdog.daemon = True
        watchdog.start()
        result["engine_id"] = dict(engine.id)
        result["advertised_options"] = {name: {"type": value.type, "default": value.default,
            "min": value.min, "max": value.max} for name, value in engine.options.items()}
        result["configured_options"] = configure(engine, lmr)
        move = chess.Move.from_uci(root_uci) if root_uci else None
        if move is not None and move not in board.legal_moves:
            raise ValueError("Illegal constrained root move")
        with engine.analysis(board.copy(stack=True), chess.engine.Limit(depth=depth, nodes=nodes),
                             root_moves=[move] if move else None, game=object(), info=chess.engine.INFO_ALL) as stream:
            for packet in stream:
                result["packets"].append(safe(packet))
                if type(packet.get("nodes")) is int and packet["nodes"] > nodes:
                    raise ValueError("Native query exceeded its node cap")
                exact = validate_packet(board, packet, move, nodes)
                if exact is not None and exact["depth"] <= depth:
                    result["exact_by_depth"][exact["depth"]] = exact
            answer = stream.wait()
        returned = answer.move
        if returned not in board.legal_moves or (move is not None and returned != move):
            raise ValueError("Returned root is illegal or disagrees with constrained candidate")
        result["returned_uci"] = returned.uci()
        line = result["exact_by_depth"].get(depth)
        if line is None or line["pv_uci"][0] != returned.uci():
            result["status"] = "incomplete_requested_depth"
            result["error"] = "No exact legal returned-root PV at requested depth"
        else:
            result.update(status="complete", completed_line=line)
    except Exception as error:
        result["error"] = "Safety timeout expired" if timed_out.is_set() else f"{type(error).__name__}: {error}"
    finally:
        if watchdog is not None:
            watchdog.cancel()
            # cancel() alone cannot stop a callback that has already started.
            # Join before checking its event or recording successful completion.
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
        result["position_unchanged"] = before == (board.fen(en_passant="fen"), list(board.move_stack))
        result["elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
        result["safety_timeout_expired"] = timed_out.is_set()
        if result["safety_timeout_expired"]:
            result.update(status="failed", error="Safety timeout expired")
        if result["engine_closed"] and (type(result["process_exit_code"]) is not int or result["process_exit_code"] != 0):
            result["status"] = "failed"
            message = f"Native process exited abnormally: {result['process_exit_code']}"
            result["error"] = message if not result["error"] else result["error"] + "; " + message
    return result


def query_passed(row):
    return (row["status"] == "complete" and row.get("safety_timeout_expired") is False
            and row.get("engine_closed") is True and row.get("position_unchanged") is True
            and type(row.get("process_exit_code")) is int and row["process_exit_code"] == 0)


def compare_depth(queries, depth):
    group = [row for row in queries if row["requested_depth"] == depth]
    common = set.intersection(*(set(row["exact_by_depth"]) for row in group)) if group else set()
    complete = bool(group) and all(query_passed(row) for row in group) and depth in common
    return {"requested_depth": depth, "all_completed": complete,
            "common_exact_depth": depth if complete else max(common) if common else None,
            "values": [{"lmr": row["lmr"], "root_uci": row["root_uci"],
                "score": row["exact_by_depth"].get(depth) if complete else None} for row in group],
            "interpretation": "Same requested and completed depth; selective search scores are not exact minimax"}


def static_observations(probe, case, queries, directory, timeout):
    requests = [{"kind": "root", "continuation": []}]
    requests.extend({"kind": "candidate_after", "candidate_uci": move, "continuation": [move]}
                    for move in case["candidates"])
    for row in queries:
        line = row.get("completed_line")
        if line:
            requests.append({"kind": "pv_endpoint", "candidate_uci": row["root_uci"],
                "lmr": row["lmr"], "depth": row["requested_depth"], "continuation": line["pv_uci"]})
    boards, lines = [], []
    for request in requests:
        history = [*case["history_uci"], *request["continuation"]]
        boards.append(legacy.replay_position(case["initial_fen"], history))
        lines.append(case["initial_fen"] + "\t" + " ".join(history))
    input_text = "\n".join(lines) + "\n"
    (directory / "static-stdin.tsv").write_text(input_text, encoding="utf-8")
    result = {"status": "failed", "command": [str(probe), "--piece-activity"],
              "searched_leaf_claim": False, "error": None,
              "interpretation": "Supplied static positions; option differences are not additive or causal search explanations"}
    try:
        flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        response = subprocess.run(result["command"], input=input_text, capture_output=True, text=True,
                                  encoding="utf-8", timeout=timeout, check=False, **flags)
        result.update(returncode=response.returncode, stdout=response.stdout, stderr=response.stderr)
        if response.returncode:
            raise ValueError("Static probe failed")
        values = legacy.parse_static_probe(response.stdout, boards)
        if any(field not in value for value in values for field in legacy.ACTIVITY_SCORE_FIELDS):
            raise ValueError("Activity probe requires 17 columns")
        root_turn = boards[0].turn
        result["observations"] = [{**request, "static": value,
            "root_pov_scores": {name: (1 if board.turn == root_turn else -1) * value[name]
                for name in (*legacy.STATIC_SCORE_FIELDS, *legacy.ACTIVITY_SCORE_FIELDS)}}
            for request, board, value in zip(requests, boards, values)]
        result["status"] = "complete"
    except Exception as error:
        result["error"] = f"{type(error).__name__}: {error}"
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root-candidates", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--depths", nargs="+", type=int, default=[4, 5, 6])
    parser.add_argument("--nodes", type=int, default=5_000_000)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--probe", type=Path)
    args = parser.parse_args(argv)
    if (not args.depths or any(value < 1 or value > 64 for value in args.depths)
            or len(set(args.depths)) != len(args.depths) or args.nodes <= 0
            or not math.isfinite(args.timeout) or args.timeout <= 0):
        parser.error("Invalid depth, node or timeout limits")
    try:
        for name in ("root_candidates", "binary", "probe"):
            path = getattr(args, name)
            if path is not None:
                setattr(args, name, path.resolve(strict=True))
        document = json.loads(args.root_candidates.read_text(encoding="utf-8"))
        cases = validate_manifest(document)
        args.output_dir.mkdir(parents=True, exist_ok=False)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.error(str(error))
    paths = [Path(__file__), Path(legacy.__file__), args.root_candidates, args.binary]
    if args.probe:
        paths.append(args.probe)
    hashes = {str(path): legacy.digest(path) for path in paths}
    (args.output_dir / "input-manifest.json").write_bytes(args.root_candidates.read_bytes())
    handler = logging.FileHandler(args.output_dir / "raw-uci.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger = logging.getLogger("chess.engine")
    old_level = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    report = {"schema": "regicide.root-candidate-probe.v1", "started_utc": utc(), "source_sha256": hashes,
        "python": platform.python_version(), "python_chess": chess.__version__, "platform": platform.platform(),
        "processor": platform.processor(), "logical_cpus": os.cpu_count(),
        "config": {"depths": args.depths, "node_cap": args.nodes, "timeout_seconds": args.timeout,
                   "classical_piece_activity": True, "lmr_variants": [True, False],
                   "fresh": "New process per candidate/free/depth/LMR query"},
        "scope": "Research diagnosis, no match, Elo, speed or strength claim; input sample is caller-declared",
        "cases": [], "errors": []}
    try:
        for index, case in enumerate(cases, 1):
            directory = args.output_dir / f"case-{index:03d}"
            directory.mkdir()
            row = {"case": case, "queries": [], "comparisons": []}
            report["cases"].append(row)
            board = legacy.replay_position(case["initial_fen"], case["history_uci"], case["before_fen"])
            for depth in args.depths:
                for lmr in (True, False):
                    for root in [None, *case["candidates"]]:
                        answer = query(args.binary, board, root, depth, args.nodes, args.timeout, lmr)
                        row["queries"].append(answer)
                        write(directory / f"query-{len(row['queries']):03d}.json", answer)
                        if not query_passed(answer):
                            report["errors"].append(f"{case['id']} depth={depth} LMR={lmr} root={root}: {answer['error']}")
                        write(args.output_dir / "report.json", report)
                row["comparisons"].append(compare_depth(row["queries"], depth))
            if args.probe:
                row["static"] = static_observations(args.probe, case, row["queries"], directory, args.timeout)
                if row["static"]["status"] != "complete":
                    report["errors"].append(f"{case['id']} static: {row['static']['error']}")
            print(json.dumps({"case": case["id"], "queries": len(row["queries"]),
                "completed_depths": [item["requested_depth"] for item in row["comparisons"] if item["all_completed"]]}), flush=True)
    except Exception as error:
        report["errors"].append(f"{type(error).__name__}: {error}")
    finally:
        report.update(finished_utc=utc(), sources_unchanged=all(legacy.digest(path) == value for path, value in hashes.items()))
        report["all_owned_engines_closed"] = all(query["engine_closed"] for case in report["cases"] for query in case["queries"])
        report["passed"] = (len(report["cases"]) == len(cases) and not report["errors"] and report["sources_unchanged"]
            and report["all_owned_engines_closed"] and all(len(case["comparisons"]) == len(args.depths)
                and all(query_passed(query) for query in case["queries"])
                and all(item["all_completed"] for item in case["comparisons"]) for case in report["cases"]))
        write(args.output_dir / "report.json", report)
        logger.removeHandler(handler)
        logger.setLevel(old_level)
        handler.close()
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
