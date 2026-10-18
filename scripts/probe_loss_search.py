"""Replay frozen loss positions with cold Regicide CLI searches and static probes.

This is a diagnostic, not a strength or wall-time benchmark. The screen selects
positions; this tool does not reselect them or tune the engine. Every subprocess
starts a new search context, replays the complete supplied history, and retains
its raw output. A reported PV endpoint is not asserted to be a searched leaf.

Usage: python scripts/probe_loss_search.py --screen SCREEN.json \
    --baseline V057 --candidate V058 --probe STATIC_PROBE --output NEW.json
"""

import argparse
import datetime
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time

import chess


BASELINE_COMMIT = "5b5b5deedbef96133836609ec00229c7a548a15a"
CANDIDATE_COMMIT = "157305d804bc1828641a7e8d9b9b793b7746bcc3"
FIXED_NODE_BUDGETS = (250_000, 1_000_000, 3_000_000)
DEPTH_QUERY = {"id": "depth-6-nodes-5000000", "depth": 6, "nodes": 5_000_000,
               "reasons": ["separate_predeclared_fixed_depth_control"]}
VARIANTS = ("v057", "v058_off", "v058_on")
STATIC_SCORE_FIELDS = (
    "material_cp", "eval_shield_on_structure_on_cp", "eval_shield_on_structure_off_cp",
    "eval_shield_off_structure_on_cp", "eval_shield_off_structure_off_cp")
STATIC_FIELDS = ("fen", "turn", *STATIC_SCORE_FIELDS, "legal_count", "in_check",
                 "claim_threefold_now", "claim_fifty_moves_now", "outcome", "repetitions")
SEARCH_REQUIRED_NUMBERS = (
    "depth", "nodes", "time_ms", "qnodes", "seldepth", "tt_probes", "tt_hits",
    "tt_cutoffs", "tt_stores", "tt_allocations", "tt_reuses", "tt_clears", "tt_bytes",
    "see_calls", "stopped")
OUTCOME_NAMES = {
    chess.Termination.CHECKMATE: "checkmate",
    chess.Termination.STALEMATE: "stalemate",
    chess.Termination.INSUFFICIENT_MATERIAL: "known_dead_material",
    chess.Termination.SEVENTYFIVE_MOVES: "seventy_five_moves",
    chess.Termination.FIVEFOLD_REPETITION: "fivefold_repetition",
}


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def replay_position(initial_fen, history, expected_fen=None):
    """Return a board retaining every legal move after the supplied initial FEN.

    History before that initial FEN is unknown. Claimable draws do not themselves
    end a supplied history; automatic terminal positions do.
    """
    require(isinstance(initial_fen, str), "initial_fen must be a string")
    require(not any(character in initial_fen for character in "\t\r\n\0"),
            "initial_fen contains a protocol control character")
    require(isinstance(history, list), "history must be a list of UCI strings")
    board = chess.Board(initial_fen)
    require(board.is_valid(), "invalid initial position")
    for index, text in enumerate(history, 1):
        require(isinstance(text, str), f"history move {index} is not a string")
        require(board.outcome(claim_draw=False) is None, f"post-terminal history move {index}")
        move = chess.Move.from_uci(text)
        require(move in board.legal_moves, f"illegal history move {index}: {text}")
        board.push(move)
    if expected_fen is not None:
        require(board.fen(en_passant="fen") == expected_fen, "history/FEN mismatch")
    return board


def parse_search_output(stdout, board=None, node_limit=None, requested_depth=None):
    """Parse a complete CLI search reply; validate its resource and legal PV data.

    Optional PVS/LMR counters stay absent when an older binary does not advertise
    them. CLI mate reports count moves and lose distance parity; mate zero has no
    recoverable sign. No score in this function is an external correctness oracle.
    """
    require(isinstance(stdout, str), "CLI output must be text")
    fields = {}
    for line in stdout.splitlines():
        require(bool(line.strip()) and ":" in line, f"malformed CLI line: {line!r}")
        name, value = line.split(":", 1)
        require(re.fullmatch(r"[a-z][a-z0-9_]*", name) is not None,
                f"malformed CLI field: {name!r}")
        require(name not in fields, f"duplicate CLI field: {name}")
        fields[name] = value.strip()
    require("bestmove" in fields and "pv" in fields, "missing bestmove/PV")
    require(("score_cp" in fields) != ("score_mate" in fields),
            "require exactly one cp or mate score")
    for name in SEARCH_REQUIRED_NUMBERS:
        require(name in fields, f"missing CLI field: {name}")
    parsed = {}
    for name, value in fields.items():
        if name in ("bestmove", "pv"):
            continue
        require(re.fullmatch(r"-?\d+", value) is not None, f"noninteger CLI field: {name}")
        parsed[name] = int(value)
        if name not in ("score_cp", "score_mate"):
            require(parsed[name] >= 0, f"negative CLI counter: {name}")
    require(parsed["stopped"] in (0, 1), "stopped must be 0 or 1")
    parsed["stopped"] = bool(parsed["stopped"])
    parsed.setdefault("score_cp", None)
    parsed.setdefault("score_mate", None)
    parsed["score_pov"] = "mover"
    parsed["mate_zero_sign_ambiguous"] = parsed["score_mate"] == 0
    # CLI emits the last completed iteration (or its legal depth-zero fallback),
    # without UCI bound flags. This does not make selective search exact minimax.
    parsed["score_report"] = "CLI last completed iteration or depth-zero fallback; no bound flags emitted"
    text = fields["bestmove"]
    parsed["bestmove"] = None if text == "0000" else text
    if parsed["bestmove"] is not None:
        chess.Move.from_uci(parsed["bestmove"])
    parsed["pv"] = fields["pv"].split()
    if parsed["pv"]:
        require(parsed["pv"][0] == parsed["bestmove"], "PV/bestmove mismatch")
    require(parsed["qnodes"] <= parsed["nodes"], "qnodes exceed total nodes")
    require(parsed["tt_cutoffs"] <= parsed["tt_hits"] <= parsed["tt_probes"],
            "inconsistent TT counters")
    for prefix in ("pvs", "lmr"):
        numerator = "pvs_probes" if prefix == "pvs" else "lmr_reductions"
        denominator = prefix + "_researches"
        if numerator in parsed and denominator in parsed:
            require(parsed[denominator] <= parsed[numerator], f"inconsistent {prefix.upper()} counters")
    if node_limit is not None:
        require(isinstance(node_limit, int) and not isinstance(node_limit, bool) and node_limit > 0,
                "node limit must be positive")
        require(parsed["nodes"] <= node_limit, "search exceeded its node limit")
    if requested_depth is not None:
        require(isinstance(requested_depth, int) and not isinstance(requested_depth, bool)
                and 1 <= requested_depth <= 64, "requested depth must be 1..64")
        require(parsed["depth"] <= requested_depth, "search exceeded its requested depth")
    parsed["depth_completed"] = (parsed["depth"] == requested_depth
                                 if requested_depth is not None else None)
    parsed["node_limit_reached"] = (parsed["nodes"] == node_limit if node_limit is not None else None)
    parsed["raw_fields"] = fields
    if board is not None:
        position = board.copy(stack=True)
        terminal = position.outcome(claim_draw=False) is not None
        if parsed["bestmove"] is None:
            require(terminal, "missing bestmove in a nonterminal position")
            require(not parsed["pv"], "terminal search returned a PV without a bestmove")
        else:
            move = chess.Move.from_uci(parsed["bestmove"])
            require(not terminal and move in position.legal_moves, "illegal CLI bestmove")
        for index, text in enumerate(parsed["pv"], 1):
            require(position.outcome(claim_draw=False) is None, f"PV continues past terminal ply {index}")
            move = chess.Move.from_uci(text)
            require(move in position.legal_moves, f"illegal PV move {index}: {text}")
            position.push(move)
        parsed["pv_end_fen"] = position.fen(en_passant="fen")
    return parsed


def parse_static_probe(stdout, expected_boards=None):
    """Parse the probe's fixed 13 columns and optionally replay-check each row."""
    require(isinstance(stdout, str), "static probe output must be text")
    lines = stdout.splitlines()
    if expected_boards is not None:
        require(len(lines) == len(expected_boards), "static probe row count mismatch")
    result = []
    for index, line in enumerate(lines):
        cells = line.split("\t")
        require(len(cells) == 13, f"static probe row {index + 1} must have 13 columns")
        item = dict(zip(STATIC_FIELDS, cells))
        require(item["turn"] in ("w", "b"), "invalid static side to move")
        for name in (*STATIC_SCORE_FIELDS, "legal_count", "in_check", "claim_threefold_now",
                     "claim_fifty_moves_now", "repetitions"):
            require(re.fullmatch(r"-?\d+", item[name]) is not None,
                    f"noninteger static field: {name}")
            item[name] = int(item[name])
        for name in ("in_check", "claim_threefold_now", "claim_fifty_moves_now"):
            require(item[name] in (0, 1), f"invalid static boolean: {name}")
            item[name] = bool(item[name])
        require(item["legal_count"] >= 0 and item["repetitions"] >= 1,
                "invalid static legal/repetition count")
        require(item["outcome"] in {"ongoing", *OUTCOME_NAMES.values()}, "unknown static outcome")
        # Parsing also rejects malformed FENs when no historical board is given.
        canonical = chess.Board(item["fen"])
        require(canonical.is_valid(), "invalid static FEN")
        require(canonical.fen(en_passant="fen") == item["fen"], "noncanonical static FEN")
        require(item["turn"] == ("w" if canonical.turn else "b"), "static FEN/turn mismatch")
        if expected_boards is not None:
            board = expected_boards[index]
            require(item["fen"] == board.fen(en_passant="fen"), "static history/FEN mismatch")
            require(item["legal_count"] == board.legal_moves.count(), "static legal-count mismatch")
            require(item["in_check"] == board.is_check(), "static check mismatch")
            outcome = board.outcome(claim_draw=False)
            expected_outcome = OUTCOME_NAMES[outcome.termination] if outcome else "ongoing"
            require(item["outcome"] == expected_outcome, "static outcome mismatch")
            require(item["claim_threefold_now"] == (outcome is None and board.is_repetition(3)),
                    "static current threefold-claim mismatch")
            require(item["claim_fifty_moves_now"] == (outcome is None and board.is_fifty_moves()),
                    "static current fifty-move-claim mismatch")
            repetitions = 1
            while repetitions < len(board.move_stack) + 1 and board.is_repetition(repetitions + 1):
                repetitions += 1
            require(item["repetitions"] == repetitions, "static repetition-count mismatch")
        item["score_pov"] = "mover"
        item["raw_tsv"] = line
        result.append(item)
    return result


def search_graph_differences(left, right):
    """Compare advertised fixed-work fields; time and added LMR fields are exempt."""
    names = {"bestmove", "score_cp", "score_mate", "depth", "nodes", "qnodes", "seldepth",
             "stopped", "pv", "see_calls"}
    names.update(name for record in (left, right) for name in record
                 if name.startswith(("tt_", "pvs_")))
    return [name for name in sorted(names) if left.get(name) != right.get(name)]


def queries_for(case):
    budgets = {}
    archived = case.get("archived_search", {}).get("nodes")
    if isinstance(archived, int) and not isinstance(archived, bool) and archived > 0:
        budgets[archived] = ["original_archived_search_node_count"]
    for budget in FIXED_NODE_BUDGETS:
        budgets.setdefault(budget, []).append("predeclared_node_budget")
    return [{"id": f"nodes-{budget}", "depth": 64, "nodes": budget, "reasons": reasons}
            for budget, reasons in budgets.items()] + [dict(DEPTH_QUERY)]


def _subprocess_text(value):
    if value is None:
        return ""
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value


def execute_search(binary, flags, case, query):
    board = replay_position(case["initial_fen"], case["history"], case["fen"])
    command = [str(binary), "search", str(query["depth"]), "--nodes", str(query["nodes"]),
               "--fen", case["initial_fen"], *flags]
    if case["history"]:
        command += ["--play", *case["history"]]
    observation = {"command": command, "requested_limits": {"depth": query["depth"],
                   "nodes": query["nodes"], "movetime_ms": None}, "started_utc": utc_now(),
                   "timeout_seconds": 900, "status": "running"}
    started = time.perf_counter()
    try:
        response = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                  timeout=900, check=False)
        observation.update(returncode=response.returncode, stdout=response.stdout, stderr=response.stderr)
        require(response.returncode == 0, f"search process exited {response.returncode}")
        observation["search"] = parse_search_output(response.stdout, board, query["nodes"], query["depth"])
        observation["status"] = "complete"
    except subprocess.TimeoutExpired as error:
        observation.update(status="failed", error="search safety timeout (not an analysis time budget)",
                           returncode=None, stdout=_subprocess_text(error.stdout),
                           stderr=_subprocess_text(error.stderr))
    except Exception as error:
        observation.update(status="failed", error=f"{type(error).__name__}: {error}")
    observation.update(finished_utc=utc_now(), elapsed_seconds=time.perf_counter() - started)
    return observation


def static_observations(probe, case, requests):
    root = replay_position(case["initial_fen"], case["history"], case["fen"])
    boards, input_lines = [], []
    for request in requests:
        history = [*case["history"], *request["continuation"]]
        board = replay_position(case["initial_fen"], history)
        boards.append(board)
        input_lines.append(case["initial_fen"] + "\t" + " ".join(history))
    text = "\n".join(input_lines) + "\n"
    record = {"command": [str(probe)], "stdin": text, "timeout_seconds": 120,
              "status": "running", "requests": requests, "observations": []}
    try:
        response = subprocess.run([str(probe)], input=text, capture_output=True, text=True,
                                  encoding="utf-8", timeout=120, check=False)
        record.update(stdout=response.stdout, stderr=response.stderr, returncode=response.returncode)
        require(response.returncode == 0, f"static probe exited {response.returncode}")
        values = parse_static_probe(response.stdout, boards)
        for request, board, value in zip(requests, boards, values):
            sign = 1 if board.turn == root.turn else -1
            record["observations"].append({**request, "initial_fen": case["initial_fen"],
                "history": [*case["history"], *request["continuation"]], "static": value,
                "root_color": "w" if root.turn else "b", "root_pov_sign": sign,
                "root_pov_scores": {name: sign * value[name] for name in STATIC_SCORE_FIELDS},
                "interpretation": "supplied static position, not an asserted searched leaf; configuration differences are not additive"})
        record["status"] = "complete"
    except subprocess.TimeoutExpired as error:
        record.update(status="failed", error="static probe safety timeout", returncode=None,
                      stdout=_subprocess_text(error.stdout), stderr=_subprocess_text(error.stderr))
    except Exception as error:
        record.update(status="failed", error=f"{type(error).__name__}: {error}")
    return record


def _initial_static_requests(case):
    requests = [{"id": "root", "kind": "root", "continuation": []}]
    if case.get("played"):
        requests.append({"id": "played-child", "kind": "played_child", "continuation": [case["played"]]})
    if case.get("best", {}).get("move"):
        requests.append({"id": "external-best-child", "kind": "external_best_child",
                         "continuation": [case["best"]["move"]]})
    if case.get("paired_alternative"):
        requests.append({"id": "paired-alternative-child", "kind": "paired_alternative_child",
                         "continuation": [case["paired_alternative"]]})
    return requests


def checkpoint(stream, document):
    stream.seek(0)
    json.dump(document, stream, ensure_ascii=False, indent=2, allow_nan=False)
    stream.write("\n")
    stream.truncate()
    stream.flush()


def run(args, stream):
    document = {"schema": "regicide-loss-search-probe-v1", "started_utc": utc_now(),
        "finished_utc": None, "status": "running", "exit_code": 2, "errors": [],
        "screen": {"path": str(args.screen), "sha256": digest(args.screen)},
        "interpretation": {
            "purpose": "frozen-position search diagnosis, not a timing benchmark or strength test",
            "history": "full supplied history retained; history before initial_fen unknown",
            "limits": "nodes and depth only; wall-time safety timeout is a failure guard",
            "score": "cp/mate remain separate and favor the current mover; external cp is a different scale",
            "pv_end": "reported PV may be truncated by TT; endpoint need not be a searched leaf",
            "static": "four evaluator configurations are observations, not an additive feature decomposition",
            "archive_replay": "original time-stop and cold node-stop may choose different moves; mismatch is an observation",
            "source_binding": "source IDs are declared metadata, not a proof linking binary bytes to source"},
        "tool": {"path": str(Path(__file__).resolve()), "sha256": digest(Path(__file__).resolve()),
                 "python_version": sys.version, "chess_version": chess.__version__},
        "variants": {}, "static_probe": {}, "cases": [], "control_comparisons": []}
    checkpoint(stream, document)
    try:
        screen = json.loads(args.screen.read_text(encoding="utf-8-sig"))
        require(screen.get("status") == "complete", "screen is not complete")
        require(screen.get("phase") == "screen", "input is not a screening result")
        require(screen.get("exit_code") == 0 and not screen.get("errors"), "screen failed or has errors")
        selected = screen["selected"]
        auxiliary = screen.get("auxiliary", [])
        require(isinstance(selected, list) and isinstance(auxiliary, list) and selected,
                "screen selected/auxiliary must be lists, with selected nonempty")
        cases = [*selected, *auxiliary]
        require(len({case["id"] for case in cases}) == len(cases), "duplicate case ID")
        if "selected_ids" in screen:
            require(screen["selected_ids"] == [case["id"] for case in selected],
                    "screen selected IDs/order mismatch")
        variants = {"v057": (args.baseline, []), "v058_off": (args.candidate, ["--no-lmr"]),
                    "v058_on": (args.candidate, [])}
        for variant, (binary, flags) in variants.items():
            document["variants"][variant] = {"binary": str(binary), "sha256": digest(binary),
                "declared_source_commit": args.baseline_commit if variant == "v057" else args.candidate_commit,
                "cli_flags": flags, "context": "new process per query; default Hash 16 MiB",
                "options": {"Hash": 16, "UseLMR": None if variant == "v057" else variant == "v058_on"}}
        document["static_probe"] = {"binary": str(args.probe), "sha256": digest(args.probe),
                                     "declared_source_commit": args.probe_commit}
        document["case_count"] = len(cases)
        checkpoint(stream, document)
        for case_index, case in enumerate(cases, 1):
            board = replay_position(case["initial_fen"], case["history"], case["fen"])
            require(board.outcome(claim_draw=False) is None, f"terminal selected case: {case['id']}")
            item = {"id": case["id"], "source_case": case, "status": "running", "queries": [],
                    "static_batches": [], "archived_move_replays": []}
            document["cases"].append(item)
            checkpoint(stream, document)
            static = static_observations(args.probe, case, _initial_static_requests(case))
            item["static_batches"].append(static)
            checkpoint(stream, document)
            require(static["status"] == "complete", f"{case['id']} initial static: {static.get('error')}")
            endpoints = []
            for query in queries_for(case):
                row = {"query": query, "variants": {}, "control_differences": None}
                item["queries"].append(row)
                for variant in VARIANTS:
                    binary, flags = variants[variant]
                    observation = execute_search(binary, flags, case, query)
                    row["variants"][variant] = observation
                    checkpoint(stream, document)
                    require(observation["status"] == "complete", f"{case['id']} {query['id']} {variant}: {observation.get('error')}")
                    search = observation["search"]
                    endpoints.append({"id": query["id"] + "/" + variant + "/pv-end", "kind": "own_pv_end",
                                      "variant": variant, "query_id": query["id"], "continuation": search["pv"],
                                      "searched_leaf_claim": False})
                    if "original_archived_search_node_count" in query["reasons"]:
                        item["archived_move_replays"].append({"variant": variant,
                            "requested_nodes": query["nodes"], "archived_move": case["played"],
                            "node_replay_move": search["bestmove"], "matches": search["bestmove"] == case["played"],
                            "interpretation": "observation only: cold node-stop differs from original retained/time-stop context"})
                differences = search_graph_differences(row["variants"]["v057"]["search"],
                                                       row["variants"]["v058_off"]["search"])
                row["control_differences"] = differences
                comparison = {"case_id": case["id"], "query_id": query["id"],
                              "passed": not differences, "differences": differences}
                document["control_comparisons"].append(comparison)
                if differences:
                    document["errors"].append(f"v057/v058 OFF graph mismatch: {case['id']} {query['id']}: {differences}")
                checkpoint(stream, document)
            static = static_observations(args.probe, case, endpoints)
            item["static_batches"].append(static)
            checkpoint(stream, document)
            require(static["status"] == "complete", f"{case['id']} PV static: {static.get('error')}")
            item["status"] = "complete"
            checkpoint(stream, document)
            print(f"COMPLETE {case_index}/{len(cases)} {case['id']}: {len(item['queries'])} limits, 3 variants; "
                  f"controls {'PASS' if all(not row['control_differences'] for row in item['queries']) else 'FAIL'}", flush=True)
        document["status"] = "failed" if document["errors"] else "complete"
        document["exit_code"] = 2 if document["errors"] else 0
    except Exception as error:
        document["status"] = "failed"
        document["errors"].append(f"{type(error).__name__}: {error}")
        if document["cases"] and document["cases"][-1]["status"] == "running":
            document["cases"][-1]["status"] = "failed"
        document["exit_code"] = 2
    document["finished_utc"] = utc_now()
    checkpoint(stream, document)
    for message in document["errors"]:
        print("ERROR " + message, file=sys.stderr, flush=True)
    return document


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("screen", "baseline", "candidate", "probe", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--baseline-commit", default=BASELINE_COMMIT)
    parser.add_argument("--candidate-commit", default=CANDIDATE_COMMIT)
    parser.add_argument("--probe-commit", default=None,
                        help="optional caller-declared diagnostic-probe source commit; null if not frozen")
    args = parser.parse_args(argv)
    try:
        for name in ("screen", "baseline", "candidate", "probe", "output"):
            setattr(args, name, getattr(args, name).expanduser().resolve())
        for name in ("screen", "baseline", "candidate", "probe"):
            require(getattr(args, name).is_file(), f"input file does not exist: {getattr(args, name)}")
        require(args.output not in {args.screen, args.baseline, args.candidate, args.probe},
                "output must not replace an input")
        require(not args.output.exists(), f"output already exists: {args.output}")
        for name in ("baseline_commit", "candidate_commit", "probe_commit"):
            value = getattr(args, name)
            require(value is None or re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", value),
                    f"{name} must be a full 40/64-character commit ID")
    except (ValueError, OSError) as error:
        parser.error(str(error))
    return args


def main(argv=None):
    args = arguments(argv)
    try:
        # Exclusive creation reserves this result. Only this invocation rewrites
        # that reserved file while checkpointing partial, honestly labelled data.
        with args.output.open("x", encoding="utf-8", newline="\n") as stream:
            document = run(args, stream)
    except OSError as error:
        print(f"ERROR {type(error).__name__}: {error}", file=sys.stderr)
        return 2
    return document["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
