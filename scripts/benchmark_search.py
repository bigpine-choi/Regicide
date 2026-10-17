"""Paired, cold-table, fixed-depth UCI search-cost benchmark (not a strength test).

Every sample starts a new UCI game, preserving the declared opening's move
history while clearing a conforming engine's previous game search state. Engine
process startup is excluded; wall time includes the complete UCI play transaction
(including ucinewgame/isready). Engine-reported time is retained separately.
Each position's engine order alternates across repetitions; a seeded shuffle
changes position order. No score or move is inferred from wall-clock speed.
"""

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import random
import re
import statistics
import subprocess
import sys
import time

import chess
import chess.engine


ROOT = Path(__file__).resolve().parents[1]
ROLES = ("baseline", "candidate")


def positive_int(text):
    value = int(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


def git_command(*arguments):
    return subprocess.run(["git", "-C", str(ROOT), *arguments], capture_output=True,
                          text=True, check=True, timeout=10).stdout.strip()


def verified_commit(text):
    if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", text):
        raise ValueError("source commits must be full 40/64-character hexadecimal object IDs")
    resolved = git_command("rev-parse", "--verify", text + "^{commit}")
    if resolved.lower() != text.lower():
        raise ValueError(f"source ID is not a commit object: {text}")
    return resolved


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def board_for(position):
    board = chess.Board(position.get("fen", chess.STARTING_FEN))
    if not board.is_valid():
        raise ValueError(f"invalid position: {position['name']}")
    for text in position.get("moves", []):
        move = chess.Move.from_uci(text)
        if board.outcome(claim_draw=True) is not None or move not in board.legal_moves:
            raise ValueError(f"illegal or post-terminal move {text}: {position['name']}")
        board.push(move)
    if board.outcome(claim_draw=True) is not None:
        raise ValueError(f"benchmark position is already terminal: {position['name']}")
    return board


def load_positions(path):
    document = json.loads(path.read_text(encoding="utf-8"))
    positions = document["positions"]
    if not isinstance(positions, list) or not positions:
        raise ValueError("position file must contain a nonempty positions list")
    names = set()
    for position in positions:
        name = position["name"]
        if not isinstance(name, str) or not name or name in names:
            raise ValueError("position names must be nonempty and unique")
        names.add(name)
        if not isinstance(position.get("moves", []), list):
            raise ValueError("position moves must be a list of UCI strings")
        position["search_fen"] = board_for(position).fen(en_passant="fen")
    return document, positions


def options_object(text):
    value = json.loads(text)
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("engine options must be a JSON object with string keys")
    if any(not isinstance(item, (str, int, float, bool, type(None))) for item in value.values()):
        raise ValueError("engine option values must be scalar JSON values")
    return value


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--positions", type=Path, default=ROOT / "tests/data/profile_positions.json")
    parser.add_argument("--depth", type=positive_int, default=3)
    parser.add_argument("--repeats", type=positive_int, default=5)
    parser.add_argument("--seed", type=int, default=20261017)
    parser.add_argument("--baseline-commit", required=True)
    parser.add_argument("--candidate-commit", required=True)
    parser.add_argument("--baseline-options", default="{}")
    parser.add_argument("--candidate-options", default="{}")
    parser.add_argument("--allow-score-differences", action="store_true",
                        help="retain and visibly report differing exact scores, but exit successfully")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    try:
        for field in ("baseline", "candidate", "positions", "output"):
            setattr(args, field, getattr(args, field).expanduser().resolve())
        for role in ROLES:
            setattr(args, role + "_commit", verified_commit(getattr(args, role + "_commit")))
            setattr(args, role + "_options", options_object(getattr(args, role + "_options")))
            if not getattr(args, role).is_file():
                raise ValueError(f"engine executable does not exist: {getattr(args, role)}")
        args.position_document, args.selected_positions = load_positions(args.positions)
        if args.output in {args.baseline, args.candidate, args.positions}:
            raise ValueError("output must not replace an input file")
        if args.output.exists() and not args.overwrite:
            raise ValueError(f"output already exists; use --overwrite: {args.output}")
    except (KeyError, TypeError, ValueError, OSError, subprocess.SubprocessError) as exc:
        parser.error(str(exc))
    return args


def score_record(info, color):
    score = info.get("score")
    if score is None:
        raise RuntimeError("engine did not report its search score")
    point = score.pov(color)
    return {"pov": "mover", "cp": point.score(), "mate": point.mate(),
            "lowerbound": bool(info.get("lowerbound", False)),
            "upperbound": bool(info.get("upperbound", False))}


def transposition_counters(info):
    """Parse Regicide's optional final UCI telemetry; absent is not zero."""
    text = info.get("string")
    if not isinstance(text, str):
        return None
    match = re.fullmatch(r"tt probes (\d+) hits (\d+) cutoffs (\d+) stores (\d+)", text.strip())
    if match is None:
        return None
    counters = dict(zip(("probes", "hits", "cutoffs", "stores"), map(int, match.groups())))
    if not counters["cutoffs"] <= counters["hits"] <= counters["probes"]:
        raise RuntimeError(f"inconsistent transposition counters: {text}")
    return counters


def sample(engine, position, depth):
    board = board_for(position)
    original = board.fen(en_passant="fen")
    start = time.perf_counter()
    # A fresh game token makes python-chess send ucinewgame before every sample.
    reply = engine.play(board, chess.engine.Limit(depth=depth), game=object(),
                        info=chess.engine.INFO_ALL)
    wall = time.perf_counter() - start
    if reply.move is None or reply.move not in board.legal_moves:
        raise RuntimeError(f"illegal or missing bestmove {reply.move}: {position['name']}")
    info = reply.info
    if info.get("depth") != depth:
        raise RuntimeError(f"engine did not finish requested depth {depth}: {info.get('depth')}")
    if not isinstance(info.get("nodes"), int) or info["nodes"] < 1:
        raise RuntimeError("engine did not report a positive search node count")
    result = {"move": reply.move.uci(), "depth": info["depth"], "seldepth": info.get("seldepth"),
              "nodes": info["nodes"], "engine_time_seconds": info.get("time"),
              "wall_time_seconds": wall, "score": score_record(info, board.turn),
              "pv": [move.uci() for move in info.get("pv", [])],
              "tt_counters": transposition_counters(info)}
    if result["score"]["lowerbound"] or result["score"]["upperbound"]:
        raise RuntimeError("benchmark requires an exact root score, not a bound")
    for text in result["pv"]:
        move = chess.Move.from_uci(text)
        if move not in board.legal_moves:
            raise RuntimeError(f"illegal PV move {text}: {position['name']}")
        board.push(move)
    if result["pv"] and result["pv"][0] != result["move"]:
        raise RuntimeError("bestmove differs from the first PV move")
    result["search_fen"] = original
    return result


def schedule(positions, repeats, seed):
    rng = random.Random(seed)
    for repetition in range(repeats):
        indices = list(range(len(positions)))
        rng.shuffle(indices)
        for index in indices:
            order = list(ROLES if (repetition + index) % 2 == 0 else reversed(ROLES))
            yield repetition + 1, index, order


def median_or_none(values):
    return statistics.median(values) if values else None


def summarize(records, positions):
    complete = [record for record in records if all(role in record for role in ROLES)]
    wall_ratios = [record["baseline"]["wall_time_seconds"] / record["candidate"]["wall_time_seconds"]
                   for record in complete if record["candidate"]["wall_time_seconds"] > 0]
    engine_ratios = [record["baseline"]["engine_time_seconds"] / record["candidate"]["engine_time_seconds"]
                     for record in complete
                     if record["baseline"]["engine_time_seconds"] is not None
                     and record["candidate"]["engine_time_seconds"] is not None
                     and record["baseline"]["engine_time_seconds"] > 0
                     and record["candidate"]["engine_time_seconds"] > 0]
    result = {"completed_pairs": len(complete),
              "score_mismatches": sum(not record["scores_equal"] for record in complete),
              "median_paired_wall_speedup_baseline_over_candidate": median_or_none(wall_ratios),
              "median_paired_engine_speedup_baseline_over_candidate": median_or_none(engine_ratios),
              "engine_time_ratio_pairs_excluding_zero_or_missing_time": len(engine_ratios),
              "median_paired_node_ratio_candidate_over_baseline": median_or_none([
                  record["candidate"]["nodes"] / record["baseline"]["nodes"] for record in complete]),
              "transposition_counters": {}, "per_position": []}
    for role in ROLES:
        reported = [record[role]["tt_counters"] for record in complete
                    if record[role].get("tt_counters") is not None]
        result["transposition_counters"][role] = {
            "samples_reporting": len(reported),
            **{name: sum(counters[name] for counters in reported) if reported else None
               for name in ("probes", "hits", "cutoffs", "stores")}}
    for position in positions:
        pairs = [record for record in complete if record["position"] == position["name"]]
        row = {"name": position["name"], "pairs": len(pairs)}
        for role in ROLES:
            row[role] = {"median_wall_time_seconds": median_or_none([
                pair[role]["wall_time_seconds"] for pair in pairs]),
                         "median_nodes": median_or_none([pair[role]["nodes"] for pair in pairs])}
        row["median_paired_wall_speedup_baseline_over_candidate"] = median_or_none([
            pair["baseline"]["wall_time_seconds"] / pair["candidate"]["wall_time_seconds"]
            for pair in pairs if pair["candidate"]["wall_time_seconds"] > 0])
        result["per_position"].append(row)
    return result


def environment():
    result = {"platform": platform.platform(), "processor": platform.processor(),
              "logical_cpu_count": os.cpu_count(), "python": sys.version,
              "python_executable": sys.executable, "python_chess": chess.__version__}
    try:
        result["harness_checkout"] = {"commit": git_command("rev-parse", "HEAD"),
                                      "status_porcelain": git_command("status", "--porcelain"),
                                      "scope": "harness checkout, not proof of either binary's build source"}
    except (OSError, subprocess.SubprocessError) as exc:
        result["harness_checkout"] = {"error": str(exc)}
    return result


def run(args, output, engine_factory=chess.engine.SimpleEngine.popen_uci):
    document = {"schema_version": 1, "started_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "purpose": "fixed-depth search cost; not an Elo or playing-strength measurement",
                "depth": args.depth, "repeats": args.repeats, "seed": args.seed,
                "table_policy": "fresh ucinewgame per sample; cold search table; no persistent-game reuse",
                "timing_policy": "wall includes UCI play transaction and game reset; process startup excluded",
                "order_policy": "seeded shuffle of positions per repetition; engine order alternates for each position",
                "allow_score_differences": args.allow_score_differences,
                "environment": environment(), "engines": {},
                "positions_file": {"path": str(args.positions), "sha256": digest(args.positions),
                                   "document": args.position_document},
                "records": [], "error": None, "exit_code": None}
    engines = {}

    def save():
        document["summary"] = summarize(document["records"], args.selected_positions)
        output.seek(0)
        json.dump(document, output, ensure_ascii=False, indent=2)
        output.write("\n")
        output.truncate()
        output.flush()

    try:
        for role in ROLES:
            binary = getattr(args, role)
            options = getattr(args, role + "_options")
            engine = engine_factory([str(binary), "uci"])
            engines[role] = engine
            for name in options:
                if name not in engine.options:
                    raise ValueError(f"{role} engine does not advertise option {name}")
            engine.configure(options)
            document["engines"][role] = {"path": str(binary), "sha256": digest(binary),
                                          "declared_source_commit": getattr(args, role + "_commit"),
                                          "id": dict(engine.id), "configured_options": options,
                                          "advertised_options": {name: {
                                              "type": item.type, "default": item.default,
                                              "min": item.min, "max": item.max, "var": item.var}
                                              for name, item in engine.options.items()}}
        save()
        for repetition, index, order in schedule(args.selected_positions, args.repeats, args.seed):
            position = args.selected_positions[index]
            record = {"repetition": repetition, "position_index": index, "position": position["name"],
                      "order": order, "scores_equal": None}
            document["records"].append(record)
            for role in order:
                record[role] = sample(engines[role], position, args.depth)
            record["scores_equal"] = record["baseline"]["score"] == record["candidate"]["score"]
            if not record["scores_equal"]:
                print(f"SCORE MISMATCH repetition={repetition} position={position['name']}: "
                      f"baseline={record['baseline']['score']} candidate={record['candidate']['score']}",
                      file=sys.stderr, flush=True)
            save()
            print(f"pair {len(document['records'])}/{len(args.selected_positions) * args.repeats}: "
                  f"{position['name']} scores_equal={record['scores_equal']}", flush=True)
        mismatches = document["summary"]["score_mismatches"]
        document["exit_code"] = 1 if mismatches and not args.allow_score_differences else 0
    except BaseException as exc:
        document["error"] = f"{type(exc).__name__}: {exc}"
        document["exit_code"] = 130 if isinstance(exc, KeyboardInterrupt) else 2
        print(document["error"], file=sys.stderr, flush=True)
    finally:
        for engine in engines.values():
            try:
                engine.quit()
            except Exception as exc:
                if document["error"] is None:
                    document["error"] = f"engine shutdown: {type(exc).__name__}: {exc}"
                    document["exit_code"] = 2
        document["finished_utc"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        save()
    return document


def main(argv=None):
    args = arguments(argv)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation protects against overwrite races after argument validation.
    with args.output.open("w" if args.overwrite else "x", encoding="utf-8") as output:
        document = run(args, output)
    print(json.dumps(document["summary"], ensure_ascii=False, indent=2))
    return document["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
