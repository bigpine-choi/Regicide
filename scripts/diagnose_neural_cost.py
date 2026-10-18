"""Separate zero-output V2 inference cost from changed search decisions.

This is a development diagnostic, never a promotion arena. A zero-output copy
keeps every hidden weight of the declared V2 model and sets only w3/b3 to zero.
HCE and zero must perform identical deterministic work. Learned/HCE differences
at fixed nodes/depth describe decisions, not objective errors or chess strength.
Finite, deeper own-engine scores provide explicitly limited decision proxies.
"""

import argparse
import copy
import itertools
import json
import math
from pathlib import Path
import random
import statistics
import sys
import time

import chess
import chess.engine
import numpy as np

import benchmark_search as benchmark
from collect_learning_data import (LearningEngine, OPTIONS, canonical_position_key,
                                   parse_score_event)
from learning_arena import (BROWSER_NAMES, LinuxProcesses, validate_native_model,
                            utc_now, write_json)
from neural_model import export_model, feature_key, load_model
from see_clean_guard import EngineCpuGuard, WindowsProcesses


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "regicide.neural.cost.diagnostic.v1"
FIXTURE_SCHEMA = "regicide.neural.diagnostic.positions.v1"
ROLES = ("hce", "zero_v2", "learned_v2")
MODES = ("depth", "nodes", "time")
DEFAULT_POSITIONS = ROOT / "tests/data/neural_diagnostic_positions_v1.json"
MATE_CP_THRESHOLD = 29904
EQUIVALENCE_FIELDS = ("move", "score", "depth", "seldepth", "nodes", "pv", "tt_counters")


def load_positions(path):
    document, positions = benchmark.load_positions(Path(path))
    if document.get("schema") != FIXTURE_SCHEMA:
        raise ValueError("unexpected diagnostic fixture schema")
    keys, features = set(), set()
    for position in positions:
        board = benchmark.board_for(position)
        key, neural = canonical_position_key(board), feature_key(board)
        if key in keys or neural in features:
            raise ValueError("diagnostic fixture repeats a board or neural input")
        keys.add(key)
        features.add(neural)
    return document, positions


def _opening_boards():
    """Protect every declared training prefix and every possible arena seed."""
    training_path = ROOT / "tests/data/learning_openings_v1.json"
    arena_path = ROOT / "tests/data/learning_arena_openings_v1.json"
    training = json.loads(training_path.read_text(encoding="utf-8"))
    arena = json.loads(arena_path.read_text(encoding="utf-8"))
    boards = []
    for opening in training["openings"]:
        board = chess.Board()
        boards.append(board.copy())
        for text in opening["moves"]:
            board.push_uci(text)
            boards.append(board.copy())
    for seed in arena["seeds"]:
        rng = random.Random(seed)
        moves = list(rng.choice(arena["prefixes"]))
        board = chess.Board()
        boards.append(board.copy())
        for text in moves:
            board.push_uci(text)
            boards.append(board.copy())
        while len(moves) < 12:
            move = rng.choice(sorted(board.legal_moves, key=lambda value: value.uci()))
            board.push(move)
            moves.append(move.uci())
            boards.append(board.copy())
    return boards, [training_path, arena_path]


def overlap_check(positions, excluded_manifests=()):
    from train_neural import json_lines, load_dataset, local_data_path
    boards, paths = _opening_boards()
    counts = {"declared_opening_boards": len(boards), "manifests": []}
    for source in excluded_manifests:
        source = Path(source).resolve()
        # Validate replay, game outcomes, split boundaries and labels before
        # trusting a manifest as the protected training/holdout inventory.
        load_dataset(source, allow_empty_splits=True, model_version=2)
        manifest = json.loads(source.read_text(encoding="utf-8"))
        samples = local_data_path(source, manifest.get("positions_path", manifest.get("samples_path")))
        count = 0
        for row in json_lines(samples):
            board = chess.Board(row["fen"])
            if not board.is_valid():
                raise ValueError("excluded manifest contains an invalid board")
            boards.append(board)
            count += 1
        games = local_data_path(source, manifest.get("games_path"))
        paths.extend([source, samples, games])
        counts["manifests"].append({"path": str(source), "rows": count})
    keys = {canonical_position_key(board) for board in boards}
    neural = {feature_key(board) for board in boards}
    for position in positions:
        board = benchmark.board_for(position)
        if canonical_position_key(board) in keys or feature_key(board) in neural:
            raise ValueError(f"diagnostic overlaps protected board/neural input: {position['name']}")
    counts.update(protected_board_keys=len(keys), protected_neural_inputs=len(neural),
                  checked_positions=len(positions), collisions=0,
                  files=[{"path": str(path), "sha256": benchmark.digest(path)} for path in dict.fromkeys(paths)],
                  scope="Exact canonical positions and model inputs in supplied manifests and declared opening prefixes/arena seed continuations only; arbitrary unseen continuation positions are not excluded.")
    return counts


def make_zero(model_path, target):
    model = load_model(model_path)
    if model.version != 2:
        raise ValueError("diagnostic requires a V2 learned model")
    model.w3.fill(0)
    model.b3.fill(0)
    export_model(model, target)
    return model


def schedule(positions, repeats, seed):
    rng = random.Random(seed)
    orders = list(itertools.permutations(ROLES))
    rng.shuffle(orders)
    for repetition in range(repeats):
        indices = list(range(len(positions)))
        rng.shuffle(indices)
        for index in indices:
            yield repetition + 1, index, list(orders[(repetition + index) % len(orders)])


def sample(engine, position, limit, *, expected_depth=None, root_move=None):
    board = benchmark.board_for(position)
    initial = board.fen(en_passant="fen")
    root = chess.Move.from_uci(root_move) if root_move else None
    if root is not None and root not in board.legal_moves:
        raise ValueError("forced root move is illegal")
    started = time.perf_counter()
    reply = engine.play(board, limit, game=object(), info=chess.engine.INFO_ALL,
                        root_moves=[root] if root is not None else None)
    wall = time.perf_counter() - started
    info = reply.info
    if reply.move is None or reply.move not in board.legal_moves:
        raise RuntimeError("engine returned illegal or missing bestmove")
    if root is not None and reply.move != root:
        raise RuntimeError("engine ignored searchmoves root restriction")
    raw_event = copy.deepcopy(getattr(engine.protocol, "last_score_event", None))
    if not isinstance(raw_event, dict) or parse_score_event(raw_event.get("raw_info", "")) != raw_event:
        raise RuntimeError("missing or inconsistent raw score event")
    point = info["score"].pov(board.turn) if info.get("score") is not None else None
    if point is None or (point.score() if raw_event["kind"] == "cp" else point.mate()) != raw_event["value"]:
        raise RuntimeError("raw and parsed search score disagree")
    score = {"pov": "mover", "cp": point.score(), "mate": point.mate(),
             "lowerbound": raw_event["lowerbound"], "upperbound": raw_event["upperbound"]}
    if score["lowerbound"] or score["upperbound"]:
        raise RuntimeError("diagnostic requires an exact root score")
    if score["cp"] is not None and (type(score["cp"]) is not int or abs(score["cp"]) >= MATE_CP_THRESHOLD):
        raise RuntimeError("mate-range CP cannot be treated as a finite score")
    if not isinstance(info.get("depth"), int) or info["depth"] < 1:
        raise RuntimeError("engine did not complete a search iteration")
    if expected_depth is not None and info["depth"] != expected_depth:
        raise RuntimeError("engine did not finish requested depth")
    if not isinstance(info.get("nodes"), int) or info["nodes"] < 1:
        raise RuntimeError("missing positive node count")
    seconds = info.get("time")
    if not isinstance(seconds, (float, int)) or not math.isfinite(seconds) or seconds < 0:
        raise RuntimeError("missing finite engine time")
    pv = info.get("pv")
    if not isinstance(pv, list) or not pv or pv[0] != reply.move:
        raise RuntimeError("missing PV or PV does not begin with bestmove")
    tokens = raw_event["raw_info"].split()
    try:
        if (int(tokens[tokens.index("depth") + 1]) != info["depth"] or
                int(tokens[tokens.index("nodes") + 1]) != info["nodes"] or
                tokens[tokens.index("pv") + 1:] != [move.uci() for move in pv]):
            raise ValueError("different raw search fields")
    except (IndexError, ValueError) as error:
        raise RuntimeError("raw score event lacks matching depth/nodes/PV") from error
    for move in pv:
        if board.outcome(claim_draw=True) is not None or move not in board.legal_moves:
            raise RuntimeError("illegal or post-terminal PV")
        board.push(move)
    return {"move": reply.move.uci(), "score": score, "depth": info["depth"],
            "seldepth": info.get("seldepth"), "nodes": info["nodes"],
            "pv": [move.uci() for move in pv], "tt_counters": benchmark.transposition_counters(info),
            "tt_memory": benchmark.transposition_memory(info), "search_fen": initial,
            "engine_time_seconds": seconds, "wall_time_seconds": wall,
            "raw_score_event": raw_event,
            "requested_limit": {"depth": limit.depth, "nodes": limit.nodes, "time": limit.time},
            "forced_root_move": root_move}


def equivalence_difference(one, two):
    return [field for field in EQUIVALENCE_FIELDS if one.get(field) != two.get(field)]


def _median(values):
    return statistics.median(values) if values else None


def summarize(records, *, clean, completed):
    result = {"timing_valid": bool(clean and completed), "modes": {},
              "scope": "Cost and finite search decisions on development positions; no Elo, win rate or model adoption evidence."}
    for mode in MODES:
        rows = [row for row in records if row["mode"] == mode and all(role in row for role in ROLES)]
        fixed = mode != "time"
        equal = fixed and all(not equivalence_difference(row["hce"], row["zero_v2"]) for row in rows)
        timed_valid = bool(clean and completed and rows and (equal if fixed else True))
        row = {"completed_triples": len(rows), "hce_zero_work_equal": equal if fixed else None,
               "hce_zero_work_mismatches": sum(bool(equivalence_difference(item["hce"], item["zero_v2"])) for item in rows) if fixed else None,
               "learned_hce_move_changes": sum(item["learned_v2"]["move"] != item["hce"]["move"] for item in rows),
               "timing_valid": timed_valid}
        for role in ("zero_v2", "learned_v2"):
            # Zero/HCE only isolates inference cost when the work is identical.
            # Learned ratio includes changes in search graph even at fixed depth.
            row[role] = {"median_paired_wall_ratio_over_hce": _median([
                item[role]["wall_time_seconds"] / item["hce"]["wall_time_seconds"]
                for item in rows if item["hce"]["wall_time_seconds"] > 0]) if timed_valid else None,
                "median_paired_engine_ratio_over_hce": _median([
                    item[role]["engine_time_seconds"] / item["hce"]["engine_time_seconds"]
                    for item in rows if item["hce"]["engine_time_seconds"] > 0 and item[role]["engine_time_seconds"] > 0]) if timed_valid else None,
                "median_node_ratio_over_hce": _median([item[role]["nodes"] / item["hce"]["nodes"] for item in rows]),
                "median_completed_depth": _median([item[role]["depth"] for item in rows]),
                "interpretation": "isolated inference overhead only in fixed-work modes with proven HCE/zero equivalence" if role == "zero_v2" else "inference plus changed search graph; move changes alone do not prove errors"}
        result["modes"][mode] = row
    return result


def rank_moves(forced):
    """Only compare exact scores at one completed root depth; CP excludes mates."""
    if not forced or len({row["depth"] for row in forced.values()}) != 1:
        return {"available": False, "reason": "forced searches did not complete one common depth"}
    if any(row["score"].get("cp") is not None and
           (type(row["score"]["cp"]) is not int or abs(row["score"]["cp"]) >= MATE_CP_THRESHOLD)
           for row in forced.values()):
        return {"available": False, "reason": "nonfinite or mate-range CP is not regret evidence"}
    ordering = sorted(forced, key=lambda move: (-chess.engine.PovScore(
        chess.engine.Mate(forced[move]["score"]["mate"]) if forced[move]["score"]["mate"] is not None
        else chess.engine.Cp(forced[move]["score"]["cp"]), chess.WHITE).relative.score(mate_score=30000), move))
    def key(move):
        score = forced[move]["score"]
        return chess.engine.Mate(score["mate"]) if score["mate"] is not None else chess.engine.Cp(score["cp"])
    best = max(key(move) for move in ordering)
    values = {}
    for move in ordering:
        score = key(move)
        values[move] = {"rank_among_compared_moves": 1 + sum(key(other) > score for other in ordering),
                        "regret_cp": best.score() - score.score() if best.score() is not None and score.score() is not None else None}
    return {"available": True, "completed_depth": next(iter(forced.values()))["depth"], "moves": values,
            "scope": "Own finite selective-search scores among compared moves only, not objective move ranks or error proof. Mate comparisons have no centipawn regret."}


def teacher_diagnosis(engine, position, moves, low_nodes, high_nodes, *, before_search=lambda: None):
    references = []
    for nodes in (low_nodes, high_nodes):
        before_search()
        references.append(sample(engine, position, chess.engine.Limit(nodes=nodes)))
    moves = sorted(set(moves) | {row["move"] for row in references})
    common_depth = min(row["depth"] for row in references)
    forced = {}
    for move in moves:
        before_search()
        forced[move] = sample(engine, position, chess.engine.Limit(depth=common_depth, nodes=high_nodes), root_move=move)
    ranks = rank_moves(forced)
    if any(row["depth"] != common_depth for row in forced.values()):
        ranks = {"available": False, "reason": "a forced search hit node cap before requested common depth"}
    one, two = references
    cp_delta = abs(one["score"]["cp"] - two["score"]["cp"]) if one["score"]["cp"] is not None and two["score"]["cp"] is not None else None
    return {"references": references, "compared_moves": moves, "forced_common_depth": common_depth,
            "forced": forced, "ranks": ranks, "reference_bestmove_stable": one["move"] == two["move"],
            "reference_cp_change": cp_delta,
            "stability_note": "Two finite node budgets agree or disagree; stability is not a guarantee of a correct chess answer."}


def run(args, *, engine_factory=LearningEngine.popen_uci, guard_factory=None):
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    learned = output / "learned-v2.nn"
    learned.write_bytes(Path(args.model).read_bytes())
    zero = output / "zero-v2.nn"
    make_zero(learned, zero)
    models = {"zero_v2": zero, "learned_v2": learned}
    document = {"schema": SCHEMA, "started_utc": utc_now(), "completed": False, "error": None,
                "binary": {"path": str(args.binary), "sha256": benchmark.digest(args.binary),
                           "declared_source_commit": args.source_commit},
                "models": {role: {"snapshot": path.name, "sha256": benchmark.digest(path)} for role, path in models.items()},
                "model_input": {"path": str(args.model), "sha256": benchmark.digest(args.model)},
                "fixture": {"path": str(args.positions), "sha256": benchmark.digest(args.positions), "document": args.position_document},
                "overlap_check": args.overlap, "environment": benchmark.environment(),
                "harness_sources": [{"path": "scripts/" + name, "sha256": benchmark.digest(ROOT / "scripts" / name)}
                    for name in ("diagnose_neural_cost.py", "benchmark_search.py", "neural_model.py", "classical_evaluation.py",
                                 "collect_learning_data.py", "learning_arena.py", "see_clean_guard.py", "train_neural.py")],
                "arm_options": {role: dict(OPTIONS, UseNeuralEvaluation=role != "hce",
                    **({"EvalFile": str(models[role])} if role != "hce" else {})) for role in ROLES},
                "configuration": {"depth": args.depth, "nodes": args.nodes, "movetime_ms": args.movetime_ms,
                                  "repeats": args.repeats, "warmups_per_role": args.warmups, "seed": args.seed,
                                  "teacher_nodes": [args.teacher_low_nodes, args.teacher_high_nodes]},
                "policy": {"history": "same complete declared FEN and legal move history; fresh UCI game per search clears TT",
                           "processes": "two persistent UCI processes: HCE baseline and candidate switched between zero/learned outside timed play; all model loads and process startup excluded",
                           "order": "all six role permutations once per block of six repetitions; position order seeded shuffle",
                           "timing": "wall includes newgame/isready/play transport; engine time separate; fixed-node partial-iteration work included in node count",
                           "strength": "development diagnostic only; no promotion, no arena positions reused, no learned model changes",
                           "teacher": "classical own engine only, finite deeper node budgets; forced moves at shared completed root depth with high-node cap"},
                "native_model_validation": {}, "engines": {}, "records": [], "warmup_records": [], "teacher": [], "resource_guard": {}}
    document["environment"]["numpy"] = np.__version__
    engines, guard = {}, None
    def save():
        clean = not document["resource_guard"].get("contaminated", True)
        document["summary"] = summarize(document["records"], clean=clean, completed=document["completed"])
        write_json(output / "diagnostic.json", document)
    try:
        for role, path in models.items():
            document["native_model_validation"][role] = validate_native_model(args.binary, path)
        if guard_factory is None:
            provider = WindowsProcesses(engine_names={"regicide.exe", "stockfish.exe", "lc0.exe"} | BROWSER_NAMES) if sys.platform == "win32" else LinuxProcesses() if sys.platform.startswith("linux") else None
            if provider is None:
                raise RuntimeError("resource guard supports Windows/Linux only")
            guard = EngineCpuGuard(provider, output / "resource-samples.jsonl", interval=.5)
        else:
            guard = guard_factory(output / "resource-samples.jsonl")
        guard.start()
        for role in ("baseline", "candidate"):
            engine = engine_factory([str(args.binary), "uci"], timeout=120)
            engines[role] = engine
            guard.register_transport(engine.transport.get_pid(), role, [str(args.binary), "uci"])
            options = dict(OPTIONS, UseNeuralEvaluation=False)
            if not all(name in engine.options for name in (*options, "EvalFile")):
                raise ValueError("engine does not advertise required diagnostic options")
            engine.configure(options)
            engine.ping()
            document["engines"][role] = {"id": dict(engine.id), "configured_options": options,
                "advertised_options": {name: {"type": item.type, "default": item.default, "min": item.min, "max": item.max, "var": item.var} for name, item in engine.options.items()}}
        guard.assert_transports()
        candidate_mode = None
        def selected(role):
            nonlocal candidate_mode
            if role == "hce":
                return engines["baseline"]
            engine = engines["candidate"]
            if candidate_mode != role:
                engine.configure({"EvalFile": str(models[role])})
                engine.configure({"UseNeuralEvaluation": True})
                engine.ping()
                candidate_mode = role
            return engine
        def checked_sample(role, position, limit, expected_depth=None):
            engine = selected(role)
            guard.assert_clean()
            value = sample(engine, position, limit, expected_depth=expected_depth)
            guard.assert_clean()
            return value
        for role in ROLES:
            for number in range(args.warmups):
                document["warmup_records"].append({"role": role, "number": number + 1,
                    "result": checked_sample(role, args.selected_positions[0], chess.engine.Limit(nodes=min(1000, args.nodes)))})
        for mode in MODES:
            limit = {"depth": chess.engine.Limit(depth=args.depth), "nodes": chess.engine.Limit(nodes=args.nodes),
                     "time": chess.engine.Limit(time=args.movetime_ms / 1000)}[mode]
            for repetition, index, order in schedule(args.selected_positions, args.repeats, args.seed):
                position = args.selected_positions[index]
                row = {"mode": mode, "repetition": repetition, "position_index": index, "position": position["name"], "order": order}
                document["records"].append(row)
                for role in order:
                    row[role] = checked_sample(role, position, limit, args.depth if mode == "depth" else None)
                if mode != "time":
                    row["hce_zero_differences"] = equivalence_difference(row["hce"], row["zero_v2"])
                    if row["hce_zero_differences"]:
                        raise RuntimeError("HCE/zero search work differs: " + ", ".join(row["hce_zero_differences"]))
                save()
        for index, position in enumerate(args.selected_positions):
            moves = {row[role]["move"] for row in document["records"] if row["position_index"] == index for role in ROLES}
            row = teacher_diagnosis(engines["baseline"], position, moves, args.teacher_low_nodes, args.teacher_high_nodes, before_search=guard.assert_clean)
            row.update(position=position["name"], position_index=index)
            document["teacher"].append(row)
            guard.assert_clean()
            save()
        guard.assert_transports()
        if benchmark.digest(args.binary) != document["binary"]["sha256"] or benchmark.digest(args.model) != document["model_input"]["sha256"] or benchmark.digest(args.positions) != document["fixture"]["sha256"]:
            raise RuntimeError("diagnostic input changed during run")
        if any(benchmark.digest(output / row["snapshot"]) != row["sha256"] for row in document["models"].values()):
            raise RuntimeError("model snapshot changed during run")
        if any(benchmark.digest(ROOT / row["path"]) != row["sha256"] for row in document["harness_sources"]):
            raise RuntimeError("diagnostic harness source changed during run")
        if any(benchmark.digest(Path(row["path"])) != row["sha256"] for row in document["overlap_check"].get("files", [])):
            raise RuntimeError("protected input changed during run")
        document["completed"] = True
    except (Exception, KeyboardInterrupt) as error:
        document["error"] = f"{type(error).__name__}: {error}"
    finally:
        for role, engine in reversed(list(engines.items())):
            try:
                engine.quit()
            except Exception as error:
                document["error"] = document["error"] or f"{role} cleanup: {error}"
            finally:
                engine.close()
        if guard:
            guard.close()
            document["resource_guard"] = copy.deepcopy(guard.summary)
            document["resource_guard"]["valid_for_comparison"] = bool(document["completed"] and not document["error"] and not guard.summary["contaminated"])
            document["resource_guard"]["scope"] += " Recognized browsers included; arbitrary host load, thermal drift and transient processes between snapshots remain unobserved."
        document["completed"] = bool(document["completed"] and not document["error"])
        document["finished_utc"] = utc_now()
        samples = output / "resource-samples.jsonl"
        document["resource_samples_sha256"] = benchmark.digest(samples) if samples.exists() else None
        save()
    return document


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--positions", type=Path, default=DEFAULT_POSITIONS)
    parser.add_argument("--exclude-manifest", action="append", type=Path, default=[])
    parser.add_argument("--depth", type=benchmark.positive_int, default=3)
    parser.add_argument("--nodes", type=benchmark.positive_int, default=10000)
    parser.add_argument("--movetime-ms", type=benchmark.positive_int, default=100)
    parser.add_argument("--repeats", type=benchmark.positive_int, default=6)
    parser.add_argument("--warmups", type=benchmark.nonnegative_int, default=1)
    parser.add_argument("--seed", type=int, default=20261019)
    parser.add_argument("--teacher-low-nodes", type=benchmark.positive_int, default=100000)
    parser.add_argument("--teacher-high-nodes", type=benchmark.positive_int, default=200000)
    args = parser.parse_args(argv)
    try:
        for name in ("binary", "model", "output", "positions"):
            setattr(args, name, getattr(args, name).expanduser().resolve())
        if not args.binary.is_file() or not args.model.is_file():
            raise ValueError("binary/model input does not exist")
        if args.output.exists():
            raise ValueError("output directory already exists; never overwrite diagnostic evidence")
        if args.depth > 64 or args.teacher_low_nodes >= args.teacher_high_nodes:
            raise ValueError("depth must be <=64 and teacher node budgets strictly increasing")
        args.source_commit = benchmark.verified_commit(args.source_commit)
        args.position_document, args.selected_positions = load_positions(args.positions)
        args.overlap = overlap_check(args.selected_positions, args.exclude_manifest)
        if load_model(args.model).version != 2:
            raise ValueError("diagnostic requires a V2 learned model")
    except (OSError, KeyError, TypeError, ValueError) as error:
        parser.error(str(error))
    return args


def main(argv=None):
    document = run(arguments(argv))
    print(json.dumps({"completed": document["completed"], "error": document["error"], "summary": document["summary"]}, ensure_ascii=False, indent=2))
    return 0 if document["completed"] and document["resource_guard"].get("valid_for_comparison") else 1


if __name__ == "__main__":
    sys.exit(main())
