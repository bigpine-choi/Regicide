"""Paired synthetic-endgame development matches against the classical evaluator.

These fixed held-out starts measure a declared narrow development distribution.
They cannot select a candidate, modify a registry, or authorize model adoption.
Incomplete games or resource contamination invalidate the entire comparison.
"""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import sys
from types import SimpleNamespace

import chess
import chess.engine
import chess.pgn

import compare_engines as comparison
from collect_learning_data import OPTIONS
from learning_arena import (BROWSER_NAMES, GuardedEngine, LinuxProcesses,
                            utc_now, validate_native_model)
from see_clean_guard import DEFAULT_ENGINE_NAMES, EngineCpuGuard, WindowsProcesses
from train_neural import sha256, write_json


SCHEMA = "regicide.endgame.development-match.v1"
OPENINGS_SCHEMA = "regicide.endgame.development.openings.v1"
ROOT = Path(__file__).resolve().parents[1]


def load_openings(path, pairs=None):
    raw = Path(path).read_bytes()
    document = json.loads(raw.decode("utf-8"))
    if not isinstance(document, dict) or document.get("schema", OPENINGS_SCHEMA) != OPENINGS_SCHEMA:
        raise ValueError("unexpected development opening schema")
    positions = document.get("positions")
    if not isinstance(positions, list) or not positions:
        raise ValueError("positions must be a nonempty declared list")
    if pairs is None:
        pairs = len(positions)
    if type(pairs) is not int or not 1 <= pairs <= 16 or pairs > len(positions):
        raise ValueError("development matches require a declared prefix of 1 to 16 pairs")
    openings, names = [], set()
    for item in positions:
        if not isinstance(item, dict):
            raise ValueError("development position must be an object")
        name, fen, moves = item.get("name"), item.get("fen"), item.get("moves", [])
        if not isinstance(name, str) or not name or name in names:
            raise ValueError("development position names must be unique nonempty strings")
        if not isinstance(fen, str) or not isinstance(moves, list) or any(not isinstance(move, str) for move in moves):
            raise ValueError("development FEN and move history have invalid types")
        names.add(name)
        board = chess.Board(fen)
        if not board.is_valid() or board.outcome(claim_draw=True) is not None:
            raise ValueError("development FEN must be a legal nonterminal standard position")
        initial = board.fen(en_passant="fen")
        for uci in moves:
            move = chess.Move.from_uci(uci)
            if board.outcome(claim_draw=True) is not None or move not in board.legal_moves:
                raise ValueError("illegal or postterminal declared development history")
            board.push(move)
        if board.outcome(claim_draw=True) is not None:
            raise ValueError("development history is already terminal")
        openings.append({"name": name, "fen": initial, "moves": list(moves),
                         "fen_after_opening": board.fen(en_passant="fen")})
    return {"document": document, "sha256": hashlib.sha256(raw).hexdigest(),
            "selection": "fixed declared FEN-list prefix before all model results; paired colors; no result-dependent replacement",
            "openings": openings[:pairs]}


def _pgn(metadata):
    records = []
    for record in metadata["games"]:
        game = chess.pgn.Game()
        game.setup(chess.Board(record["initial_fen"]))
        game.headers.update(Event="Regicide synthetic endgame development match", Site="local",
            Date=metadata["started_utc"][:10].replace("-", "."), Round=str(record["game"]),
            Result=record["result"], White=record["white"], Black=record["black"],
            Pair=str(record["pair"]), Opening=record["opening"],
            TerminationReason=record["termination"], DrawPolicy="claim",
            RequestedMoveTimeMS=str(metadata["movetime_ms"]),
            BinarySHA256=metadata["binary_sha256"], CandidateModelSHA256=metadata["model_sha256"])
        node = game
        for move in record["uci_moves"]:
            node = node.add_variation(chess.Move.from_uci(move))
        records.append(str(game))
    return "\n\n".join(records) + "\n"


def verify_records(metadata):
    """Independently reconstruct every rule result and its resource evidence."""
    if (metadata.get("schema") != SCHEMA or metadata.get("errors")
            or not metadata.get("completed") or metadata.get("draw_policy") != "claim"):
        raise ValueError("development match has errors, unfinished games or wrong schema")
    guard = metadata.get("resource_guard", {})
    if guard.get("contaminated") or not guard.get("valid_for_comparison"):
        raise ValueError("development resource guard did not pass")
    ms = metadata.get("movetime_ms")
    if (type(ms) is not int or ms < 1 or metadata.get("hash_mb") != 16
            or metadata.get("threads") != 1):
        raise ValueError("invalid equal-time development resource settings")
    allowance = ms / 1000 + max(.1, ms / 1000)
    if metadata.get("move_response_allowance_seconds") != allowance:
        raise ValueError("development response allowance differs from fixed policy")
    openings = metadata["opening_data"]["openings"]
    games = metadata["games"]
    if (len(openings) != metadata.get("requested_pairs") or not 1 <= len(openings) <= 16
            or len(games) != len(openings) * 2):
        raise ValueError("development pair/game count mismatch")
    points = []
    for index, opening in enumerate(openings):
        for color_index, record in enumerate(games[2 * index:2 * index + 2]):
            candidate_white = color_index == 0
            white = "candidate" if candidate_white else "baseline"
            if (record.get("game") != 2 * index + color_index + 1 or record.get("pair") != index + 1
                    or record.get("white") != white or record.get("black") != ("baseline" if candidate_white else "candidate")
                    or record.get("opening") != opening["name"] or record.get("initial_fen") != opening["fen"]
                    or record.get("opening_uci_moves") != opening["moves"]
                    or record.get("opening_fen") != opening["fen_after_opening"]
                    or record.get("uci_moves", [])[:len(opening["moves"])] != opening["moves"]
                    or record.get("error") is not None or not record.get("completed")):
                raise ValueError("invalid paired development schedule or initial history")
            board = chess.Board(record["initial_fen"])
            if not board.is_valid() or board.outcome(claim_draw=True) is not None:
                raise ValueError("development initial FEN is invalid or terminal")
            opening_board = board.copy(stack=True)
            for uci in opening["moves"]:
                move = chess.Move.from_uci(uci)
                if opening_board.outcome(claim_draw=True) is not None or move not in opening_board.legal_moves:
                    raise ValueError("development declared history is illegal or postterminal")
                opening_board.push(move)
            if (opening_board.outcome(claim_draw=True) is not None
                    or opening_board.fen(en_passant="fen") != opening["fen_after_opening"]):
                raise ValueError("development declared opening FEN disagrees with history")
            searched = record.get("search_moves", [])
            if len(searched) != len(record["uci_moves"]) - len(opening["moves"]):
                raise ValueError("development move resource evidence missing")
            for ply, uci in enumerate(record["uci_moves"]):
                if board.outcome(claim_draw=True) is not None:
                    raise ValueError("development game continues after a chess-rule ending")
                if ply >= len(opening["moves"]):
                    evidence = searched[ply - len(opening["moves"])]
                    role = record["white"] if board.turn else record["black"]
                    elapsed = evidence.get("elapsed_seconds")
                    if (evidence.get("move") != uci or evidence.get("role") != role
                            or evidence.get("fen_before") != board.fen(en_passant="fen")
                            or isinstance(elapsed, bool) or not isinstance(elapsed, (float, int))
                            or not math.isfinite(elapsed) or elapsed < 0 or elapsed > allowance):
                        raise ValueError("development move timing, role or position evidence mismatch")
                move = chess.Move.from_uci(uci)
                if move not in board.legal_moves:
                    raise ValueError("development history contains an illegal move")
                board.push(move)
            outcome = board.outcome(claim_draw=True)
            if (outcome is None or record.get("result") != outcome.result()
                    or record.get("termination") != outcome.termination.name.lower()
                    or record.get("final_fen") != board.fen(en_passant="fen")
                    or record.get("plies") != len(record["uci_moves"])):
                raise ValueError("development result is not supported by its complete history")
            score = .5 if outcome.winner is None else float((outcome.winner == chess.WHITE) == candidate_white)
            if record.get("candidate_points") != score:
                raise ValueError("development score disagrees with chess rules")
            points.append(score)
    return {"candidate_wins": points.count(1), "candidate_draws": points.count(.5),
            "candidate_losses": points.count(0), "candidate_points": sum(points),
            "candidate_score": sum(points) / len(points), "complete_games": len(points),
            "complete_pairs": len(openings),
            "pair_scores": [(points[index] + points[index + 1]) / 2 for index in range(0, len(points), 2)],
            "scope": "fixed synthetic endgame development starts; small dependent sample, no Elo or universal strength claim; cannot authorize adoption"}


def run_matches(binary, model, openings_path, output, *, pairs=None, movetime_ms=100, max_plies=384):
    if any(type(value) is not int or value < 1 for value in (movetime_ms, max_plies)):
        raise ValueError("movetime and ply cap must be positive integers")
    binary, model, openings_path = (Path(path).resolve() for path in (binary, model, openings_path))
    output = Path(output).resolve()
    if output.exists():
        raise FileExistsError(output)
    openings = load_openings(openings_path, pairs)
    if any(len(opening["moves"]) >= max_plies for opening in openings["openings"]):
        raise ValueError("ply cap must leave at least one engine-selected move")
    hashes = {"binary": sha256(binary), "model": sha256(model), "openings": sha256(openings_path)}
    code_hashes = {name: sha256(ROOT / "scripts" / name) for name in (
        "endgame_matches.py", "compare_engines.py", "learning_arena.py", "see_clean_guard.py",
        "collect_learning_data.py", "train_neural.py")}
    output.mkdir(parents=True)
    snapshot = output / "candidate.nn"
    snapshot.write_bytes(model.read_bytes())
    (output / "openings.json").write_bytes(openings_path.read_bytes())
    if sha256(snapshot) != hashes["model"] or sha256(output / "openings.json") != hashes["openings"]:
        raise ValueError("development inputs changed while snapshotting")
    metadata = {"schema": SCHEMA, "status": "running", "started_utc": utc_now(),
                "finished_utc": None, "binary_sha256": hashes["binary"], "model_sha256": hashes["model"],
                "opening_data": openings, "requested_pairs": len(openings["openings"]),
                "implementation_sha256": code_hashes,
                "movetime_ms": movetime_ms, "hash_mb": 16, "threads": 1, "max_plies": max_plies,
                "move_response_allowance_seconds": movetime_ms / 1000 + max(.1, movetime_ms / 1000),
                "draw_policy": "claim", "native_model_validation": None,
                "host": {"platform": platform.platform(), "machine": platform.machine(),
                         "processor": platform.processor(), "logical_cpus": os.cpu_count(),
                         "python_chess": chess.__version__}, "engines": {}, "games": [],
                "errors": [], "completed": False, "resource_guard": {}, "summary": None,
                "valid_for_comparison": False,
                "adoption": "not attempted; development-only match, never a promotion gate or candidate-selection signal"}
    engines, guard = {}, None
    try:
        # The parser subprocess exits before native sampling starts, so exactly
        # two persistent transports participate in the guarded comparison.
        metadata["native_model_validation"] = validate_native_model(binary, snapshot)
        provider = WindowsProcesses(engine_names=DEFAULT_ENGINE_NAMES | BROWSER_NAMES) if sys.platform == "win32" \
            else LinuxProcesses() if sys.platform.startswith("linux") else None
        if provider is None:
            raise RuntimeError("native development guard supports Windows and Linux only")
        guard = EngineCpuGuard(provider, output / "resource-samples.jsonl", interval=.5)
        guard.start()
        for role, neural in (("baseline", False), ("candidate", True)):
            engine = chess.engine.SimpleEngine.popen_uci([str(binary), "uci"], timeout=10)
            engines[role] = engine
            guard.register_transport(engine.transport.get_pid(), role, [str(binary), "uci"])
            options = dict(OPTIONS, UseNeuralEvaluation=neural)
            if neural:
                options["EvalFile"] = str(snapshot)
            if any(name not in engine.options for name in options):
                raise ValueError("development engine is missing a required UCI option")
            if neural:
                engine.configure({"EvalFile": str(snapshot)})
            engine.configure({name: value for name, value in options.items() if name != "EvalFile"})
            engine.ping()
            metadata["engines"][role] = {"id": dict(engine.id), "options": options,
                                         "binary_sha256": hashes["binary"],
                                         "model_sha256": hashes["model"] if neural else None}
        guard.assert_transports()
        wrapped = {role: GuardedEngine(engine, guard, movetime_ms) for role, engine in engines.items()}
        args = SimpleNamespace(nodes=None, depth=None, movetime=movetime_ms,
                               max_plies=max_plies, draw_policy="claim")
        for pair, opening in enumerate(openings["openings"], 1):
            for candidate_white in (True, False):
                guard.sample("before-game")
                guard.assert_clean()
                record = comparison.new_record(len(metadata["games"]) + 1, pair, opening, candidate_white)
                record["initial_fen"] = opening["fen"]
                metadata["games"].append(record)
                comparison.play_one(args, wrapped, record)
                guard.sample("after-game")
                guard.assert_clean()
                if not record["completed"]:
                    raise RuntimeError("unfinished development game; entire comparison invalid")
                print(json.dumps({"development_game": len(metadata["games"]),
                                  "scheduled": len(openings["openings"]) * 2,
                                  "result": record["result"], "termination": record["termination"]}), flush=True)
        guard.assert_transports()
        guard.sample("after-all-games")
        guard.assert_clean()
        for name, path in (("binary", binary), ("model", model), ("openings", openings_path)):
            if sha256(path) != hashes[name]:
                raise ValueError("development frozen input changed: " + name)
        if any(sha256(ROOT / "scripts" / name) != expected for name, expected in code_hashes.items()):
            raise ValueError("development implementation changed during match")
        if sha256(snapshot) != hashes["model"] or sha256(output / "openings.json") != hashes["openings"]:
            raise ValueError("development snapshot changed during match")
        metadata["completed"] = True
    except (Exception, KeyboardInterrupt) as error:
        metadata["errors"].append(f"{type(error).__name__}: {error}")
    finally:
        for role, engine in reversed(list(engines.items())):
            try:
                engine.quit()
            except Exception as error:
                metadata["errors"].append(f"{role} cleanup: {type(error).__name__}: {error}")
            finally:
                engine.close()
        if guard is not None:
            guard.close()
            metadata["resource_guard"] = dict(guard.summary)
            metadata["resource_guard"]["valid_for_comparison"] = bool(
                metadata["completed"] and not metadata["errors"] and not guard.summary["contaminated"])
            metadata["resource_guard"]["scope"] = guard.summary.get("scope", "") + (
                " Recognized browsers obey the same zero-observed-CPU policy; arbitrary workloads, unknown names and thermal drift are not excluded.")
        if metadata["completed"] and not metadata["errors"]:
            try:
                metadata["summary"] = verify_records(metadata)
            except (KeyError, TypeError, ValueError) as error:
                metadata["errors"].append(f"independent replay: {type(error).__name__}: {error}")
        metadata["status"] = "complete" if metadata["summary"] is not None else "failed"
        metadata["valid_for_comparison"] = metadata["status"] == "complete"
        metadata["finished_utc"] = utc_now()
        (output / "games.pgn").write_text(_pgn(metadata), encoding="utf-8", newline="\n")
        metadata["pgn_sha256"] = sha256(output / "games.pgn")
        metadata["resource_samples_sha256"] = sha256(output / "resource-samples.jsonl") \
            if (output / "resource-samples.jsonl").exists() else None
        write_json(output / "match.json", metadata)
    return metadata


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--openings", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pairs", type=int)
    parser.add_argument("--movetime", type=int, default=100)
    parser.add_argument("--max-plies", type=int, default=384)
    args = parser.parse_args(argv)
    try:
        result = run_matches(args.binary, args.model, args.openings, args.output,
                             pairs=args.pairs, movetime_ms=args.movetime, max_plies=args.max_plies)
        return 0 if result["status"] == "complete" else 1
    except (ValueError, OSError) as error:
        print(f"development match failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
