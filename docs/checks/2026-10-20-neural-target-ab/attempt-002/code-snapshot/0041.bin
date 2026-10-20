"""Run fixed-opening, paired-color UCI matches and preserve replayable evidence.

Only the supplied binaries select moves. python-chess independently checks legality
and adjudicates the game. Default policy: always claim available threefold/50-move
draws (including a claim by announcing the next move); automatic policy does not
claim them. The ply cap is an unfinished '*' game, never a draw.

The backtrack metric counts adjacent engine-selected moves by the same side which
are both quiet non-pawn moves, neither made while in check. Each such transition
is one opportunity; reversing A->B to B->A with the same piece is one backtrack.
Opening moves, captures, pawn moves and check evasions do not contribute. This
observable metric does not establish that a reversal was strategically unnecessary.
"""

import argparse
import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import random
import re
import subprocess
import sys
import tempfile
import time

import chess
import chess.engine
import chess.pgn


ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_SEED = 20261017
BOOTSTRAP_SAMPLES = 20000


def positive_int(text):
    value = int(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


def git_command(*arguments):
    return subprocess.run(["git", "-C", str(ROOT), *arguments], capture_output=True,
                          text=True, check=True, timeout=10).stdout.strip()


def verified_commit(text):
    """Verify an explicit full object ID; do not guess a binary's build provenance."""
    if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", text):
        raise ValueError("source commits must be full 40/64-character hexadecimal object IDs")
    resolved = git_command("rev-parse", "--verify", text + "^{commit}")
    if resolved.lower() != text.lower():
        raise ValueError(f"source ID is not a commit object: {text}")
    return resolved


def checkout_provenance():
    try:
        return {"commit": git_command("rev-parse", "HEAD"),
                "status_porcelain": git_command("status", "--porcelain"),
                "scope": "harness checkout at match start; not proof of either binary's build source"}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"commit": None, "error": str(exc)}


def load_openings(path, count=None):
    contents = json.loads(path.read_text(encoding="utf-8"))
    openings = contents["openings"]
    if not isinstance(openings, list) or not openings:
        raise ValueError("opening file must contain a nonempty openings list")
    if count is not None and count > len(openings):
        raise ValueError("--opening-count exceeds the declared opening list")
    names = set()
    for opening in openings:
        if not isinstance(opening["name"], str) or not opening["name"] or opening["name"] in names:
            raise ValueError("opening names must be nonempty and unique")
        names.add(opening["name"])
        if not isinstance(opening["moves"], list):
            raise ValueError("opening moves must be a list of UCI strings")
        board = chess.Board()
        for text in opening["moves"]:
            move = chess.Move.from_uci(text)
            if board.outcome(claim_draw=True) is not None or move not in board.legal_moves:
                raise ValueError(f"illegal or post-terminal opening move {text} in {opening['name']}")
            board.push(move)
        if board.outcome(claim_draw=True) is not None:
            raise ValueError(f"opening is already terminal: {opening['name']}")
        opening["fen_after_opening"] = board.fen(en_passant="fen")
    return contents, openings[:count] if count is not None else openings


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--pgn", type=Path, required=True)
    limits = parser.add_mutually_exclusive_group()
    limits.add_argument("--nodes", type=positive_int, help="search nodes per move; default 1000")
    limits.add_argument("--movetime", type=positive_int, help="milliseconds per move")
    limits.add_argument("--depth", type=positive_int, help="nominal full-search depth")
    parser.add_argument("--openings", type=Path, default=ROOT / "tests/data/comparison_openings.json")
    parser.add_argument("--opening-count", type=positive_int, help="preselected prefix, before observing results")
    parser.add_argument("--max-plies", type=positive_int, default=500,
                        help="total ply cap including opening; unfinished '*' at the cap")
    parser.add_argument("--draw-policy", choices=("claim", "automatic"), default="claim")
    parser.add_argument("--baseline-commit", required=True)
    parser.add_argument("--candidate-commit", required=True)
    parser.add_argument("--candidate-options", default="{}", help="JSON mapping of advertised UCI options")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    args.baseline = args.baseline.expanduser().resolve()
    args.candidate = args.candidate.expanduser().resolve()
    args.openings = args.openings.expanduser().resolve()
    args.pgn = args.pgn.expanduser().resolve()
    args.json = args.pgn.with_suffix(".json")
    if args.nodes is None and args.movetime is None and args.depth is None:
        args.nodes = 1000
    try:
        args.baseline_commit = verified_commit(args.baseline_commit)
        args.candidate_commit = verified_commit(args.candidate_commit)
        args.candidate_options = json.loads(args.candidate_options)
        if not isinstance(args.candidate_options, dict):
            raise ValueError("--candidate-options must be a JSON object")
        args.opening_document, args.selected_openings = load_openings(args.openings, args.opening_count)
        if any(len(opening["moves"]) >= args.max_plies for opening in args.selected_openings):
            raise ValueError("--max-plies must leave at least one searched ply beyond every selected opening")
        if args.pgn == args.json:
            raise ValueError("PGN and JSON output paths must differ")
        for binary in (args.baseline, args.candidate):
            if not binary.is_file():
                raise ValueError(f"engine executable does not exist: {binary}")
        if {args.pgn, args.json} & {args.baseline, args.candidate, args.openings}:
            raise ValueError("output paths must not replace input files")
        if not args.overwrite:
            for path in (args.pgn, args.json):
                if path.exists():
                    raise ValueError(f"output already exists; use --overwrite: {path}")
    except (KeyError, TypeError, ValueError, OSError, subprocess.SubprocessError) as exc:
        parser.error(str(exc))
    return args


def new_record(number, pair, opening, candidate_white):
    return {"game": number, "pair": pair, "opening": opening["name"],
            "opening_uci_moves": list(opening["moves"]),
            "opening_fen": opening["fen_after_opening"],
            "white": "candidate" if candidate_white else "baseline",
            "black": "baseline" if candidate_white else "candidate",
            "initial_fen": chess.STARTING_FEN,
            "final_fen": opening["fen_after_opening"],
            "uci_moves": list(opening["moves"]), "plies": len(opening["moves"]),
            "search_moves": [], "result": "*", "termination": "not_started",
            "completed": False, "candidate_points": None, "elapsed_seconds": 0.0,
            "error": None, "backtracks": {role: {"count": 0, "opportunities": 0}
                                           for role in ("baseline", "candidate")}}


def score_record(info, color):
    score = info.get("score")
    if score is None:
        return None
    point = score.pov(color)
    return {"pov": "mover", "cp": point.score(), "mate": point.mate(),
            "lowerbound": bool(info.get("lowerbound", False)),
            "upperbound": bool(info.get("upperbound", False))}


def play_one(args, engines, record):
    board = chess.Board(record["initial_fen"])
    for text in record["opening_uci_moves"]:
        board.push_uci(text)
    limit = chess.engine.Limit(nodes=args.nodes, depth=args.depth,
                               time=args.movetime / 1000 if args.movetime else None)
    previous = {chess.WHITE: None, chess.BLACK: None}
    game_token = object()
    start = time.perf_counter()
    try:
        while True:
            outcome = board.outcome(claim_draw=args.draw_policy == "claim")
            if outcome is not None:
                result = outcome.result()
                points = 0.5 if outcome.winner is None else float(
                    (outcome.winner == chess.WHITE) == (record["white"] == "candidate"))
                record.update(result=result, termination=outcome.termination.name.lower(),
                              completed=True, candidate_points=points)
                return
            if record["plies"] >= args.max_plies:
                record["termination"] = "ply_cap"
                return
            color = board.turn
            role = record["white" if color == chess.WHITE else "black"]
            move_start = time.perf_counter()
            reply = engines[role].play(board, limit, game=game_token, info=chess.engine.INFO_ALL)
            elapsed = time.perf_counter() - move_start
            move = reply.move
            if move is None or move not in board.legal_moves:
                raise RuntimeError(f"{role} returned illegal move {move!s} in {board.fen(en_passant='fen')}")
            piece = board.piece_type_at(move.from_square)
            eligible = piece != chess.PAWN and not board.is_capture(move) and not board.is_check()
            prior = previous[color]
            opportunity = bool(eligible and prior is not None and prior["eligible"])
            reversal = bool(opportunity and piece == prior["piece"]
                            and move.from_square == prior["to"] and move.to_square == prior["from"])
            if opportunity:
                record["backtracks"][role]["opportunities"] += 1
            if reversal:
                record["backtracks"][role]["count"] += 1
            previous[color] = {"from": move.from_square, "to": move.to_square,
                               "piece": piece, "eligible": eligible}
            info = reply.info
            record["search_moves"].append({
                "ply": record["plies"] + 1, "role": role,
                "color": "white" if color else "black", "move": move.uci(),
                "fen_before": board.fen(en_passant="fen"), "elapsed_seconds": round(elapsed, 6),
                "nodes": info.get("nodes"), "depth": info.get("depth"),
                "seldepth": info.get("seldepth"), "engine_time_seconds": info.get("time"),
                "score": score_record(info, color),
                "pv": [item.uci() for item in info.get("pv", [])],
                "quiet_nonpawn_not_in_check": eligible,
                "backtrack_opportunity": opportunity, "immediate_backtrack": reversal})
            record["uci_moves"].append(move.uci())
            board.push(move)
            record.update(plies=len(record["uci_moves"]), final_fen=board.fen(en_passant="fen"))
    except BaseException as exc:
        record.update(termination="interrupted" if isinstance(exc, KeyboardInterrupt) else "engine_error",
                      error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        record["elapsed_seconds"] = round(time.perf_counter() - start, 6)
        record["resources"] = {role: resource_summary(
            [move for move in record["search_moves"] if move["role"] == role])
                               for role in ("baseline", "candidate")}


def resource_summary(moves):
    return {"searched_moves": len(moves),
            "reported_nodes": sum(move["nodes"] for move in moves if move["nodes"] is not None),
            "moves_without_node_report": sum(move["nodes"] is None for move in moves),
            "elapsed_seconds": round(sum(move["elapsed_seconds"] for move in moves), 6)}


def wilson_score_interval(points, count):
    if not count:
        return None
    z = 1.959963984540054
    proportion = points / count
    denominator = 1 + z * z / count
    center = (proportion + z * z / (2 * count)) / denominator
    radius = z * math.sqrt(proportion * (1 - proportion) / count
                          + z * z / (4 * count * count)) / denominator
    return [max(0.0, center - radius), min(1.0, center + radius)]


def summarize(records, openings):
    complete = [game for game in records if game["completed"]]
    points = sum(game["candidate_points"] for game in complete)
    pairs = []
    for index, opening in enumerate(openings, 1):
        games = [game for game in records if game["pair"] == index]
        done = len(games) == 2 and all(game["completed"] for game in games)
        pairs.append({"pair": index, "opening": opening["name"],
                      "games": [game["game"] for game in games], "completed": done,
                      "results": [game["result"] for game in games],
                      "candidate_points": sum(game["candidate_points"] for game in games) if done else None,
                      "candidate_score": sum(game["candidate_points"] for game in games) / 2 if done else None})
    pair_scores = [pair["candidate_score"] for pair in pairs if pair["completed"]]
    bootstrap = None
    if len(pair_scores) >= 2:
        generator = random.Random(BOOTSTRAP_SEED)
        samples = sorted(sum(generator.choices(pair_scores, k=len(pair_scores))) / len(pair_scores)
                         for _ in range(BOOTSTRAP_SAMPLES))
        bootstrap = {"interval": [samples[int(0.025 * (len(samples) - 1))],
                                  samples[int(0.975 * (len(samples) - 1))]],
                     "seed": BOOTSTRAP_SEED, "resamples": BOOTSTRAP_SAMPLES,
                     "completed_pairs": len(pair_scores),
                     "interpretation": "Descriptive paired-opening percentile bootstrap; small fixed opening sample, not a universal strength or Elo interval."}
    moves = [move for game in records for move in game["search_moves"]]
    backtracks = {}
    for role in ("baseline", "candidate"):
        count = sum(game["backtracks"][role]["count"] for game in records)
        opportunities = sum(game["backtracks"][role]["opportunities"] for game in records)
        backtracks[role] = {"count": count, "opportunities": opportunities,
                            "rate": count / opportunities if opportunities else None}
    return {"candidate_wins": sum(game["candidate_points"] == 1 for game in complete),
            "candidate_draws": sum(game["candidate_points"] == 0.5 for game in complete),
            "candidate_losses": sum(game["candidate_points"] == 0 for game in complete),
            "completed_games": len(complete), "incomplete_games": len(records) - len(complete),
            "unplayed_games": max(0, len(openings) * 2 - len(records)),
            "scheduled_games": len(openings) * 2, "attempted_games": len(records),
            "incomplete_scope": "incomplete_games counts attempted unfinished games; unplayed_games counts scheduled games never started after an error.",
            "candidate_points": points, "candidate_score": points / len(complete) if complete else None,
            "score_scope": "Finite match score among completed games only; no Elo estimate.",
            "wilson95_descriptive": wilson_score_interval(points, len(complete)),
            "wilson_interpretation": "Approximate descriptive Wilson interval with draws as half a success; independent Bernoulli assumptions do not hold for draws or paired openings and can overstate certainty.",
            "paired_opening_bootstrap95_descriptive": bootstrap, "pairs": pairs,
            "backtracks": backtracks,
            "resources": {role: resource_summary([move for move in moves if move["role"] == role])
                          for role in ("baseline", "candidate")}}


def binary_provenance(binary, commit, options):
    with binary.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"command": [str(binary), "uci"], "sha256": digest,
            "provided_source_commit": commit, "source_commit_object_verified": True,
            "source_provenance_note": "Commit is caller-declared and exists in this repository; the harness does not infer or prove the binary was built from it.",
            "requested_options": options, "engine_id": {}, "advertised_options": {}}


def run_match(args):
    start = time.perf_counter()
    metadata = {"schema_version": 1,
                "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "harness_checkout": checkout_provenance(),
                "engines": {"baseline": binary_provenance(args.baseline, args.baseline_commit, {}),
                            "candidate": binary_provenance(args.candidate, args.candidate_commit,
                                                           args.candidate_options)},
                "python_version": platform.python_version(), "chess_version": chess.__version__,
                "platform": platform.platform(), "machine": platform.machine(),
                "processor": platform.processor(), "logical_cpus": os.cpu_count(),
                "limits": {"nodes": args.nodes, "depth": args.depth, "movetime_ms": args.movetime},
                "max_plies": args.max_plies, "draw_policy": args.draw_policy,
                "draw_policy_detail": "Both players always claim an available threefold or 50-move draw, including an intended legal next move." if args.draw_policy == "claim" else "Automatic rule endings only; no optional claims.",
                "opening_source": str(args.openings),
                "opening_file_sha256": hashlib.sha256(args.openings.read_bytes()).hexdigest(),
                "opening_document": args.opening_document,
                "selected_openings": args.selected_openings,
                "opening_selection": "Ordered prefix fixed by invocation before results; both colors per opening.",
                "pair_order": "Each opening: candidate White, then candidate Black; openings in file order.",
                "backtrack_definition": "Adjacent engine-selected moves by the same side, both quiet non-pawn moves and neither a check evasion, form an opportunity. A->B then B->A by the same piece is a backtrack. Opening moves excluded. This is not a judgment that the reversal was unnecessary.",
                "move_selection": "Supplied binaries only; deterministic opening and color schedule; no runner move randomness.",
                "games_requested": len(args.selected_openings) * 2, "games": [], "errors": []}
    engines = {}
    code = 0
    # Record the first scheduled game before startup, preserving launch/configuration errors.
    metadata["games"].append(new_record(1, 1, args.selected_openings[0], True))
    try:
        for role in ("baseline", "candidate"):
            item = metadata["engines"][role]
            engine = chess.engine.SimpleEngine.popen_uci(item["command"], timeout=30.0)
            engines[role] = engine
            item["engine_id"] = dict(engine.id)
            item["advertised_options"] = {
                name: {"type": option.type, "default": option.default, "min": option.min,
                       "max": option.max, "var": option.var}
                for name, option in engine.options.items()}
            if item["requested_options"]:
                # Validate check values strictly: Python truthiness must not turn "false" into true.
                for name, value in item["requested_options"].items():
                    if name not in engine.options:
                        raise ValueError(f"{role} does not advertise UCI option {name}")
                    if engine.options[name].type == "check" and not isinstance(value, bool):
                        raise ValueError(f"UCI check option {name} requires a JSON boolean")
                engine.configure(item["requested_options"])
        number = 0
        for pair, opening in enumerate(args.selected_openings, 1):
            for candidate_white in (True, False):
                number += 1
                if number > 1:
                    metadata["games"].append(new_record(number, pair, opening, candidate_white))
                record = metadata["games"][-1]
                play_one(args, engines, record)
                print(f"game {number}/{metadata['games_requested']} {opening['name']}: "
                      f"{record['result']} ({record['termination']}), candidate "
                      f"{'White' if candidate_white else 'Black'}, {record['plies']} plies", flush=True)
                if not record["completed"]:
                    code = 2
    except (Exception, KeyboardInterrupt) as exc:
        code = 2
        message = f"{type(exc).__name__}: {exc}"
        metadata["errors"].append(message)
        record = metadata["games"][-1]
        if record["error"] is None:
            record.update(termination="interrupted" if isinstance(exc, KeyboardInterrupt) else "engine_error",
                          error=message)
        print(f"error: {message}", file=sys.stderr)
    finally:
        for role, engine in reversed(list(engines.items())):
            try:
                engine.quit()
            except Exception as exc:
                metadata["errors"].append(f"{role} cleanup: {type(exc).__name__}: {exc}")
                code = 2
            finally:
                engine.close()
    metadata["total_elapsed_seconds"] = round(time.perf_counter() - start, 6)
    metadata["completed"] = len(metadata["games"]) == metadata["games_requested"] and all(
        game["completed"] for game in metadata["games"])
    metadata["summary"] = summarize(metadata["games"], args.selected_openings)
    metadata["exit_code"] = code
    return metadata, code


def pgn_text(metadata):
    games = []
    for record in metadata["games"]:
        game = chess.pgn.Game()
        white = metadata["engines"][record["white"]]
        black = metadata["engines"][record["black"]]
        game.headers.update({
            "Event": "Regicide controlled paired comparison", "Site": "local",
            "Date": metadata["started_at"][:10].replace("-", "."), "Round": str(record["game"]),
            "White": f"{record['white']}: {white['engine_id'].get('name', 'unknown')}",
            "Black": f"{record['black']}: {black['engine_id'].get('name', 'unknown')}",
            "Result": record["result"], "Opening": record["opening"], "Pair": str(record["pair"]),
            "OpeningPlies": str(len(record["opening_uci_moves"])),
            "Termination": "normal" if record["completed"] else "unterminated",
            "TerminationReason": record["termination"], "DrawPolicy": metadata["draw_policy"],
            "WhiteSHA256": white["sha256"], "BlackSHA256": black["sha256"],
            "WhiteSourceCommit": white["provided_source_commit"],
            "BlackSourceCommit": black["provided_source_commit"],
            "RequestedNodes": str(metadata["limits"]["nodes"] or "-"),
            "RequestedDepth": str(metadata["limits"]["depth"] or "-"),
            "RequestedMoveTimeMS": str(metadata["limits"]["movetime_ms"] or "-")})
        node = game
        for move in record["uci_moves"]:
            node = node.add_variation(chess.Move.from_uci(move))
        if record["error"]:
            node.comment = record["error"].replace("{", "(").replace("}", ")")
        games.append(str(game))
    return "\n\n".join(games) + "\n\n"


def save_outputs(args, metadata):
    contents = [(args.pgn, pgn_text(metadata)),
                (args.json, json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")]
    staged, published = [], []
    try:
        for path, content in contents:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                             dir=path.parent, prefix=f".{path.name}.",
                                             suffix=".tmp", delete=False) as stream:
                temp = Path(stream.name)
                staged.append((path, temp))
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        for path, temp in staged:
            if args.overwrite:
                os.replace(temp, path)
            else:
                os.link(temp, path)
                published.append((path, temp))
    except BaseException:
        for path, temp in published:
            if path.exists() and temp.exists() and os.path.samefile(path, temp):
                path.unlink()
        raise
    finally:
        for _, temp in staged:
            temp.unlink(missing_ok=True)


def main(argv=None):
    args = arguments(argv)
    try:
        metadata, code = run_match(args)
        save_outputs(args, metadata)
    except (OSError, ValueError) as exc:
        print(f"error saving comparison evidence: {exc}", file=sys.stderr)
        return 2
    summary = metadata["summary"]
    print(f"candidate W/D/L: {summary['candidate_wins']}/{summary['candidate_draws']}/"
          f"{summary['candidate_losses']}; unfinished: {summary['incomplete_games']}")
    print(f"PGN: {args.pgn}\nJSON: {args.json}")
    return code


if __name__ == "__main__":
    sys.exit(main())
