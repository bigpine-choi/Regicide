"""Held-out paired-color promotion arena. An incomplete or dirty match never passes.

The statistical unit is an opening pair, not an individual game. The one-sided
Hoeffding lower bound concerns the declared randomized opening distribution and
time control; it is neither Elo nor a universal guarantee of chess strength.
"""

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
import time
from types import SimpleNamespace

import chess
import chess.engine
import chess.pgn

import compare_engines as comparison
from collect_learning_data import OPTIONS
from see_clean_guard import (CPU_TICKS_PER_SECOND, EngineCpuGuard, ProcessInfo,
                             WindowsProcesses)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OPENINGS = ROOT / "tests/data/learning_arena_openings_v1.json"
SCHEMA = "regicide.learning.arena.v1"
POLICY = "pair-hoeffding-summable-alpha-v1"
BROWSER_NAMES = {"chrome.exe", "msedge.exe", "whale.exe", "firefox.exe", "opera.exe",
                 "chrome", "msedge", "whale", "firefox", "opera"}


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def validate_native_model(binary, model):
    """Exercise the actual C++ parser, without trusting a Python-only format check."""
    reply = subprocess.run([str(Path(binary).resolve()), "eval", "--eval-file", str(Path(model).resolve())],
                           capture_output=True, text=True, timeout=30, check=True)
    if not re.fullmatch(r"score_cp: -?\d+\s*", reply.stdout):
        raise ValueError("native model verification returned an unexpected response")
    return {"score_cp": int(reply.stdout.split(":", 1)[1]), "model_sha256": sha256(model)}


def promotion_decision(pair_scores, attempt_index, *, complete, clean, movetime_ms=100):
    if isinstance(attempt_index, bool) or not isinstance(attempt_index, int) or attempt_index < 1:
        raise ValueError("attempt_index must be a globally increasing positive integer")
    if any(isinstance(value, bool) or value not in (0, .25, .5, .75, 1) for value in pair_scores):
        raise ValueError("pair scores must be averages of two legal game scores")
    alpha = .05 / (attempt_index * (attempt_index + 1))
    count = len(pair_scores)
    mean = sum(pair_scores) / count if count else None
    lower = max(0.0, mean - math.sqrt(math.log(1 / alpha) / (2 * count))) if count else None
    eligible = count >= 32 and movetime_ms >= 100 and bool(complete) and bool(clean)
    return {"policy": POLICY, "attempt_index": attempt_index, "familywise_alpha_budget": .05,
            "attempt_alpha": alpha, "minimum_pairs": 32, "minimum_movetime_ms": 100, "completed_pairs": count,
            "candidate_pair_score": mean, "one_sided_lower_bound": lower,
            "eligible": eligible, "passed": bool(eligible and lower > .5),
            "criterion": "at least 32 complete clean independent opening pairs at >=100ms per move and one-sided lower bound > 0.5",
            "scope": "Declared randomized reserved opening distribution and equal time control only; no Elo claim."}


def load_openings(path, count, seed):
    """Draw independent seeds with replacement; reserve prefixes never seed training."""
    raw = Path(path).read_bytes()
    document = json.loads(raw.decode("utf-8"))
    if document.get("schema") != "regicide.learning.arena.openings.v1":
        raise ValueError("unexpected reserved opening schema")
    seeds = document.get("seeds")
    prefixes = document.get("prefixes")
    if (not isinstance(seeds, list) or len(seeds) < 32 or len(set(seeds)) != len(seeds)
            or any(isinstance(value, bool) or not isinstance(value, int) for value in seeds)):
        raise ValueError("reserved opening seed pool must contain at least 32 distinct integers")
    if not isinstance(prefixes, list) or not prefixes:
        raise ValueError("reserved prefixes are missing")
    training = json.loads((ROOT / "tests/data/learning_openings_v1.json").read_text(encoding="utf-8"))
    training_first = {item["moves"][0] for item in training["openings"]}
    for prefix in prefixes:
        if (not isinstance(prefix, list) or not prefix or prefix[0] in training_first):
            raise ValueError("arena opening prefixes must be separate from training families")
        board = chess.Board()
        for move in prefix:
            board.push_uci(move)
        if board.outcome(claim_draw=True) is not None:
            raise ValueError("terminal reserved prefix")
    generator = random.Random(seed)
    openings = []
    # Each pair gets an IID draw from this fixed seed pool; duplicate draws are
    # retained, never replaced after inspecting either position or game result.
    for index in range(count):
        selected = generator.choice(seeds)
        rng = random.Random(selected)
        moves = list(rng.choice(prefixes))
        board = chess.Board()
        for move in moves:
            board.push_uci(move)
        while len(moves) < 12:
            if board.outcome(claim_draw=True) is not None:
                raise ValueError("a reserved generated opening is terminal")
            move = rng.choice(sorted(board.legal_moves, key=lambda item: item.uci()))
            moves.append(move.uci())
            board.push(move)
        if board.outcome(claim_draw=True) is not None:
            raise ValueError("a reserved generated opening is terminal")
        openings.append({"name": f"reserved-{index + 1:04d}-seed-{selected}", "seed": selected,
                         "moves": moves, "fen_after_opening": board.fen(en_passant="fen")})
    return {"document": document, "sha256": hashlib.sha256(raw).hexdigest(), "seed": seed,
            "selection": "IID seed draws with replacement from reserved pool, fixed before any result",
            "training_prohibition": "All arena games, scores and positions are held out from all learning datasets",
            "openings": openings}


class LinuxProcesses:
    """Native /proc CPU counters compatible with the existing fail-closed guard."""
    def __init__(self):
        self.engine_names = frozenset({"regicide", "regicide.exe", "stockfish", "lc0"} | BROWSER_NAMES)
        self.frequency = os.sysconf("SC_CLK_TCK")

    def is_engine(self, process):
        name = process.name.casefold()
        return name in self.engine_names or name.startswith(("regicide", "stockfish"))

    def snapshot(self):
        result = {}
        for folder in Path("/proc").iterdir():
            if not folder.name.isdecimal():
                continue
            try:
                text = (folder / "stat").read_text()
                left, right = text.index("("), text.rindex(")")
                tail = text[right + 2:].split()
                result[int(folder.name)] = ProcessInfo(int(folder.name), int(tail[1]), text[left + 1:right],
                    int(tail[19]), (int(tail[11]) + int(tail[12])) * CPU_TICKS_PER_SECOND // self.frequency)
            except FileNotFoundError:
                continue  # Final guard sees missing known external identities as contamination.
            except (OSError, ValueError, IndexError) as error:
                raise RuntimeError(f"cannot inspect native process {folder.name}: {error}") from error
        return result


class GuardedEngine:
    def __init__(self, engine, guard, movetime_ms):
        self.engine, self.guard = engine, guard
        self.timeout_seconds = movetime_ms / 1000 + max(.1, movetime_ms / 1000)

    def play(self, *arguments, **keywords):
        self.guard.assert_clean()
        started = time.monotonic()
        result = self.engine.play(*arguments, **keywords)
        if time.monotonic() - started > self.timeout_seconds:
            raise TimeoutError("engine response exceeded the declared move-time transport allowance")
        self.guard.assert_clean()
        return result


def _pgn(metadata):
    records = []
    for record in metadata["games"]:
        game = chess.pgn.Game()
        game.headers.update(Event="Regicide held-out learning promotion arena", Site="local",
                            Date=metadata["started_utc"][:10].replace("-", "."),
                            Round=str(record["game"]), Result=record["result"],
                            White=record["white"], Black=record["black"], Pair=str(record["pair"]),
                            Opening=record["opening"], TerminationReason=record["termination"],
                            DrawPolicy="claim", RequestedMoveTimeMS=str(metadata["movetime_ms"]),
                            BinarySHA256=metadata["binary_sha256"],
                            CandidateModelSHA256=metadata["candidate_sha256"],
                            ChampionModelSHA256=metadata["champion_sha256"] or "classical")
        node = game
        for move in record["uci_moves"]:
            node = node.add_variation(chess.Move.from_uci(move))
        records.append(str(game))
    return "\n\n".join(records) + "\n"


def verify_match_records(metadata):
    """Recompute rule outcomes and paired scores; never trust a cached pass flag."""
    if metadata.get("schema") != SCHEMA or metadata.get("draw_policy") != "claim":
        raise ValueError("unexpected arena schema or draw policy")
    if metadata.get("errors") or not metadata.get("completed"):
        raise ValueError("arena has errors or unfinished games")
    guard = metadata.get("resource_guard", {})
    if not guard.get("valid_for_comparison") or guard.get("contaminated"):
        raise ValueError("arena resource guard did not pass")
    if metadata.get("movetime_ms", 0) <= 0 or metadata.get("hash_mb") != 16 or metadata.get("threads") != 1:
        raise ValueError("invalid equal-time resource settings")
    allowance = metadata["movetime_ms"] / 1000 + max(.1, metadata["movetime_ms"] / 1000)
    if metadata.get("move_response_allowance_seconds") != allowance:
        raise ValueError("move response allowance differs from the fixed policy")
    openings = metadata["opening_reserve"]["openings"]
    games = metadata["games"]
    if len(games) != 2 * len(openings) or len(openings) != metadata["requested_pairs"]:
        raise ValueError("arena pair/game count mismatch")
    scores = []
    for index, opening in enumerate(openings):
        pair = games[2 * index:2 * index + 2]
        points = []
        for color_index, record in enumerate(pair):
            white = "candidate" if color_index == 0 else "baseline"
            if (record["pair"] != index + 1 or record["game"] != 2 * index + color_index + 1
                    or record["white"] != white or record["black"] != ("baseline" if white == "candidate" else "candidate")
                    or record["opening_uci_moves"] != opening["moves"]
                    or record["initial_fen"] != chess.STARTING_FEN
                    or record.get("error") is not None or not record["completed"]
                    or record["uci_moves"][:len(opening["moves"])] != opening["moves"]):
                raise ValueError("invalid paired game schedule or opening")
            board = chess.Board()
            searched = record.get("search_moves", [])
            if len(searched) != len(record["uci_moves"]) - len(opening["moves"]):
                raise ValueError("arena move resource evidence is missing")
            for ply, move in enumerate(record["uci_moves"]):
                if board.outcome(claim_draw=True) is not None:
                    raise ValueError("arena history continues after a rule ending")
                if ply >= len(opening["moves"]):
                    evidence = searched[ply - len(opening["moves"])]
                    role = record["white"] if board.turn else record["black"]
                    elapsed = evidence.get("elapsed_seconds")
                    if (evidence.get("move") != move or evidence.get("role") != role
                            or evidence.get("fen_before") != board.fen(en_passant="fen")
                            or not isinstance(elapsed, (float, int)) or not math.isfinite(elapsed)
                            or elapsed < 0 or elapsed > metadata["move_response_allowance_seconds"]):
                        raise ValueError("arena move timing, role or position evidence mismatch")
                board.push_uci(move)
            outcome = board.outcome(claim_draw=True)
            if (outcome is None or outcome.result() != record["result"]
                    or outcome.termination.name.lower() != record["termination"]
                    or board.fen(en_passant="fen") != record["final_fen"]
                    or len(record["uci_moves"]) != record["plies"]):
                raise ValueError("arena result is not supported by its full history")
            score = .5 if outcome.winner is None else float((outcome.winner == chess.WHITE) == (white == "candidate"))
            if score != record["candidate_points"]:
                raise ValueError("candidate score does not match rule outcome")
            points.append(score)
        scores.append(sum(points) / 2)
    decision = promotion_decision(scores, metadata["attempt_index"], complete=True, clean=True,
                                  movetime_ms=metadata["movetime_ms"])
    if metadata.get("decision") != decision:
        raise ValueError("stored confidence decision does not match replayed outcomes")
    return decision


def run_arena(binary, candidate_model, champion_model_or_none, output, *, pairs=32,
              movetime_ms=100, seed=20261018, attempt_index=1, opening_data=None, max_plies=500):
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0
           for value in (pairs, movetime_ms, max_plies)):
        raise ValueError("pairs, movetime_ms and max_plies must be positive integers")
    promotion_decision([], attempt_index, complete=False, clean=False)
    binary, candidate = Path(binary).resolve(), Path(candidate_model).resolve()
    champion = Path(champion_model_or_none).resolve() if champion_model_or_none else None
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    snapshot = output / "candidate.nn"
    snapshot.write_bytes(candidate.read_bytes())
    old_snapshot = output / "champion.nn" if champion else None
    if champion:
        old_snapshot.write_bytes(champion.read_bytes())
    reserve = load_openings(opening_data or DEFAULT_OPENINGS, pairs, seed)
    metadata = {"schema": SCHEMA, "started_utc": utc_now(), "finished_utc": None,
                "artifact_directory": str(output), "status": "running",
                "binary_sha256": sha256(binary), "candidate_sha256": sha256(snapshot),
                "champion_sha256": sha256(old_snapshot) if old_snapshot else None,
                "candidate_snapshot": snapshot.name, "champion_snapshot": old_snapshot.name if old_snapshot else None,
                "native_model_validation": {}, "requested_pairs": pairs, "attempt_index": attempt_index,
                "movetime_ms": movetime_ms, "hash_mb": 16, "threads": 1,
                "move_response_allowance_seconds": movetime_ms / 1000 + max(.1, movetime_ms / 1000),
                "move_response_allowance_note": "Same requested engine budget for both sides; reject transport round trip longer than budget plus max(100ms, budget).",
                "thread_note": "Regicide engine search is single threaded; no Threads UCI option exists",
                "max_plies": max_plies, "draw_policy": "claim", "opening_reserve": reserve,
                "host": {"platform": platform.platform(), "machine": platform.machine(),
                         "processor": platform.processor(), "logical_cpus": os.cpu_count(),
                         "python": platform.python_version(), "python_chess": chess.__version__},
                "engines": {}, "games": [], "errors": [], "completed": False,
                "resource_guard": {}, "decision": None}
    engines, guard = {}, None
    try:
        metadata["native_model_validation"]["candidate"] = validate_native_model(binary, snapshot)
        if champion:
            metadata["native_model_validation"]["baseline"] = validate_native_model(binary, old_snapshot)
        provider = WindowsProcesses(engine_names={"regicide.exe", "stockfish.exe", "lc0.exe"} | BROWSER_NAMES) \
            if sys.platform == "win32" else LinuxProcesses() if sys.platform.startswith("linux") else None
        if provider is None:
            raise RuntimeError("native resource guard is supported on Windows and Linux only")
        guard = EngineCpuGuard(provider, output / "resource-samples.jsonl", interval=.5)
        guard.start()
        for role, model in (("baseline", old_snapshot), ("candidate", snapshot)):
            raw_engine = chess.engine.SimpleEngine.popen_uci([str(binary), "uci"], timeout=10)
            engines[role] = raw_engine
            guard.register_transport(raw_engine.transport.get_pid(), role, [str(binary), "uci"])
            options = dict(OPTIONS, UseNeuralEvaluation=model is not None)
            if model:
                options["EvalFile"] = str(model)
            for name, value in options.items():
                if name not in raw_engine.options:
                    raise ValueError(f"engine does not advertise {name}")
                # Loading must precede enable. UCI preserves explicitly sent state,
                # and the exact immutable bytes have already passed the C++ parser.
            if model:
                raw_engine.configure({"EvalFile": str(model)})
            raw_engine.configure({key: value for key, value in options.items() if key != "EvalFile"})
            raw_engine.ping()
            metadata["engines"][role] = {"id": dict(raw_engine.id), "options": options,
                                         "binary_sha256": metadata["binary_sha256"],
                                         "model_sha256": sha256(model) if model else None}
        guard.assert_transports()
        args = SimpleNamespace(nodes=None, depth=None, movetime=movetime_ms, max_plies=max_plies, draw_policy="claim")
        wrapped = {key: GuardedEngine(value, guard, movetime_ms) for key, value in engines.items()}
        for pair, opening in enumerate(reserve["openings"], 1):
            for candidate_white in (True, False):
                guard.sample("before-game")
                guard.assert_clean()
                record = comparison.new_record(len(metadata["games"]) + 1, pair, opening, candidate_white)
                metadata["games"].append(record)
                comparison.play_one(args, wrapped, record)
                print(f"arena {len(metadata['games'])}/{pairs * 2}: {record['result']} ({record['termination']})", flush=True)
                if not record["completed"]:
                    raise RuntimeError("unfinished arena game; no strength adoption is allowed")
        guard.assert_transports()
        if (sha256(binary) != metadata["binary_sha256"] or sha256(candidate) != metadata["candidate_sha256"]
                or champion and sha256(champion) != metadata["champion_sha256"]):
            raise ValueError("input binary or model changed during the match")
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
        if guard:
            guard.close()
            metadata["resource_guard"] = dict(guard.summary)
            metadata["resource_guard"]["scope"] += (
                " Recognized browsers are monitored by the same native CPU rule. "
                "Arbitrary non-browser host workloads, thermal drift and unknown process names are not excluded.")
            metadata["resource_guard"]["valid_for_comparison"] = bool(
                metadata["completed"] and not metadata["errors"] and not guard.summary["contaminated"])
        scores = []
        for index in range(pairs):
            games = metadata["games"][index * 2:index * 2 + 2]
            if len(games) == 2 and all(game["completed"] for game in games):
                scores.append(sum(game["candidate_points"] for game in games) / 2)
        metadata["finished_utc"] = utc_now()
        metadata["decision"] = promotion_decision(scores, attempt_index,
            complete=metadata["completed"] and not metadata["errors"],
            clean=metadata["resource_guard"].get("valid_for_comparison", False), movetime_ms=movetime_ms)
        metadata["status"] = "complete" if (metadata["completed"] and not metadata["errors"]
            and metadata["resource_guard"].get("valid_for_comparison", False)) else "failed"
        metadata["eligible"] = metadata["decision"]["eligible"]
        metadata["promotion_passed"] = metadata["decision"]["passed"]
        if metadata["decision"]["passed"]:
            verify_match_records(metadata)
        (output / "games.pgn").write_text(_pgn(metadata), encoding="utf-8")
        metadata["pgn_sha256"] = sha256(output / "games.pgn")
        metadata["resource_samples_sha256"] = sha256(output / "resource-samples.jsonl") \
            if (output / "resource-samples.jsonl").exists() else None
        write_json(output / "arena.json", metadata)
    return metadata
