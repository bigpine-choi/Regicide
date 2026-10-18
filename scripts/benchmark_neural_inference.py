"""Exact-work, cross-binary neural inference cost comparison, never a promotion arena.

Reuse the strict raw UCI score/PV validator and existing fail-closed resource
guard. Time-limited searches measure work capacity, not chess strength.
"""

import argparse
import json
from pathlib import Path
import random
import statistics
import sys

import chess.engine

import benchmark_search as benchmark
from collect_learning_data import LearningEngine, OPTIONS
import diagnose_neural_cost as diagnostic
from learning_arena import BROWSER_NAMES, LinuxProcesses, utc_now, write_json, validate_native_model
from see_clean_guard import EngineCpuGuard, WindowsProcesses

ROOT = Path(__file__).resolve().parents[1]
ROLES = ("baseline", "candidate")
MODES = ("depth", "nodes", "time")


def schedule(positions, repeats, seed):
    rng = random.Random(seed)
    for repeat in range(repeats):
        indices = list(range(len(positions)))
        rng.shuffle(indices)
        for index in indices:
            order = ROLES if (repeat + index) % 2 == 0 else ROLES[::-1]
            yield repeat + 1, index, order


def summary(records, *, valid):
    arms = sorted({row["arm"] for row in records})
    result = {"timing_valid": valid, "arms": {}}
    for arm in arms:
        modes = {}
        for mode in MODES:
            rows = [row for row in records if row["arm"] == arm and row["mode"] == mode
                    and all(role in row for role in ROLES)]
            ratios = [row["candidate"]["wall_time_seconds"] /
                      row["baseline"]["wall_time_seconds"] for row in rows]
            node_ratios = [row["candidate"]["nodes"] / row["baseline"]["nodes"] for row in rows]
            modes[mode] = {"pairs": len(rows), "unequal_fixed_work_pairs":
                          sum(bool(row.get("differences")) for row in rows) if mode != "time" else None,
                          "candidate_baseline_wall_median": statistics.median(ratios) if valid and ratios else None,
                          "candidate_baseline_nodes_median": statistics.median(node_ratios) if valid and node_ratios else None,
                          "wall_ratios": ratios, "node_ratios": node_ratios}
        result["arms"][arm] = modes
    return result


def run(args):
    output = args.output
    output.mkdir(parents=True, exist_ok=False)
    models = {}
    for name, source in args.model:
        target = output / (name + ".nn")
        target.write_bytes(source.read_bytes())
        models[name] = target
    document = {"schema": "regicide.neural.inference.cost.v1", "started_utc": utc_now(),
                "completed": False, "error": None, "records": [], "warmups": [],
                "environment": benchmark.environment(),
                "engines": {role: {"path": str(getattr(args, role)),
                     "sha256": benchmark.digest(getattr(args, role)),
                     "source_commit": getattr(args, role + "_commit")} for role in ROLES},
                "models": {name: {"path": str(path), "sha256": benchmark.digest(path)}
                           for name, path in models.items()},
                "fixture": {"path": str(args.positions), "sha256": benchmark.digest(args.positions),
                            "document": args.position_document},
                "configuration": {"repeats": args.repeats, "seed": args.seed,
                    "depth": args.depth, "nodes": args.nodes, "movetime_ms": args.movetime_ms,
                    "warmups_per_arm_engine": 2, "options": dict(OPTIONS)},
                "policy": {"order": "seeded positions; balanced adjacent old/new order over even repetitions",
                    "state": "same full history, fresh logical game/empty TT each search; two persistent processes",
                    "timing": "model loading and startup excluded; wall includes newgame/isready/play transport",
                    "equivalence": list(diagnostic.EQUIVALENCE_FIELDS),
                    "scope": "development cost measurement only; no training, strength estimate or model adoption"},
                "harness_sources": [{"path": "scripts/" + name, "sha256": benchmark.digest(ROOT / "scripts" / name)}
                    for name in ("benchmark_neural_inference.py", "diagnose_neural_cost.py", "benchmark_search.py",
                                 "collect_learning_data.py", "learning_arena.py", "see_clean_guard.py")],
                "resource_guard": {}}
    engines, guard = {}, None
    def save():
        valid = document["completed"] and not document["resource_guard"].get("contaminated", True)
        document["summary"] = summary(document["records"], valid=valid)
        write_json(output / "comparison.json", document)
    try:
        # UCI configuration errors are informational strings and can leave a
        # previous model active. Fail before timing on either native parser.
        document["native_model_validation"] = {
            role: {name: validate_native_model(getattr(args, role), path)
                   for name, path in models.items()} for role in ROLES}
        provider = WindowsProcesses(engine_names={"regicide.exe", "stockfish.exe", "lc0.exe"} | BROWSER_NAMES) if sys.platform == "win32" else LinuxProcesses()
        guard = EngineCpuGuard(provider, output / "resource-samples.jsonl", interval=.5)
        guard.start()
        guard.require_idle(5)
        for role in ROLES:
            command = [str(getattr(args, role)), "uci"]
            engine = LearningEngine.popen_uci(command, timeout=120)
            engines[role] = engine
            guard.register_transport(engine.transport.get_pid(), role, command)
            engine.configure(dict(OPTIONS, UseNeuralEvaluation=False))
            engine.ping()
            document["engines"][role]["id"] = dict(engine.id)
        guard.assert_transports()
        arms = {"hce": None, **models}
        for arm, model in arms.items():
            for engine in engines.values():
                if model is not None:
                    engine.configure({"EvalFile": str(model)})
                engine.configure({"UseNeuralEvaluation": model is not None})
                engine.ping()
            def sample(role, position, limit, expected=None):
                guard.assert_clean()
                value = diagnostic.sample(engines[role], position, limit, expected_depth=expected)
                guard.assert_clean()
                if limit.time is not None and value["wall_time_seconds"] > limit.time + max(.1, limit.time):
                    raise RuntimeError("time-limited search exceeded the response allowance")
                return value
            for role in ROLES:
                for number in range(2):
                    document["warmups"].append({"arm": arm, "role": role, "number": number + 1,
                        "result": sample(role, args.selected_positions[0], chess.engine.Limit(nodes=1000))})
            for mode in MODES:
                limit = {"depth": chess.engine.Limit(depth=args.depth),
                         "nodes": chess.engine.Limit(nodes=args.nodes),
                         "time": chess.engine.Limit(time=args.movetime_ms / 1000)}[mode]
                for repeat, index, order in schedule(args.selected_positions, args.repeats, args.seed):
                    position = args.selected_positions[index]
                    row = {"arm": arm, "mode": mode, "repeat": repeat, "position_index": index,
                           "position": position["name"], "order": list(order)}
                    document["records"].append(row)
                    for role in order:
                        row[role] = sample(role, position, limit, args.depth if mode == "depth" else None)
                    if mode != "time":
                        row["differences"] = diagnostic.equivalence_difference(row["baseline"], row["candidate"])
                        if row["differences"]:
                            raise RuntimeError("fixed work mismatch: " + ", ".join(row["differences"]))
                print(arm, mode, "complete", flush=True)
                document["resource_guard"] = dict(guard.summary)
                save()
        guard.assert_transports()
        document["completed"] = True
    except BaseException as error:
        document["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        for engine in engines.values():
            try:
                engine.quit()
            except Exception:
                engine.close()
        if guard is not None:
            guard.close()
            if guard.summary["contaminated"]:
                document["completed"] = False
            guard.summary["valid_for_comparison"] = bool(document["completed"] and not guard.summary["contaminated"])
            document["resource_guard"] = dict(guard.summary)
        document["finished_utc"] = utc_now()
        save()
    if not document["completed"]:
        raise RuntimeError("comparison did not finish cleanly")
    return document


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--baseline-commit", required=True)
    parser.add_argument("--candidate-commit", required=True)
    parser.add_argument("--model", action="append", nargs=2, metavar=("NAME", "PATH"), required=True)
    parser.add_argument("--positions", type=Path, default=diagnostic.DEFAULT_POSITIONS)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=benchmark.positive_int, default=6)
    parser.add_argument("--seed", type=int, default=20261019)
    parser.add_argument("--depth", type=benchmark.positive_int, default=3)
    parser.add_argument("--nodes", type=benchmark.positive_int, default=10000)
    parser.add_argument("--movetime-ms", type=benchmark.positive_int, default=100)
    args = parser.parse_args(argv)
    try:
        if args.repeats % 2:
            raise ValueError("repeats must be even to balance old/new order")
        for name in ("baseline", "candidate", "positions", "output"):
            setattr(args, name, getattr(args, name).resolve())
        for role in ROLES:
            setattr(args, role + "_commit", benchmark.verified_commit(getattr(args, role + "_commit")))
            if not getattr(args, role).is_file():
                raise ValueError("binary does not exist: " + role)
        if args.output.exists():
            raise ValueError("output already exists")
        names = [name for name, _ in args.model]
        if len(set(names)) != len(names) or "hce" in names or any(not name or not name.replace("_", "").isalnum() for name in names):
            raise ValueError("model names must be unique alphanumeric identifiers, distinct from hce")
        args.model = [(name, Path(path).resolve()) for name, path in args.model]
        for _, path in args.model:
            if not path.is_file():
                raise ValueError("model does not exist: " + str(path))
        args.position_document, args.selected_positions = diagnostic.load_positions(args.positions)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.error(str(error))
    return args


if __name__ == "__main__":
    run(arguments())
