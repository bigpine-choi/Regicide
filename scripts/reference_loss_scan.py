"""Finite, node-limited external reference evidence for the five archived losses.

This diagnostic never changes Regicide, plays a match, or labels a disagreement
an evaluation bug. Screen every candidate decision first, select positions by
the fixed policy below, then confirm those positions at two larger budgets.
Every position retains the complete game history, including draw context.

The output must be new. Checkpoints preserve completed work after each query;
an exception leaves a failed, partial report and exits with status 2. A hard
kill can still interrupt a checkpoint write. Engine options and origin claims
are recorded separately from the executable bytes that this script hashes.
"""

import argparse
import copy
import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import sys
import time

import chess
import chess.engine


LOSS_GAMES = (1, 3, 9, 13, 16)
SCREEN_NODES = 50_000
CONFIRM_NODES = (1_000_000, 4_000_000)
REFERENCE_OPTIONS = {
    "Threads": 1,
    "Hash": 32,
    "Use NNUE": True,
    "UCI_LimitStrength": False,
    "SyzygyProbeLimit": 0,
}
SELECTION_POLICY = {
    "version": 1,
    "significant_cp_gap": 100,
    "minimum_unrestricted_cp": -300,
    "earliest": "earliest cp gap >=100 with best cp >=-300, or best nonlosing to forced losing mate",
    "nonlosing_for_mate_transition": "best cp >=0 or best mate >0; forced mate <0; no bound scores",
    "largest": "largest finite cp gap with best cp >=-300; if none, largest finite gap as fallback",
    "ties": "earliest ply",
    "deduplicate": True,
    "maximum_primary_positions_per_game": 2,
    "no_finite_fallback": "fail rather than invent a position or convert mate to cp",
}


class ReferenceEngine(chess.engine.SimpleEngine):
    def _timeout_for(self, limit):
        # python-chess normally disables its analysis watchdog when limit.time
        # is None. Retain a 600-second communication watchdog while sending
        # only a node budget to the engine; this is not a movetime limit.
        return self.timeout


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def command_argument(value):
    try:
        command = json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError("reference command must be a JSON array") from exc
    if (not isinstance(command, list) or not command or
            any(not isinstance(item, str) or not item or "\0" in item for item in command)):
        raise argparse.ArgumentTypeError("reference command must be a nonempty JSON array of nonempty strings")
    return command


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--reference-command", type=command_argument, required=True)
    parser.add_argument("--reference-binary", type=Path, required=True,
                        help="actual executable bytes readable by this Python process, including a WSL UNC path")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--phase", choices=("screen", "confirm"), required=True)
    parser.add_argument("--screen", type=Path)
    args = parser.parse_args(argv)
    if args.phase == "confirm" and args.screen is None:
        parser.error("--screen is required for confirm")
    if args.phase == "screen" and args.screen is not None:
        parser.error("--screen is only valid for confirm")
    return args


class Checkpoint:
    """Reserve once with exclusive creation; never replace an existing file."""

    def __init__(self, path, report):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("x", encoding="utf-8", newline="\n")
        self.report = report
        self.save()

    def save(self):
        self.report["checkpoint_utc"] = utc_now()
        self.stream.seek(0)
        json.dump(self.report, self.stream, ensure_ascii=False, indent=2, allow_nan=False)
        self.stream.write("\n")
        self.stream.truncate()
        self.stream.flush()
        os.fsync(self.stream.fileno())

    def close(self):
        self.stream.close()


def board_for(record):
    board = chess.Board(record["initial_fen"])
    for text in record["history"]:
        move = chess.Move.from_uci(text)
        if move not in board.legal_moves:
            raise ValueError(f"{record['id']}: illegal history move {text}")
        board.push(move)
    if board.fen(en_passant="fen") != record["fen"]:
        raise ValueError(f"{record['id']}: history and FEN disagree")
    color = "w" if board.turn else "b"
    if color != record["candidate_color"]:
        raise ValueError(f"{record['id']}: candidate color does not match side to move")
    if board.is_game_over(claim_draw=True):
        raise ValueError(f"{record['id']}: diagnostic decision position is already terminal/claimable")
    played = chess.Move.from_uci(record["played"])
    if played not in board.legal_moves:
        raise ValueError(f"{record['id']}: archived decision is illegal")
    if board.san(played) != record["san"]:
        raise ValueError(f"{record['id']}: SAN does not match archived decision")
    return board


def source_games(source):
    if source.get("completed") is not True or source.get("errors"):
        raise ValueError("source match must be complete and have no errors")
    games = source.get("games")
    if not isinstance(games, list):
        raise ValueError("source games must be a list")
    indexed = {}
    for game in games:
        number = game["game"]
        if number in indexed:
            raise ValueError(f"duplicate source game {number}")
        indexed[number] = game
    losses = sorted(game["game"] for game in games if game.get("candidate_points") == 0)
    if losses != list(LOSS_GAMES):
        raise ValueError(f"expected exactly candidate losses {LOSS_GAMES}, got {losses}")
    for number in LOSS_GAMES:
        game = indexed[number]
        if not game.get("completed") or game.get("error"):
            raise ValueError(f"source game {number} is not a complete error-free loss")
        candidate_white = game["white"] == "candidate"
        expected = "0-1" if candidate_white else "1-0"
        if game["result"] != expected or game["black"] != ("baseline" if candidate_white else "candidate"):
            raise ValueError(f"source game {number}: role/result mismatch")
    return indexed


def position_record(game, search, *, identifier=None):
    ply = search["ply"]
    if not isinstance(ply, int) or isinstance(ply, bool) or not 1 <= ply <= len(game["uci_moves"]):
        raise ValueError("invalid archived ply")
    history = game["uci_moves"][:ply - 1]
    board = chess.Board(game["initial_fen"])
    for text in history:
        board.push_uci(text)
    played = game["uci_moves"][ply - 1]
    if played != search["move"] or board.fen(en_passant="fen") != search["fen_before"]:
        raise ValueError(f"game {game['game']} ply {ply}: archive/search mismatch")
    color = "w" if board.turn else "b"
    if search["role"] != "candidate" or game["white" if board.turn else "black"] != "candidate":
        raise ValueError(f"game {game['game']} ply {ply}: not a candidate decision")
    if search.get("color") != ("white" if board.turn else "black"):
        raise ValueError(f"game {game['game']} ply {ply}: archived color mismatch")
    record = {
        "id": identifier or f"g{game['game']}-p{ply}",
        "game": game["game"], "ply": ply,
        "opening": game["opening"], "label": "primary_loss_decision",
        "initial_fen": game["initial_fen"], "history": history,
        "fen": board.fen(en_passant="fen"), "candidate_color": color,
        "played": played, "san": board.san(chess.Move.from_uci(played)),
        "archived_search": copy.deepcopy(search), "selection_reasons": [],
    }
    board_for(record)
    return record


def primary_records(games):
    records = []
    for number in LOSS_GAMES:
        game = games[number]
        seen = set()
        for search in game["search_moves"]:
            if search["role"] != "candidate":
                continue
            record = position_record(game, search)
            if record["ply"] in seen:
                raise ValueError(f"duplicate candidate search in game {number}")
            seen.add(record["ply"])
            records.append(record)
        expected = {
            ply for ply in range(len(game["opening_uci_moves"]) + 1, len(game["uci_moves"]) + 1)
            if (ply % 2 == 1) == (game["white"] == "candidate")
        }
        if seen != expected:
            raise ValueError(f"game {number}: candidate search coverage differs from played decisions")
    return records


def auxiliary_record(games):
    left, right = games[3], games[4]
    if left["initial_fen"] != right["initial_fen"] or left["uci_moves"][:73] != right["uci_moves"][:73]:
        raise ValueError("pair 2 does not share the recorded 73-ply history")
    if left["uci_moves"][73] != "f3f2" or right["uci_moves"][73] != "d1e2":
        raise ValueError("pair 2 recorded alternatives are not f3f2/d1e2")
    searches = [item for item in right["search_moves"] if item["ply"] == 74]
    if len(searches) != 1:
        raise ValueError("pair 2 auxiliary archived search is missing or duplicated")
    record = position_record(right, searches[0], identifier="pair2-p74")
    record.update(label="auxiliary_paired_divergence", paired_source_game=3,
                  paired_alternative="f3f2", paired_alternative_san="f2",
                  selection_reasons=["first pair-2 divergence after 73 identical plies; not a sixth loss"])
    board = board_for(record)
    alternative = chess.Move.from_uci(record["paired_alternative"])
    if alternative not in board.legal_moves or board.san(alternative) != "f2" or record["candidate_color"] != "b":
        raise ValueError("pair 2 auxiliary alternative/color is invalid")
    return record


def option_record(option):
    return {"type": option.type, "default": option.default, "min": option.min,
            "max": option.max, "var": option.var}


def configure_reference(engine, report, checkpoint):
    advertised = {name: option_record(option) for name, option in engine.options.items()}
    report["reference"]["engine_id"] = dict(engine.id)
    report["reference"]["advertised_options"] = advertised
    validated = {}
    for name, requested in REFERENCE_OPTIONS.items():
        if name not in engine.options:
            raise ValueError(f"required reference option is unavailable: {name}")
        parsed = engine.options[name].parse(requested)
        if parsed != requested or type(parsed) is not type(requested):
            raise ValueError(f"reference option did not validate unchanged: {name}")
        validated[name] = parsed
    if "Clear Hash" not in engine.options or engine.options["Clear Hash"].type != "button":
        raise ValueError("reference must advertise a Clear Hash button")
    engine.configure(validated)
    report["reference"]["validated_options"] = validated
    report["reference"]["configuration_note"] = (
        "Values were validated against advertised option types/ranges and configure returned. "
        "UCI has no general readback; this is not independent verification of engine internals. "
        "python-chess manages MultiPV/Ponder/UCI_Chess960/UCI_Variant.")
    checkpoint.save()


def analyse_query(engine, record, nodes, forced, report, checkpoint):
    board = board_for(record)
    root_move = chess.Move.from_uci(forced) if forced is not None else None
    if root_move is not None and root_move not in board.legal_moves:
        raise ValueError(f"{record['id']}: illegal forced alternative {forced}")
    activity = {"record_id": record["id"], "requested_nodes": nodes,
                "forced_move": forced, "started_utc": utc_now(), "status": "running"}
    report["active_query"] = activity
    checkpoint.save()
    engine.configure({"Clear Hash": None})
    start = time.perf_counter()
    info = engine.analyse(board, chess.engine.Limit(nodes=nodes),
                          root_moves=[root_move] if root_move is not None else None,
                          game=object(), info=chess.engine.INFO_ALL)
    wall = time.perf_counter() - start
    pv = info.get("pv", [])
    if not pv or info.get("score") is None:
        raise ValueError(f"{record['id']}: reference omitted score/PV")
    if root_move is not None and pv[0] != root_move:
        raise ValueError(f"{record['id']}: reference ignored forced root move")
    line = board.copy(stack=True)
    for move in pv:
        if move not in line.legal_moves:
            raise ValueError(f"{record['id']}: illegal reference PV move {move.uci()}")
        line.push(move)
    actual_nodes = info.get("nodes")
    depth = info.get("depth")
    if (not isinstance(actual_nodes, int) or isinstance(actual_nodes, bool) or actual_nodes < 1 or
            not isinstance(depth, int) or isinstance(depth, bool) or depth < 1):
        raise ValueError(f"{record['id']}: reference omitted positive nodes/depth")
    score = info["score"].pov(board.turn)
    cp, mate = score.score(), score.mate()
    if (cp is None) == (mate is None):
        raise ValueError(f"{record['id']}: reference score is neither exclusively cp nor mate")
    finished = utc_now()
    result = {
        "move": pv[0].uci(), "pv": [move.uci() for move in pv],
        "score": {"cp": cp, "mate": mate, "pov": record["candidate_color"]},
        "requested_nodes": nodes, "nodes": actual_nodes, "depth": depth,
        "seldepth": info.get("seldepth"),
        "upperbound": bool(info.get("upperbound", False)),
        "lowerbound": bool(info.get("lowerbound", False)),
        "engine_time_seconds": info.get("time"), "wall_time_seconds": wall,
        "started_utc": activity["started_utc"], "finished_utc": finished,
    }
    report["queries_completed"] += 1
    activity.update(status="complete", finished_utc=finished)
    return result


def exact_score(result):
    return not result.get("upperbound", False) and not result.get("lowerbound", False)


def cp_gap(best, forced):
    if not exact_score(best) or not exact_score(forced):
        return None
    best_cp, played_cp = best["score"]["cp"], forced["score"]["cp"]
    return best_cp - played_cp if best_cp is not None and played_cp is not None else None


def significant(record):
    best, forced = record["best"], record["forced_played"]
    if not exact_score(best) or not exact_score(forced):
        return False
    best_cp = best["score"]["cp"]
    gap = record["cp_gap"]
    finite = best_cp is not None and best_cp >= -300 and gap is not None and gap >= 100
    best_mate, forced_mate = best["score"]["mate"], forced["score"]["mate"]
    nonlosing = (best_cp is not None and best_cp >= 0) or (best_mate is not None and best_mate > 0)
    return finite or (nonlosing and forced_mate is not None and forced_mate < 0)


def select_records(records):
    """Mutate only reason labels; return at most two predeclared picks per loss."""
    for record in records:
        record["selection_reasons"] = []
    selected = []
    for game in LOSS_GAMES:
        group = sorted((item for item in records if item["game"] == game), key=lambda item: item["ply"])
        if not group or any("best" not in item or "forced_played" not in item for item in group):
            raise ValueError(f"game {game}: selection requires the complete screen")
        earliest = next((item for item in group if significant(item)), None)
        if earliest is not None:
            earliest["selection_reasons"].append("earliest_significant_reference_gap_or_mate_transition")
        finite = [item for item in group if item["cp_gap"] is not None]
        viable = [item for item in finite if item["best"]["score"]["cp"] >= -300]
        pool = viable or finite
        largest = max(pool, key=lambda item: (item["cp_gap"], -item["ply"])) if pool else None
        if largest is not None:
            largest["selection_reasons"].append(
                "largest_finite_gap_with_best_cp_at_least_minus_300" if viable else
                "fallback_largest_finite_gap_already_losing_reference_position")
        picks = [item for item in (earliest, largest) if item is not None]
        if not picks:
            raise ValueError(f"game {game}: no eligible finite gap or mate-transition position")
        seen = set()
        for item in picks:
            if item["id"] not in seen:
                selected.append(item)
                seen.add(item["id"])
    return selected


def run_screen(engine, games, report, checkpoint):
    records = primary_records(games)
    report["expected_primary_decisions"] = len(records)
    report["records"] = records
    report["auxiliary"] = [auxiliary_record(games)]
    checkpoint.save()
    for game in LOSS_GAMES:
        for record in records:
            if record["game"] != game:
                continue
            for key, forced in (("best", None), ("forced_played", record["played"])):
                record[key] = analyse_query(engine, record, SCREEN_NODES, forced, report, checkpoint)
                checkpoint.save()
            record["cp_gap"] = cp_gap(record["best"], record["forced_played"])
            record["status"] = "complete"
            checkpoint.save()
        count = sum(item["game"] == game for item in records)
        print(f"screen game {game}: {count} candidate decisions complete", flush=True)
    report["selected"] = select_records(records)
    report["selected_ids"] = [item["id"] for item in report["selected"]]
    checkpoint.save()
    print("screen selected: " + ", ".join(report["selected_ids"]), flush=True)


def checked_screen(screen, report, games):
    if screen.get("phase") != "screen" or screen.get("status") != "complete":
        raise ValueError("confirm requires a complete screen report")
    if screen.get("source", {}).get("sha256") != report["source"]["sha256"]:
        raise ValueError("screen was not produced from the same source bytes")
    if screen.get("reference", {}).get("binary_sha256") != report["reference"]["binary_sha256"]:
        raise ValueError("screen used different reference executable bytes")
    if screen.get("reference", {}).get("validated_options") != REFERENCE_OPTIONS:
        raise ValueError("screen reference settings differ")
    if screen.get("selection_policy") != SELECTION_POLICY:
        raise ValueError("screen selection policy differs")
    expected = {item["id"]: item for item in primary_records(games)}
    records = copy.deepcopy(screen.get("records", []))
    if len(records) != len(expected) or {item["id"] for item in records} != set(expected):
        raise ValueError("screen decision coverage differs from source")
    fields = ("game", "ply", "initial_fen", "history", "fen", "candidate_color", "played", "san", "archived_search")
    for item in records:
        if any(item.get(field) != expected[item["id"]][field] for field in fields):
            raise ValueError(f"{item['id']}: screen position differs from source")
        if item.get("status") != "complete" or item.get("cp_gap") != cp_gap(item["best"], item["forced_played"]):
            raise ValueError(f"{item['id']}: screen is partial or gap is inconsistent")
    selected = select_records(records)
    if screen.get("selected_ids") != [item["id"] for item in selected]:
        raise ValueError("screen selected IDs do not follow the fixed policy")
    if screen.get("selected") != selected:
        raise ValueError("screen selected records do not match independently selected records")
    auxiliary = auxiliary_record(games)
    if screen.get("auxiliary") != [auxiliary]:
        raise ValueError("screen auxiliary position differs from paired source history")
    return selected, auxiliary


def run_confirm(engine, games, screen, report, checkpoint):
    selected, auxiliary = checked_screen(screen, report, games)
    for original in [*selected, auxiliary]:
        record = {key: copy.deepcopy(value) for key, value in original.items()
                  if key not in ("best", "forced_played", "cp_gap", "status")}
        if original["label"] != "auxiliary_paired_divergence":
            record["screen_analysis"] = {key: copy.deepcopy(original[key])
                                         for key in ("best", "forced_played", "cp_gap")}
        record["budgets"] = []
        record["status"] = "running"
        report["records"].append(record)
        checkpoint.save()
        for nodes in CONFIRM_NODES:
            result = {"requested_nodes": nodes, "status": "running"}
            record["budgets"].append(result)
            queries = [("best", None), ("forced_played", record["played"])]
            if "paired_alternative" in record:
                queries.append(("forced_paired_alternative", record["paired_alternative"]))
            for key, forced in queries:
                result[key] = analyse_query(engine, record, nodes, forced, report, checkpoint)
                checkpoint.save()
            result["cp_gap"] = cp_gap(result["best"], result["forced_played"])
            result["status"] = "complete"
            checkpoint.save()
            print(f"confirm {record['id']}: {nodes} nodes per query complete", flush=True)
        record["status"] = "complete"
        checkpoint.save()
        print(f"confirm position {record['id']} complete", flush=True)


def new_report(args):
    return {
        "schema_version": 1, "phase": args.phase, "status": "running",
        "started_utc": utc_now(), "finished_utc": None,
        "purpose": "finite external reference evidence; not a proof of the best move or an evaluation bug",
        "loss_game_ids": list(LOSS_GAMES),
        "selection_policy": copy.deepcopy(SELECTION_POLICY),
        "screen_nodes_per_query": SCREEN_NODES,
        "confirm_nodes_per_query": list(CONFIRM_NODES),
        "limits_note": "Only fixed node budgets; no move time or depth limit. Actual reported nodes may overshoot. Engine communication timeout is 600 seconds.",
        "table_policy": "Clear Hash before every query and a fresh python-chess game object per query",
        "source": {"path": str(args.source), "sha256": None},
        "screen_input": {"path": str(args.screen), "sha256": None} if args.screen is not None else None,
        "reference": {
            "command": args.reference_command, "binary_path": str(args.reference_binary),
            "binary_sha256": None, "requested_options": copy.deepcopy(REFERENCE_OPTIONS),
            "command_binary_relationship": "caller-declared; hashing the supplied file does not prove the command executes that exact file",
            "origin": {
                "upstream_release_url": "https://github.com/official-stockfish/Stockfish/releases/tag/sf_16",
                "license": "GPL-3.0", "distribution_build": "Ubuntu 16-1build1",
                "status": "caller-declared attribution; not independently established by this script or by the UCI engine ID",
            },
        },
        "runtime": {"python": sys.version, "python_chess": chess.__version__,
                    "platform": platform.platform()},
        "queries_completed": 0, "active_query": None, "records": [], "errors": [],
    }


def main(argv=None):
    args = parse_args(argv)
    report = new_report(args)
    checkpoint = None
    engine = None
    exit_code = 0
    try:
        checkpoint = Checkpoint(args.output, report)
        report["source"]["sha256"] = sha256_file(args.source)
        source = json.loads(args.source.read_text(encoding="utf-8-sig"))
        games = source_games(source)
        report["source"]["engine_provenance_caller_declared_by_match"] = copy.deepcopy(source.get("engines"))
        report["source"]["limits"] = copy.deepcopy(source.get("limits"))
        report["reference"]["binary_sha256"] = sha256_file(args.reference_binary)
        screen = None
        if args.screen is not None:
            report["screen_input"]["sha256"] = sha256_file(args.screen)
            screen = json.loads(args.screen.read_text(encoding="utf-8-sig"))
            checked_screen(screen, report, games)
        # Source validation is complete before starting any external process.
        primary_records(games)
        auxiliary_record(games)
        checkpoint.save()
        engine = ReferenceEngine.popen_uci(args.reference_command, timeout=600)
        configure_reference(engine, report, checkpoint)
        if args.phase == "screen":
            run_screen(engine, games, report, checkpoint)
        else:
            run_confirm(engine, games, screen, report, checkpoint)
        report["status"] = "complete"
    except (Exception, KeyboardInterrupt) as exc:
        exit_code = 2
        report["status"] = "failed"
        report["errors"].append({"type": type(exc).__name__, "message": str(exc), "utc": utc_now()})
        if report["active_query"] is not None and report["active_query"]["status"] == "running":
            report["active_query"].update(status="failed", finished_utc=utc_now())
        print(f"reference {args.phase} failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
    finally:
        if engine is not None:
            try:
                engine.quit()
            except Exception as exc:
                exit_code = 2
                report["status"] = "failed"
                report["errors"].append({"type": type(exc).__name__, "message": "engine shutdown: " + str(exc), "utc": utc_now()})
                engine.close()
        report["finished_utc"] = utc_now()
        report["exit_code"] = exit_code
        if checkpoint is not None:
            try:
                checkpoint.save()
            finally:
                checkpoint.close()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
