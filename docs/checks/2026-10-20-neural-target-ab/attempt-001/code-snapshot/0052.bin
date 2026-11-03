"""Run two Regicide UCI processes and save independently checked self-play.

Only Regicide chooses moves. python-chess supplies UCI transport, independent
legality/adjudication checks, and PGN writing; it is not an opponent engine.
"""

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import time

import chess
import chess.engine
import chess.pgn


class GameError(RuntimeError):
    """An engine failed to supply a legal move in an ongoing game."""


def positive_int(text):
    value = int(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path, help="Regicide executable (started with 'uci')")
    parser.add_argument("--pgn", type=Path, required=True, help="output PGN; JSON uses the same stem")
    parser.add_argument("--depth", type=positive_int, help="search plies; default 2 when no limit is given")
    parser.add_argument("--nodes", type=positive_int, help="maximum search nodes per move")
    parser.add_argument("--movetime", type=positive_int, help="search time per move in milliseconds")
    parser.add_argument("--games", type=positive_int, default=1)
    parser.add_argument("--fen", default=chess.STARTING_FEN)
    parser.add_argument("--max-plies", type=positive_int, default=1000,
                        help="safeguard; reaching it leaves an incomplete '*' result")
    parser.add_argument("--draw-policy", choices=("claim", "automatic"), default="claim",
                        help="claim: both players always claim available 3-fold/50-move draws")
    parser.add_argument("--overwrite", action="store_true", help="explicitly replace existing outputs")
    args = parser.parse_args()
    args.binary = args.binary.expanduser().resolve()
    args.pgn = args.pgn.expanduser().resolve()
    args.json = args.pgn.with_suffix(".json")
    if not args.binary.is_file():
        parser.error(f"engine executable does not exist: {args.binary}")
    if args.pgn == args.json:
        parser.error("--pgn must have a filename distinct from its .json companion")
    if args.binary in (args.pgn, args.json):
        parser.error("output paths must not replace the engine executable")
    if not args.overwrite:
        for path in (args.pgn, args.json):
            if path.exists():
                parser.error(f"output already exists; use --overwrite to replace it: {path}")
    try:
        board = chess.Board(args.fen)
    except ValueError as exc:
        parser.error(f"invalid FEN: {exc}")
    if not board.is_valid():
        parser.error(f"invalid standard-chess position (status {board.status()})")
    args.fen = board.fen(en_passant="fen")
    if args.depth is None and args.nodes is None and args.movetime is None:
        args.depth = 2
    return args


def source_version():
    root = Path(__file__).resolve().parents[1]
    try:
        commit = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                                capture_output=True, text=True, check=True, timeout=10).stdout.strip()
        dirty = bool(subprocess.run(["git", "-C", str(root), "status", "--porcelain"],
                                    capture_output=True, text=True, check=True, timeout=10).stdout.strip())
        return {"commit": commit, "dirty": dirty,
                "label": commit + ("-dirty" if dirty else "")}
    except (OSError, subprocess.SubprocessError):
        return {"commit": None, "dirty": None, "label": "unknown"}


def new_record(args, number):
    return {"game": number, "initial_fen": args.fen, "final_fen": args.fen,
            "uci_moves": [], "plies": 0, "result": "*", "termination": "not_started",
            "completed": False, "elapsed_seconds": 0.0, "error": None}


def play_one(args, engines, record):
    board = chess.Board(args.fen)
    limit = chess.engine.Limit(depth=args.depth, nodes=args.nodes,
                               time=args.movetime / 1000 if args.movetime is not None else None)
    game_token = object()
    start = time.perf_counter()
    try:
        while True:
            outcome = board.outcome(claim_draw=args.draw_policy == "claim")
            if outcome is not None:
                record.update(result=outcome.result(), termination=outcome.termination.name.lower(),
                              completed=True)
                return
            if len(record["uci_moves"]) >= args.max_plies:
                record["termination"] = "ply_cap"
                return
            engine = engines["white" if board.turn == chess.WHITE else "black"]
            reply = engine.play(board, limit, game=game_token)
            # Board.push deliberately trusts its input, so verify before changing state.
            if reply.move is None or reply.move not in board.legal_moves:
                raise GameError(f"invalid engine move {reply.move!s} in {board.fen(en_passant='fen')}")
            record["uci_moves"].append(reply.move.uci())
            board.push(reply.move)
            record["plies"] = len(record["uci_moves"])
            record["final_fen"] = board.fen(en_passant="fen")
    except BaseException as exc:
        record.update(termination="interrupted" if isinstance(exc, KeyboardInterrupt) else "engine_error",
                      error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        record["elapsed_seconds"] = round(time.perf_counter() - start, 6)


def run_games(args):
    start = time.perf_counter()
    with args.binary.open("rb") as binary:
        digest = hashlib.file_digest(binary, "sha256").hexdigest()
    metadata = {
        "schema_version": 1,
        "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "engine_command": [str(args.binary), "uci"],
        "binary_sha256": digest,
        "source": source_version(),
        "python_version": platform.python_version(),
        "chess_version": chess.__version__,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "logical_cpus": os.cpu_count(),
        "limits": {"depth": args.depth, "nodes": args.nodes, "movetime_ms": args.movetime},
        "max_plies": args.max_plies,
        "draw_policy": args.draw_policy,
        "move_selection": "Regicide only; deterministic search, no runner randomness",
        "games_requested": args.games,
        "engine_ids": {},
        "games": [],
        "errors": [],
    }
    engines = {}
    code = 0
    try:
        # Create a record before startup so even a launch/protocol error leaves evidence.
        metadata["games"].append(new_record(args, 1))
        for color in ("white", "black"):
            engine = chess.engine.SimpleEngine.popen_uci([str(args.binary), "uci"], timeout=30.0)
            engines[color] = engine
            metadata["engine_ids"][color] = dict(engine.id)
        for number in range(1, args.games + 1):
            if number > 1:
                metadata["games"].append(new_record(args, number))
            record = metadata["games"][-1]
            play_one(args, engines, record)
            print(f"game {number}: {record['result']} ({record['termination']}); "
                  f"{record['plies']} plies", flush=True)
            if not record["completed"]:
                code = 2
    except (Exception, KeyboardInterrupt) as exc:
        code = 130 if isinstance(exc, KeyboardInterrupt) else 1
        message = f"{type(exc).__name__}: {exc}"
        metadata["errors"].append(message)
        record = metadata["games"][-1]
        if record["error"] is None:
            record.update(termination="interrupted" if code == 130 else "engine_error", error=message)
        print(f"error: {message}", file=sys.stderr)
    finally:
        for color, engine in reversed(list(engines.items())):
            try:
                engine.quit()
            except Exception as exc:
                # close() also terminates a crashed or unresponsive subprocess transport.
                metadata["errors"].append(f"{color} engine cleanup: {type(exc).__name__}: {exc}")
                if code == 0:
                    code = 1
            finally:
                engine.close()
        metadata["total_elapsed_seconds"] = round(time.perf_counter() - start, 6)
    metadata["completed"] = (len(metadata["games"]) == args.games
                              and all(game["completed"] for game in metadata["games"]))
    metadata["exit_code"] = code
    return metadata, code


def pgn_text(metadata):
    games = []
    date = metadata["started_at"][:10].replace("-", ".")
    for record in metadata["games"]:
        game = chess.pgn.Game()
        game.setup(chess.Board(record["initial_fen"]))
        game.headers.update({"Event": "Regicide self-play validation", "Site": "local",
                             "Date": date, "Round": str(record["game"]),
                             "White": metadata["engine_ids"].get("white", {}).get("name", "Regicide"),
                             "Black": metadata["engine_ids"].get("black", {}).get("name", "Regicide"),
                             "Result": record["result"],
                             "Termination": "normal" if record["completed"] else "unterminated",
                             "TerminationReason": record["termination"],
                             "DrawPolicy": metadata["draw_policy"],
                             "EngineSHA256": metadata["binary_sha256"],
                             "SourceVersion": metadata["source"]["label"]})
        node = game
        for move in record["uci_moves"]:
            node = node.add_variation(chess.Move.from_uci(move))
        if record["error"]:
            node.comment = record["error"].replace("{", "(").replace("}", ")")
        games.append(str(game))
    return "\n\n".join(games) + "\n\n"


def save_outputs(args, metadata):
    """Publish complete UTF-8 files atomically, refusing races without --overwrite."""
    contents = [(args.pgn, pgn_text(metadata)),
                (args.json, json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")]
    staged = []
    published = []
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
                # A hard link publishes the fully written file and fails atomically
                # if another process created the destination since argument parsing.
                os.link(temp, path)
                published.append((path, temp))
    except BaseException:
        # Roll back only our own newly created outputs; never delete another writer's file.
        for path, temp in published:
            if path.exists() and temp.exists() and os.path.samefile(path, temp):
                path.unlink()
        raise
    finally:
        for _, temp in staged:
            temp.unlink(missing_ok=True)


def main():
    args = arguments()
    try:
        metadata, code = run_games(args)
        # Engine cleanup finishes before either output becomes visible.
        save_outputs(args, metadata)
    except (OSError, ValueError) as exc:
        print(f"error saving game evidence: {exc}", file=sys.stderr)
        return 1
    print(f"PGN: {args.pgn}\nJSON: {args.json}")
    return code


if __name__ == "__main__":
    sys.exit(main())
