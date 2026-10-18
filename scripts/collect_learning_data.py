"""Collect reproducible, finite, Regicide-only self-play learning evidence.

The tiny initial corpus is an engineering experiment, not a strength claim.
Games stopped at a ply cap remain unfinished and never supply outcome labels.
Predefined opening-family splits are never selected from observed results.
No human games or external-engine labels are read. Each teacher query sends
the complete history with a fresh logical UCI game and a fixed node budget.
"""

import argparse
from collections import Counter, defaultdict
import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import random
import subprocess
import sys
import time

import chess
import chess.engine
import chess.pgn


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OPENINGS = ROOT / "tests/data/learning_openings_v1.json"
SCHEMA = "regicide.selfplay.dataset.v1"
SPLITS = ("train", "validation", "test")
MATERIAL = {chess.PAWN: 100, chess.KNIGHT: 320, chess.BISHOP: 330,
            chess.ROOK: 500, chess.QUEEN: 900, chess.KING: 0}
OPTIONS = {"Hash": 16, "Move Overhead": 0, "UsePositionalEvaluation": True,
           "UseQuiescence": True, "UseTranspositionTable": True,
           "UseFastMoves": True, "UseQuietMoveOrdering": True,
           "UseQuiescenceChecks": True, "PVS": True, "UseSEE": True,
           "UsePawnShield": True, "UsePawnStructure": True, "UseLMR": True}


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def positive_int(text):
    value = int(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return value


def replay_position(initial_fen, history):
    if not isinstance(initial_fen, str) or len(initial_fen.split()) != 6:
        raise ValueError("initial_fen requires six FEN fields")
    board = chess.Board(initial_fen)
    if not board.is_valid():
        raise ValueError("invalid initial position")
    if not isinstance(history, list):
        raise ValueError("history must be a list")
    for text in history:
        if board.outcome(claim_draw=True) is not None:
            raise ValueError("history continues after a terminal/claimable position")
        if not isinstance(text, str):
            raise ValueError("history moves must be UCI strings")
        move = chess.Move.from_uci(text)
        if move not in board.legal_moves:
            raise ValueError(f"illegal history move: {text}")
        board.push(move)
    return board


def canonical_position_key(board):
    """No clocks; include legal en-passant, side to move and castling rights."""
    return " ".join(board.fen(en_passant="legal").split()[:4])


def neural_feature_key(board):
    """Exact nonzero indices of the side-relative 768-feature model input.

    Channels are own PNBRQK, then opponent PNBRQK. Black-to-move squares
    are rank mirrored (square XOR 56). Rights, en-passant and clocks are
    absent from this first model and therefore cannot distinguish inputs.
    """
    return tuple(sorted((piece.piece_type - 1 + (0 if piece.color == board.turn else 6)) * 64 +
                        (square if board.turn == chess.WHITE else square ^ 56)
                        for square, piece in board.piece_map().items()))


def material_cp_stm(board):
    white = sum(value * (len(board.pieces(kind, chess.WHITE)) -
                         len(board.pieces(kind, chess.BLACK)))
                for kind, value in MATERIAL.items())
    return white if board.turn else -white


def load_openings(path):
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if data.get("schema") != "regicide.learning.openings.v1":
        raise ValueError("unsupported opening schema")
    openings = data.get("openings")
    if not isinstance(openings, list) or not openings:
        raise ValueError("a nonempty openings list is required")
    ids, family_splits = set(), {}
    for opening in openings:
        if not isinstance(opening, dict):
            raise ValueError("opening must be an object")
        for key in ("id", "name", "family"):
            if not isinstance(opening.get(key), str) or not opening[key].strip():
                raise ValueError(f"opening {key} must be nonempty")
        if opening["id"] in ids:
            raise ValueError("duplicate opening id")
        ids.add(opening["id"])
        split = opening.get("split")
        if split not in SPLITS:
            raise ValueError("opening split must be train, validation or test")
        prior = family_splits.setdefault(opening["family"], split)
        if prior != split:
            raise ValueError("one opening family cannot span splits")
        board = replay_position(opening.get("initial_fen", chess.STARTING_FEN),
                                opening.get("moves"))
        if board.outcome(claim_draw=True) is not None:
            raise ValueError("opening is already terminal/claimable")
    return data


def seeded_jobs(openings, games_per_opening, seed):
    rng = random.Random(seed)
    jobs = [{"opening": opening, "repeat": repeat, "seed": rng.getrandbits(64)}
            for opening in openings for repeat in range(games_per_opening)]
    rng.shuffle(jobs)
    return jobs


def diversify_opening(opening, seed, random_plies=2):
    history = list(opening["moves"])
    board = replay_position(opening.get("initial_fen", chess.STARTING_FEN), history)
    rng = random.Random(seed)
    random_moves = []
    for _ in range(random_plies):
        # Opening augmentation may be unwise, but cannot itself adjudicate a game.
        candidates = []
        for move in sorted(board.legal_moves, key=lambda item: item.uci()):
            board.push(move)
            if board.outcome(claim_draw=True) is None:
                candidates.append(move)
            board.pop()
        if not candidates:
            raise ValueError("no nonterminal random opening continuation")
        move = rng.choice(candidates)
        board.push(move)
        random_moves.append(move.uci())
        history.append(move.uci())
    return board, history, random_moves


def sample_exclusion(board):
    if board.outcome(claim_draw=False) is not None:
        return "terminal"
    if board.can_claim_draw():
        return "claimable_draw"
    if board.is_check():
        return "root_in_check"
    return None


def parse_score_event(line):
    """Capture bounds with their score, never accumulated stale UCI flags."""
    tokens = line.split()
    if not tokens or tokens[0] != "info":
        return None
    fields = tokens[:tokens.index("string")] if "string" in tokens else tokens
    if "score" not in fields:
        return None
    if "multipv" in fields and int(fields[fields.index("multipv") + 1]) != 1:
        return None
    index = fields.index("score")
    if index + 2 >= len(fields) or fields[index + 1] not in ("cp", "mate"):
        raise ValueError("invalid UCI score event")
    bound = fields[index + 3] if index + 3 < len(fields) else None
    if (bound in ("upperbound", "lowerbound") and index + 4 < len(fields) and
            fields[index + 4] in ("upperbound", "lowerbound")):
        raise ValueError("conflicting UCI score bounds")
    return {"kind": fields[index + 1], "value": int(fields[index + 2]),
            "upperbound": bound == "upperbound", "lowerbound": bound == "lowerbound",
            "raw_info": line}


class LearningProtocol(chess.engine.UciProtocol):
    async def play(self, board, limit, **kwargs):
        self.last_score_event = None
        self.score_error = None
        result = await super().play(board, limit, **kwargs)
        if self.score_error:
            raise ValueError(self.score_error)
        return result

    def line_received(self, line):
        try:
            event = parse_score_event(line)
            if event is not None:
                self.last_score_event = event
        except (ValueError, IndexError, TypeError) as exc:
            self.score_error = f"raw UCI score capture failed: {exc}"
        super().line_received(line)


class LearningEngine(chess.engine.SimpleEngine):
    @classmethod
    def popen_uci(cls, command, *, timeout=600, **kwargs):
        return cls.popen(LearningProtocol, command, timeout=timeout, **kwargs)

    def _timeout_for(self, limit):
        # Keep a communication watchdog for node-only searches. No movetime.
        return self.timeout


def exact_cp_info(board, info, raw_event):
    """Return an eligible completed cp label or a documented exclusion reason."""
    if raw_event is None or info.get("score") is None:
        return None, "missing_score"
    score = info["score"].pov(board.turn)
    if raw_event["kind"] == "mate":
        if score.mate() != raw_event["value"]:
            raise ValueError("raw and parsed mate scores disagree")
        return None, "mate_score"
    if score.score() != raw_event["value"] or score.mate() is not None:
        raise ValueError("raw and parsed cp scores disagree")
    if raw_event["upperbound"] or raw_event["lowerbound"]:
        return None, "bound_score"
    depth, nodes = info.get("depth"), info.get("nodes")
    if not isinstance(depth, int) or depth < 1:
        return None, "no_completed_depth"
    if not isinstance(nodes, int) or nodes < 1:
        return None, "missing_nodes"
    pv = info.get("pv")
    if not isinstance(pv, list) or not pv:
        return None, "missing_pv"
    probe = board.copy(stack=True)
    for move in pv:
        if move not in probe.legal_moves:
            raise ValueError("illegal teacher PV")
        if probe.outcome(claim_draw=False) is not None:
            raise ValueError("teacher PV continues after automatic terminal position")
        probe.push(move)
    return {"teacher_cp_stm": score.score(), "teacher_depth": depth,
            "teacher_nodes": nodes, "teacher_pv": [move.uci() for move in pv],
            "teacher_score_raw_info": raw_event["raw_info"]}, None


def outcome_stm(result, turn):
    if result == "1/2-1/2":
        return 0
    if result not in ("1-0", "0-1"):
        raise ValueError("unfinished games cannot supply outcome labels")
    white_won = result == "1-0"
    return 1 if white_won == (turn == "w") else -1


def remove_cross_split_positions(positions):
    groups, feature_groups = defaultdict(set), defaultdict(set)
    position_keys = []
    for row in positions:
        board = replay_position(row["initial_fen"], row["history"])
        if board.fen(en_passant="fen") != row["fen"]:
            raise ValueError("sample history and FEN disagree")
        board_key, feature_key = canonical_position_key(board), neural_feature_key(board)
        groups[board_key].add(row["split"])
        feature_groups[feature_key].add(row["split"])
        position_keys.append((board_key, feature_key))
    overlaps = {key for key, splits in groups.items() if len(splits) > 1}
    feature_overlaps = {key for key, splits in feature_groups.items() if len(splits) > 1}
    filtered = [row for row, (board_key, feature_key) in zip(positions, position_keys)
                if board_key not in overlaps and feature_key not in feature_overlaps]
    return filtered, {"canonical_groups": len(overlaps),
                      "neural_feature_groups": len(feature_overlaps),
                      "canonical_excluded_positions": sum(board_key in overlaps for board_key, _ in position_keys),
                      "neural_feature_excluded_positions": sum(feature_key in feature_overlaps
                                                               for _, feature_key in position_keys),
                      "excluded_positions": len(positions) - len(filtered),
                      "policy": "exclude every occurrence of cross-split canonical positions or exact neural inputs from every split; never reassign families",
                      "neural_feature_key": "sorted channel*64+square; channels own PNBRQK then opponent PNBRQK; black turn square XOR 56; no rights/en-passant/clocks"}


def checkpoint_json(path, value):
    with Path(path).open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def write_jsonl(path, values):
    with Path(path).open("w", encoding="utf-8", newline="\n") as stream:
        for value in values:
            stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def reserve_output(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=False)
    return path


def checkout_provenance():
    try:
        def git(*args):
            return subprocess.run(["git", "-C", str(ROOT), *args], check=True,
                                  capture_output=True, text=True, timeout=10).stdout.strip()
        return {"commit": git("rev-parse", "HEAD"),
                "status_porcelain": git("status", "--porcelain"),
                "scope": "collector checkout only; not proof of executable build origin"}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"commit": None, "error": str(exc)}


def collect(args):
    opening_data = load_openings(args.openings)
    binary = args.binary.expanduser().resolve(strict=True)
    jobs = seeded_jobs(opening_data["openings"], args.games_per_opening, args.seed)
    output = reserve_output(args.output)
    exclusions = Counter()
    games, positions = [], []
    manifest = {"schema": SCHEMA, "status": "running", "started_utc": utc_now(),
                "positions_path": "positions.jsonl", "samples_path": "positions.jsonl",
                "games_path": "games.jsonl", "pgn_path": "games.pgn",
                "seed": args.seed, "games_requested": len(jobs),
                "limits": {"selfplay_nodes": args.nodes, "teacher_nodes": args.teacher_nodes,
                           "max_plies_total": args.max_plies, "sample_every": args.sample_every,
                           "random_opening_plies": 2, "communication_watchdog_seconds": 600,
                           "time_limit": None},
                "engine": {"binary_name": binary.name, "sha256": sha256_file(binary),
                           "source_commit_caller_declared": args.source_commit,
                           "build_source_verified": False, "configured_options": OPTIONS,
                           "selection": "Regicide only; identical binary for both colors and teacher"},
                "collector": {"sha256": sha256_file(__file__), "checkout": checkout_provenance(),
                              "python": sys.version, "python_chess": chess.__version__,
                              "platform": platform.platform()},
                "openings": {"name": args.openings.name, "sha256": sha256_file(args.openings),
                             "definition": opening_data},
                "label_policy": {"teacher": "own Regicide exact cp, side-to-move perspective; no mate conversion",
                                 "outcome": "completed adjudicated games only; side-to-move perspective",
                                 "draws": "claim every available current or announced-next-move draw",
                                 "sampling": "post-opening+random augmentation, every N engine plies; root check/claim/terminal excluded",
                                 "teacher_context": "fresh logical UCI game per query; full history; no time budget",
                                 "known_limit": "teacher imitates existing engine; outcome is small self-play evidence, not an independent truth"},
                "errors": []}
    pgn_games = []

    def save():
        filtered, leakage = remove_cross_split_positions(positions)
        write_jsonl(output / "games.jsonl", games)
        write_jsonl(output / "positions.jsonl", filtered)
        (output / "games.pgn").write_text("\n\n".join(pgn_games) + ("\n" if pgn_games else ""),
                                          encoding="utf-8", newline="\n")
        manifest.update({"checkpoint_utc": utc_now(), "games_finished": len(games),
                         "completed_games": sum(game["completed"] for game in games),
                         "unfinished_games": sum(not game["completed"] for game in games),
                         "positions": len(filtered), "positions_by_split": dict(Counter(row["split"] for row in filtered)),
                         "games_by_split": dict(Counter(game["split"] for game in games)),
                         "exclusions": dict(exclusions), "cross_split_exclusions": leakage,
                         "files_sha256": {name: sha256_file(output / name)
                                          for name in ("games.jsonl", "positions.jsonl", "games.pgn")}})
        checkpoint_json(output / "manifest.json", manifest)

    save()
    try:
        with LearningEngine.popen_uci([str(binary), "uci"], timeout=600) as player, \
                LearningEngine.popen_uci([str(binary), "uci"], timeout=600) as teacher:
            for engine in (player, teacher):
                if not engine.id.get("name", "").startswith("Regicide "):
                    raise ValueError("collection requires a Regicide UCI binary")
                missing = set(OPTIONS) - set(engine.options)
                if missing:
                    raise ValueError(f"Regicide omitted required options: {sorted(missing)}")
                engine.configure(OPTIONS)
                # A neural-enabled development binary must still generate this
                # first corpus with its existing handcrafted teacher.
                if "UseNeuralEvaluation" in engine.options:
                    engine.configure({"UseNeuralEvaluation": False})
            manifest["engine"]["uci_id"] = player.id
            manifest["engine"]["optional_options"] = {
                "UseNeuralEvaluation": False if "UseNeuralEvaluation" in player.options else "not advertised"}
            manifest["engine"]["advertised_options"] = {
                name: {"type": option.type, "default": option.default,
                       "min": option.min, "max": option.max, "var": option.var}
                for name, option in player.options.items()}
            save()
            for index, job in enumerate(jobs, 1):
                opening = job["opening"]
                game_id = f"g{index:04d}-{opening['id']}-r{job['repeat'] + 1}"
                board, history, random_moves = diversify_opening(opening, job["seed"])
                initial_fen = opening.get("initial_fen", chess.STARTING_FEN)
                opening_length = len(history)
                manifest["active_game"] = {"game_id": game_id, "opening_id": opening["id"],
                                           "family": opening["family"], "split": opening["split"],
                                           "seed": job["seed"], "initial_fen": initial_fen,
                                           "history": history, "result": "*", "completed": False}
                pending, search_records = [], []
                game_context = object()
                last_progress = time.monotonic()
                print(f"game {index}/{len(jobs)} {game_id} {opening['split']} start", flush=True)
                while len(history) < args.max_plies and board.outcome(claim_draw=True) is None:
                    if (len(history) - opening_length) % args.sample_every == 0:
                        reason = sample_exclusion(board)
                        if reason:
                            exclusions[reason] += 1
                        else:
                            pending.append({"history": list(history), "fen": board.fen(en_passant="fen"),
                                            "ply": len(history), "turn": "w" if board.turn else "b",
                                            "material_cp_stm": material_cp_stm(board)})
                    result = player.play(board, chess.engine.Limit(nodes=args.nodes),
                                         game=game_context, info=chess.engine.INFO_ALL)
                    if result.move is None or result.move not in board.legal_moves:
                        raise ValueError(f"{game_id}: engine returned no legal move")
                    cp_label, label_reason = exact_cp_info(board, result.info, player.protocol.last_score_event)
                    search_records.append({"ply": len(history), "move": result.move.uci(),
                                           "depth": result.info.get("depth"), "nodes": result.info.get("nodes"),
                                           "cp_stm": cp_label["teacher_cp_stm"] if cp_label else None,
                                           "score_exclusion": label_reason,
                                           "raw_score_event": player.protocol.last_score_event})
                    board.push(result.move)
                    history.append(result.move.uci())
                    if time.monotonic() - last_progress >= 20:
                        print(f"game {index}/{len(jobs)} ply {len(history)}", flush=True)
                        last_progress = time.monotonic()
                outcome = board.outcome(claim_draw=True)
                completed = outcome is not None
                result_text = outcome.result() if completed else "*"
                row = {"game_id": game_id, "opening_id": opening["id"], "family": opening["family"],
                       "split": opening["split"], "seed": job["seed"], "initial_fen": initial_fen,
                       "opening_moves": opening["moves"], "random_opening_moves": random_moves,
                       "history": history, "fen": board.fen(en_passant="fen"),
                       "result": result_text, "completed": completed,
                       "termination": outcome.termination.name if completed else "PLY_CAP_UNFINISHED",
                       "selfplay_searches": search_records, "pending_samples": len(pending),
                       "accepted_teacher_samples": 0}
                if not completed:
                    exclusions["unfinished_game_positions"] += len(pending)
                else:
                    for sample_index, sample in enumerate(pending):
                        sample_board = replay_position(initial_fen, sample["history"])
                        teacher_result = teacher.play(sample_board, chess.engine.Limit(nodes=args.teacher_nodes),
                                                      game=object(), info=chess.engine.INFO_ALL)
                        if teacher_result.move is None or teacher_result.move not in sample_board.legal_moves:
                            raise ValueError(f"{game_id}: teacher returned no legal move")
                        label, reason = exact_cp_info(sample_board, teacher_result.info,
                                                      teacher.protocol.last_score_event)
                        if reason:
                            exclusions[f"teacher_{reason}"] += 1
                            continue
                        if teacher_result.move.uci() != label["teacher_pv"][0]:
                            raise ValueError("teacher bestmove and PV differ")
                        positions.append({"game_id": game_id, "opening_id": opening["id"],
                                          "family": opening["family"], "split": opening["split"],
                                          "initial_fen": initial_fen, **sample, **label,
                                          "outcome_stm": outcome_stm(result_text, sample["turn"])})
                        row["accepted_teacher_samples"] += 1
                        if time.monotonic() - last_progress >= 20:
                            print(f"game {index}/{len(jobs)} teacher {sample_index + 1}/{len(pending)}", flush=True)
                            last_progress = time.monotonic()
                game = chess.pgn.Game.from_board(board)
                game.headers.update({"Event": "Regicide learning self-play v1", "White": "Regicide",
                                     "Black": "Regicide", "Result": result_text, "GameId": game_id,
                                     "OpeningFamily": opening["family"], "Split": opening["split"],
                                     "Termination": row["termination"]})
                pgn_games.append(str(game))
                games.append(row)
                manifest.pop("active_game", None)
                save()
                print(f"game {index}/{len(jobs)} {result_text} {row['termination']} samples {row['accepted_teacher_samples']}",
                      flush=True)
        manifest.update({"status": "complete", "finished_utc": utc_now()})
        save()
        return manifest
    except BaseException as exc:
        manifest.update({"status": "failed", "finished_utc": utc_now()})
        manifest["errors"].append({"type": type(exc).__name__, "message": str(exc)})
        save()
        raise


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="new directory; existing outputs are refused")
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--games-per-opening", type=positive_int, required=True)
    parser.add_argument("--nodes", type=positive_int, required=True)
    parser.add_argument("--max-plies", type=positive_int, required=True)
    parser.add_argument("--sample-every", type=positive_int, required=True)
    parser.add_argument("--teacher-nodes", type=positive_int, required=True)
    parser.add_argument("--openings", type=Path, default=DEFAULT_OPENINGS)
    parser.add_argument("--source-commit", help="caller-declared build source; recorded, not inferred from bytes")
    return parser.parse_args(argv)


def main(argv=None):
    try:
        result = collect(parse_args(argv))
        print(f"complete: {result['completed_games']} completed games, {result['positions']} eligible positions", flush=True)
        return 0
    except Exception as exc:
        print(f"collection failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
