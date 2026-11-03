"""Run the preregistered 96-game target A/B block, preserving raw evidence.

The existing compare_engines.play_one owns move selection, rule adjudication,
full-history UCI positions and per-move records. Two persistent engine processes
allow the existing fail-closed native CPU guard to cover the whole block.
The role names baseline/candidate mean target A/target B, respectively.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import io
import json
import math
from pathlib import Path
import random
import re
import sys
import threading
import time
from types import SimpleNamespace

import chess
import chess.engine
import chess.pgn

import compare_engines as comparison
import neural_precision as precision
import see_clean_guard as guarded
from run_neural_strength_comparison import Awake


ROOT = Path(__file__).resolve().parents[1]
SEEDS = (20261020, 20261021, 20261022)
ROLES = ("baseline", "candidate")
COMMON = {"Hash": 16, "Move Overhead": 20,
          **{name: True for name in (
              "UsePositionalEvaluation", "UseQuiescence", "UseTranspositionTable",
              "UseFastMoves", "UseQuietMoveOrdering", "UseQuiescenceChecks", "PVS",
              "UseSEE", "UseNeuralEvaluation", "UseIncrementalNeuralEvaluation")}}
CRITICAL_SOURCES = ("scripts/run_neural_target_ab_matches.py", "scripts/compare_engines.py",
                    "scripts/see_clean_guard.py", "scripts/run_neural_strength_comparison.py",
                    "scripts/neural_precision.py", "scripts/neural_training.py",
                    "scripts/neural_qat.py")


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def require(value, message):
    if not value:
        raise ValueError(message)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, document):
    with Path(path).open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(document, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def resolve(path):
    path = Path(path)
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def bound_files(freeze):
    require(freeze.get("schema_version") == 1, "Unsupported freeze schema")
    files = {}
    for key, item in freeze["files"].items():
        path, expected = (resolve(key), item) if isinstance(item, str) else (
            resolve(item["path"]), item["sha256"])
        require(isinstance(expected, str) and re.fullmatch(r"[0-9a-f]{64}", expected),
                "Malformed frozen SHA256")
        require(str(path) not in files or files[str(path)] == expected,
                "Contradictory frozen fingerprints")
        files[str(path)] = expected
    require({str(resolve(path)) for path in CRITICAL_SOURCES} <= set(files),
            "Freeze every executed match, runtime and monitor source")
    for key in ("engine", "bank"):
        item = freeze[key]
        path = str(resolve(item["path"]))
        require(files.get(path) == item["sha256"], f"Frozen {key} must occur in files")
    return files


def assert_inputs(files, freeze_path, freeze_sha):
    require(digest(freeze_path) == freeze_sha, "Execution freeze changed")
    frozen_head = read_json(freeze_path).get("checkout_head")
    if frozen_head is not None:
        require(comparison.git_command("rev-parse", "HEAD") == frozen_head,
                "Checkout HEAD differs from execution freeze")
    for path, expected in files.items():
        require(digest(path) == expected, "Frozen input changed: " + path)
    return {"checked_utc": now(), "freeze_sha256": freeze_sha,
            "checkout_head": frozen_head, "files": dict(files)}


def load_models(path):
    document = read_json(path)
    rows = document if isinstance(document, list) else document["models"]
    require(isinstance(rows, list) and len(rows) == 6, "Exactly six frozen models are required")
    models = {}
    for source in rows:
        item = dict(source)
        arm, seed = item["arm"], item["seed"]
        require(arm in ("A", "B") and type(seed) is int and seed in SEEDS,
                "Unplanned arm or shuffle seed")
        require((arm, seed) not in models, "Duplicate arm/seed model")
        item["path"] = str(resolve(item["path"]))
        require(digest(item["path"]) == item["sha256"], "Frozen model SHA256 mismatch")
        model = precision.load_model(item["path"])
        require((model["format_version"], model["input_scale"], model["clip"],
                 model["output_scale"]) == (2, 32, 4064, 256), "Unplanned model precision")
        checksum = precision.model_checksum(model)
        require(type(item["checksum"]) is int and item["checksum"] == checksum,
                "Frozen model checksum mismatch")
        models[arm, seed] = item
    require(set(models) == {(arm, seed) for arm in ("A", "B") for seed in SEEDS},
            "Missing paired shuffle seed")
    return models


def load_openings(path):
    rows = read_json(path)["match_openings_100"]
    require(isinstance(rows, list) and len(rows) == 16, "Exactly 16 fresh opening families required")
    families, positions, openings = set(), set(), []
    for row in rows:
        family = row["family_id"]
        require(isinstance(family, str) and family and family not in families,
                "Duplicate or invalid match opening family")
        families.add(family)
        initial = row.get("initial_fen", row.get("starting_fen", chess.STARTING_FEN))
        require(initial == chess.STARTING_FEN, "Match openings must retain normal-start history")
        moves = row.get("history", row.get("moves_uci"))
        require(isinstance(moves, list) and 1 <= len(moves) < 500, "Invalid opening history")
        board = chess.Board(initial)
        for text in moves:
            require(type(text) is str and board.outcome(claim_draw=True) is None,
                    "Malformed or post-terminal opening history")
            move = chess.Move.from_uci(text)
            require(move in board.legal_moves, "Illegal frozen opening move")
            board.push(move)
        require(board.outcome(claim_draw=True) is None, "Terminal match opening")
        fen = board.fen(en_passant="fen")
        require(row.get("fen_after_opening", fen) == fen, "Frozen opening FEN differs from replay")
        key = " ".join(board.fen(en_passant="legal").split()[:4])
        require(key not in positions, "Duplicate match opening position")
        positions.add(key)
        openings.append({"name": family, "family_id": family, "initial_fen": initial,
                         "moves": list(moves), "fen_after_opening": fen})
    return openings


def make_schedule(openings):
    schedule = []
    for index, opening in enumerate(openings):
        for offset in range(3):
            seed = SEEDS[(index + offset) % 3]
            b_first_white = (index + offset) % 2 == 0
            pair = index * 3 + offset + 1
            for b_white in (b_first_white, not b_first_white):
                schedule.append({"game": len(schedule) + 1, "pair": pair,
                                 "family_id": opening["family_id"], "seed": seed,
                                 "candidate_white": b_white})
    return schedule


class RawLog:
    def __init__(self, path):
        self.stream = Path(path).open("x", encoding="utf-8", newline="\n")
        self.lock, self.seq, self.rows = threading.Lock(), 0, []

    def line(self, role, direction, line):
        with self.lock:
            self.seq += 1
            row = {"seq": self.seq, "monotonic": time.monotonic(), "utc": now(),
                   "role": role, "direction": direction, "line": line}
            self.stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            self.stream.flush()
            self.rows.append(row)

    def protocol(self, role):
        log = self

        class Recorded(chess.engine.UciProtocol):
            def send_line(self, line):
                log.line(role, "send", line)
                super().send_line(line)

            def _line_received(self, line):
                log.line(role, "recv", line)
                super()._line_received(line)

        return Recorded


class BudgetExpired(RuntimeError):
    pass


class Budget:
    def __init__(self, seconds):
        self.started = time.monotonic()
        self.deadline = self.started + seconds
        self.closed, self.expired = threading.Event(), threading.Event()
        self.engines, self.thread = [], None

    def check(self):
        if self.expired.is_set() or time.monotonic() >= self.deadline:
            self.expired.set()
            raise BudgetExpired("The frozen 45-minute match cap was reached")

    def start(self):
        def watch():
            if self.closed.wait(max(0, self.deadline - time.monotonic())):
                return
            self.expired.set()
            for engine in list(self.engines):
                try:
                    engine.protocol.loop.call_soon_threadsafe(engine.protocol.send_line, "stop")
                except RuntimeError:
                    pass
            if not self.closed.wait(0.5):
                for engine in list(self.engines):
                    engine.close()
        self.thread = threading.Thread(target=watch, name="match-hard-wall-cap", daemon=True)
        self.thread.start()

    def close(self):
        self.closed.set()
        if self.thread is not None:
            self.thread.join(timeout=2)


class CheckedEngine:
    def __init__(self, engine, budget, guard):
        self.engine, self.budget, self.guard = engine, budget, guard

    def __getattr__(self, name):
        return getattr(self.engine, name)

    def play(self, *args, **kwargs):
        self.budget.check()
        self.guard.assert_clean()
        result = self.engine.play(*args, **kwargs)
        self.budget.check()
        self.guard.assert_clean()
        return result


def configure_pair(engines, raw, models, seed):
    start_seq = raw.seq
    options, acknowledgements = {}, {}
    for role, arm in zip(ROLES, ("A", "B")):
        item = models[arm, seed]
        options[role] = {**COMMON, "NeuralModelFile": item["path"]}
        if "Threads" in engines[role].options:
            options[role]["Threads"] = 1
        engines[role].configure(options[role])
        engines[role].ping()
        lines = [row for row in raw.rows if row["seq"] > start_seq and row["role"] == role
                 and row["direction"] == "recv"
                 and row["line"] == f"info string neural model checksum {item['checksum']}"]
        require(len(lines) == 1, "Missing or repeated actual loaded-model acknowledgement")
        acknowledgements[role] = {**lines[0], "pid": engines[role].transport.get_pid()}
    require({key: value for key, value in options["baseline"].items() if key != "NeuralModelFile"}
            == {key: value for key, value in options["candidate"].items() if key != "NeuralModelFile"},
            "Unequal non-model options")
    return options, acknowledgements


def bootstrap(scores):
    generator = random.Random(20261030)
    samples = sorted(sum(generator.choices(scores, k=len(scores))) / len(scores)
                     for _ in range(10000))
    return {"seed": 20261030, "resamples": 10000, "opening_families": len(scores),
            "interval": [samples[int(.025 * 9999)], samples[int(.975 * 9999)]],
            "scope": "Descriptive fixed opening-family bootstrap; three seeds and both colors remain together. No Elo or universal strength interval."}


def summarize(records, openings):
    complete = [game for game in records if game["completed"]]
    points = sum(game["candidate_points"] for game in complete)
    seeds = {}
    for seed in SEEDS:
        subset = [game for game in complete if game["seed"] == seed]
        seed_points = sum(game["candidate_points"] for game in subset)
        seeds[str(seed)] = {"completed_games": len(subset), "B_points": seed_points,
                            "B_score": seed_points / len(subset) if subset else None,
                            "B_wins": sum(game["candidate_points"] == 1 for game in subset),
                            "B_draws": sum(game["candidate_points"] == .5 for game in subset),
                            "B_losses": sum(game["candidate_points"] == 0 for game in subset)}
    families = []
    for opening in openings:
        games = [game for game in records if game["family_id"] == opening["family_id"]]
        done = len(games) == 6 and all(game["completed"] for game in games)
        families.append({"family_id": opening["family_id"], "complete": done,
                         "B_points": sum(game["candidate_points"] for game in games) if done else None,
                         "B_score": sum(game["candidate_points"] for game in games) / 6 if done else None})
    all_complete = len(complete) == len(records) == 96
    moves = [move for game in records for move in game["search_moves"]]
    resources = {}
    for role in ROLES:
        subset = [move for move in moves if move["role"] == role]
        resources[role] = {**comparison.resource_summary(subset),
                           "wall_over_requested_count": sum(move["elapsed_seconds"] > .1 + 1e-9 for move in subset),
                           "internal_over_allocated_count": sum((move.get("engine_time_seconds") or 0) > .08 + 1e-9 for move in subset),
                           "max_wall_ms": max((move["elapsed_seconds"] * 1000 for move in subset), default=None),
                           "max_internal_ms": max(((move.get("engine_time_seconds") or 0) * 1000 for move in subset), default=None)}
    return {"scheduled_games": 96, "attempted_games": len(records), "completed_games": len(complete),
            "incomplete_games": len(records) - len(complete), "unplayed_games": 96 - len(records),
            "B_wins": sum(game["candidate_points"] == 1 for game in complete),
            "B_draws": sum(game["candidate_points"] == .5 for game in complete),
            "B_losses": sum(game["candidate_points"] == 0 for game in complete),
            "B_points": points, "B_score": points / len(complete) if complete else None,
            "by_seed": seeds, "by_family": families,
            "opening_family_bootstrap95": bootstrap([row["B_score"] for row in families]) if all_complete else None,
            "score_scope": "Completed games only; incomplete games remain unfinished, never draws.",
            "resources": resources,
            "resource_scope": "Different played positions; aggregate nodes/depth are not same-position speed comparisons.",
            "time_scope": "100ms requested, 80ms after 20ms internal Move Overhead; wall overruns are reported rather than clock forfeits.",
            "screen_score_condition": all_complete and points >= 53 and sum(
                row["B_score"] is not None and row["B_score"] > .5 for row in seeds.values()) >= 2,
            "one_second_games_authorized_or_run": False}


def audit_records(records, openings, schedule, models):
    """Independently replay recorded moves, PVs, score shapes and adjudication."""
    require(len(records) <= len(schedule), "Unexpected extra game")
    opening_by_id = {row["family_id"]: row for row in openings}
    actual_plies = pv_plies = 0
    completed = 0
    for record, expected in zip(records, schedule):
        for key in ("game", "pair", "family_id", "seed"):
            require(record[key] == expected[key], "Recorded schedule differs: " + key)
        require(record["white"] == ("candidate" if expected["candidate_white"] else "baseline")
                and record["black"] == ("baseline" if expected["candidate_white"] else "candidate"),
                "Incorrect color swap")
        opening = opening_by_id[record["family_id"]]
        require(record["initial_fen"] == opening["initial_fen"]
                and record["opening_uci_moves"] == opening["moves"]
                and record["opening_fen"] == opening["fen_after_opening"], "Opening history differs")
        for role, arm in zip(ROLES, ("A", "B")):
            require(record["models"][role] == models[arm, expected["seed"]], "Incorrect per-game model")
            require(record["requested_options"][role] == {
                **COMMON, "NeuralModelFile": models[arm, expected["seed"]]["path"],
                **({"Threads": 1} if "Threads" in record["requested_options"][role] else {})},
                "Incorrect per-game engine options")
        moves, prefix = record["uci_moves"], opening["moves"]
        require(moves[:len(prefix)] == prefix and record["plies"] == len(moves) <= 500,
                "Incorrect opening prefix or ply count")
        require(len(record["search_moves"]) == len(moves) - len(prefix), "Search record count differs")
        board = chess.Board(record["initial_fen"])
        for text in prefix:
            board.push_uci(text)
        for ply, (text, item) in enumerate(zip(moves[len(prefix):], record["search_moves"]), len(prefix) + 1):
            require(board.outcome(claim_draw=True) is None, "Continued after a claimable outcome")
            role = record["white" if board.turn else "black"]
            require(item["ply"] == ply and item["move"] == text and item["role"] == role
                    and item["color"] == ("white" if board.turn else "black")
                    and item["fen_before"] == board.fen(en_passant="fen"), "Search position/role differs")
            move = chess.Move.from_uci(text)
            require(move in board.legal_moves, "Illegal recorded move")
            position = board.copy(stack=True)
            pv = item["pv"]
            require(isinstance(pv, list) and (not pv or pv[0] == text), "PV root differs from bestmove")
            for pv_text in pv:
                pv_move = chess.Move.from_uci(pv_text)
                require(pv_move in position.legal_moves, "Illegal recorded PV move")
                position.push(pv_move)
                pv_plies += 1
            score = item["score"]
            require(isinstance(score, dict) and score["pov"] == "mover"
                    and ((type(score["cp"]) is int and score["mate"] is None)
                         or (score["cp"] is None and type(score["mate"]) is int))
                    and type(score["lowerbound"]) is bool and type(score["upperbound"]) is bool,
                    "Malformed search score")
            for key in ("nodes", "depth", "seldepth"):
                require(type(item[key]) is int and item[key] >= 0, "Invalid search count: " + key)
            for key in ("elapsed_seconds", "engine_time_seconds"):
                require(type(item[key]) in (int, float) and math.isfinite(item[key]) and item[key] >= 0,
                        "Invalid search time: " + key)
            board.push(move)
        require(board.fen(en_passant="fen") == record["final_fen"], "Final FEN differs")
        actual_plies += len(moves)
        outcome = board.outcome(claim_draw=True)
        if record["completed"]:
            require(record["error"] is None and outcome is not None
                    and record["result"] == outcome.result()
                    and record["termination"] == outcome.termination.name.lower(),
                    "Incorrect completed game adjudication")
            point = .5 if outcome.winner is None else float(
                (outcome.winner == chess.WHITE) == (record["white"] == "candidate"))
            require(record["candidate_points"] == point, "Incorrect B score")
            completed += 1
        else:
            require(record["result"] == "*" and record["candidate_points"] is None,
                    "Unfinished game was assigned a result")
    return {"recorded_games": len(records), "completed_games": completed,
            "actual_plies_replayed": actual_plies, "pv_plies_replayed": pv_plies,
            "all_96_completed": len(records) == completed == 96}


def pgn_text(metadata):
    source = io.StringIO(comparison.pgn_text(metadata))
    result = []
    for record in metadata["games"]:
        game = chess.pgn.read_game(source)
        game.headers["Event"] = "Regicide target A versus target B, 100ms"
        game.headers["ShuffleSeed"] = str(record["seed"])
        for color in ("White", "Black"):
            model = record["models"][record[color.lower()]]
            game.headers[color + "TargetArm"] = model["arm"]
            game.headers[color + "ModelSHA256"] = model["sha256"]
            game.headers[color + "ModelChecksum"] = str(model["checksum"])
        result.append(str(game))
    return "\n\n".join(result) + "\n\n"


def run(args):
    output = args.output.resolve()
    require(not output.exists(), "Preserve previous match output")
    require(args.movetime == 100 and 0 < args.seconds_budget <= 2700,
            "Only the 100ms block with at most 45 minutes is authorized")
    engine, bank, models_path, freeze_path = map(resolve, (args.engine, args.bank, args.models, args.freeze))
    freeze = read_json(freeze_path)
    require(resolve(freeze["engine"]["path"]) == engine and resolve(freeze["bank"]["path"]) == bank,
            "CLI engine or bank differs from execution freeze")
    files = bound_files(freeze)
    freeze_sha = digest(freeze_path)
    models_document = read_json(models_path)
    if isinstance(models_document, dict):
        require(models_document.get("freeze_sha256") == freeze_sha,
                "Models were not frozen against this execution freeze")
    models = load_models(models_path)
    for model in models.values():
        files[model["path"]] = model["sha256"]
    files[str(models_path)] = digest(models_path)
    openings = load_openings(bank)
    schedule = make_schedule(openings)
    before = assert_inputs(files, freeze_path, freeze_sha)
    output.mkdir(parents=True)
    raw = RawLog(output / "uci.jsonl")
    budget, awake = Budget(args.seconds_budget), Awake()
    guard = guarded.EngineCpuGuard(guarded.WindowsProcesses(), output / "monitor.jsonl",
                                   interval=.5, abort_file=output / "abort-request.json")
    engines = {}
    metadata = {"schema_version": 1, "experiment": "neural-target-ab-100ms", "started_at": now(),
                "harness_checkout": comparison.checkout_provenance(),
                "freeze_path": str(freeze_path), "freeze_sha256": freeze_sha,
                "models_path": str(models_path), "models_sha256": digest(models_path),
                "engines": {}, "role_mapping": {"baseline": "A", "candidate": "B"},
                "limits": {"nodes": None, "depth": None, "movetime_ms": 100},
                "max_plies": 500, "draw_policy": "claim", "seconds_budget": args.seconds_budget,
                "games_requested": 96, "selected_openings": openings, "schedule": schedule,
                "opening_file_sha256": digest(bank), "games": [], "errors": [], "completed": False,
                "frozen_before": before, "frozen_after": None, "valid_for_comparison": False,
                "source_provenance_note": "Current checkout and frozen bytes are recorded; no claim that HEAD alone identifies the engine build."}
    state = {"schema_version": 1, "started_utc": metadata["started_at"], "freeze_sha256": freeze_sha,
             "freeze_path": str(freeze_path), "frozen_before": before, "frozen_after": None,
             "sequential_workloads": True, "movetime_ms": 100, "seconds_budget": args.seconds_budget,
             "error": None, "exit_code": 2, "valid_for_comparison": False}
    budget.start()
    try:
        awake.start()
        guard.start()
        guard.require_idle(10)
        for role in ROLES:
            budget.check()
            process = chess.engine.SimpleEngine.popen(raw.protocol(role), [str(engine), "uci"], timeout=2)
            budget.engines.append(process)
            engines[role] = CheckedEngine(process, budget, guard)
            guard.register_transport(process.transport.get_pid(), role, [str(engine), "uci"])
            metadata["engines"][role] = {"command": [str(engine), "uci"], "sha256": digest(engine),
                "provided_source_commit": freeze.get("engine_source_commit", freeze.get("checkout_head", "unknown")),
                "source_commit_object_verified": False, "engine_id": dict(process.id),
                "advertised_options": {name: {"type": item.type, "default": item.default,
                    "min": item.min, "max": item.max, "var": item.var} for name, item in process.options.items()},
                "pid": process.transport.get_pid(), "requested_options": dict(COMMON)}
            for name in COMMON:
                require(name in process.options, "Missing common engine option: " + name)
        guard.assert_transports()
        require(metadata["engines"]["baseline"]["engine_id"] == metadata["engines"]["candidate"]["engine_id"]
                and metadata["engines"]["baseline"]["advertised_options"] == metadata["engines"]["candidate"]["advertised_options"],
                "Byte-identical processes have differing UCI identities/options")
        by_id = {row["family_id"]: row for row in openings}
        limits = SimpleNamespace(nodes=None, depth=None, movetime=100, max_plies=500, draw_policy="claim")
        for index in range(0, len(schedule), 2):
            budget.check()
            expected = schedule[index]
            # A complete color pair has at most 100 seconds of requested search.
            # Do not knowingly begin a pair whose worst-case search cannot fit.
            if budget.deadline - time.monotonic() < 105:
                raise BudgetExpired("Insufficient frozen budget to start the next complete color pair")
            assert_inputs(files, freeze_path, freeze_sha)
            configured, acknowledgements = configure_pair(engines, raw, models, expected["seed"])
            for item in schedule[index:index + 2]:
                opening = by_id[item["family_id"]]
                record = comparison.new_record(item["game"], item["pair"], opening, item["candidate_white"])
                record.update(seed=item["seed"], family_id=item["family_id"],
                    models={role: models[arm, item["seed"]] for role, arm in zip(ROLES, ("A", "B"))},
                    requested_options=configured, model_acks=acknowledgements,
                    uci_seq_before=raw.seq, started_utc=now())
                metadata["games"].append(record)
                guard.sample("before-game")
                guard.assert_transports()
                try:
                    comparison.play_one(limits, engines, record)
                finally:
                    record["finished_utc"] = now()
                    record["uci_seq_after"] = raw.seq
                    guard.sample("after-game")
                    (output / "progress.json").write_text(json.dumps({"utc": now(),
                        "attempted_games": len(metadata["games"]), "last_game": record}, ensure_ascii=False) + "\n", encoding="utf-8")
                guard.assert_clean()
                print(f"game {item['game']}/96 {item['family_id']} seed {item['seed']}: "
                      f"{record['result']} ({record['termination']})", flush=True)
            require(all(record["completed"] for record in metadata["games"][-2:]),
                    "A color pair ended unfinished; preserve it and stop without retry")
        state["exit_code"] = 0
    except BaseException as exc:
        state["error"] = f"{type(exc).__name__}: {exc}"
        metadata["errors"].append(state["error"])
        print(state["error"], file=sys.stderr, flush=True)
    finally:
        cleanup_start = time.monotonic()
        for role, process in reversed(list(engines.items())):
            try:
                process.quit()
            except BaseException as exc:
                metadata["errors"].append(f"{role} cleanup: {type(exc).__name__}: {exc}")
            finally:
                process.close()
        budget.close()
        raw.stream.close()
        try:
            guard.close()
        except BaseException as exc:
            metadata["errors"].append("CPU guard cleanup: " + str(exc))
        try:
            awake.close()
        except BaseException as exc:
            metadata["errors"].append("Wake restore: " + str(exc))
        if not awake.record["restore_succeeded"]:
            metadata["errors"].append("Temporary wake state was not restored")
        try:
            metadata["frozen_after"] = state["frozen_after"] = assert_inputs(files, freeze_path, freeze_sha)
        except BaseException as exc:
            metadata["errors"].append(str(exc))
        metadata["completed"] = len(metadata["games"]) == 96 and all(row["completed"] for row in metadata["games"])
        metadata["summary"] = summarize(metadata["games"], openings)
        metadata["total_elapsed_seconds"] = time.monotonic() - budget.started
        metadata["cleanup_elapsed_seconds"] = time.monotonic() - cleanup_start
        metadata["budget_expired"] = budget.expired.is_set()
        metadata["wake_state"] = awake.record
        valid = metadata["completed"] and not metadata["errors"] and not guard.summary["contaminated"]
        try:
            replay = audit_records(metadata["games"], openings, schedule, models)
            audit = {"schema_version": 1, "created_utc": now(), "status": "PASS" if valid else "INCOMPLETE_OR_INVALID",
                     "record_replay_status": "PASS", **replay, "summary_recomputed": summarize(metadata["games"], openings),
                     "scope": "Independent board/PV/outcome/score-shape replay in this runner; separately replay raw UCI/native monitor before strength interpretation."}
        except BaseException as exc:
            audit = {"schema_version": 1, "created_utc": now(), "status": "FAIL", "error": str(exc)}
            metadata["errors"].append("Recorded-game replay: " + str(exc))
            valid = False
        metadata["valid_for_comparison"] = bool(valid)
        guard.summary["valid_for_comparison"] = bool(valid)
        metadata["external_engine_guard"] = guard.summary
        metadata["exit_code"] = state["exit_code"] = 0 if valid else 2
        metadata["summary"]["screening_passed"] = bool(valid and metadata["summary"]["screen_score_condition"])
        write_json(output / "monitor.json", guard.summary)
        write_json(output / "match.json", metadata)
        (output / "match.pgn").write_text(pgn_text(metadata), encoding="utf-8", newline="\n")
        audit["artifact_sha256"] = {path.name: digest(path) for path in output.iterdir() if path.is_file()}
        write_json(output / "audit.json", audit)
        state.update(finished_utc=now(), valid_for_comparison=valid and audit["status"] == "PASS",
                     budget_expired=metadata["budget_expired"], wake_state=awake.record,
                     artifact_sha256={path.name: digest(path) for path in output.iterdir() if path.is_file()})
        write_json(output / "driver-state.json", state)
    return state["exit_code"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("engine", "bank", "models", "freeze", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--movetime", type=int, default=100, choices=(100,))
    parser.add_argument("--seconds-budget", type=float, default=2700)
    args = parser.parse_args(argv)
    try:
        return run(args)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"FAIL: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
