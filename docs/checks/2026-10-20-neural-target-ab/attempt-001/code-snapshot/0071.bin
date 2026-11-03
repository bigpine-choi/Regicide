"""Independent, read-only replay of the paired-target neural experiment.

This auditor never calls the experiment/data/match driver's validation methods.
Python-chess is an oracle for legal histories, NumPy recomputes error summaries,
and the existing model loader is used only for the deployed integer file format.
--engine additionally checks every exported model against C++ on every row and
executes the C++ special-move/undo/search-cancellation regression executable.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import random
import subprocess
import sys

import chess
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import neural_precision as precision
import neural_training as base
from audit_see_results import audit_game, audit_pgn, effective_options

SEEDS = (20261020, 20261021, 20261022)
TARGETS = {"static": "classical_static_white_cp", "search10k": "target_white_cp"}
KINDS = ("selfplay_start", "synthetic_endgame")


def require(condition: object, message: str) -> None:
    if not condition:
        raise ValueError(message)


def sha256(path: Path | str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_json(path: Path | str) -> dict:
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    require(isinstance(value, dict), "JSON metadata is not an object")
    return value


def read_rows(path: Path | str) -> list[dict]:
    rows = [json.loads(line) for line in Path(path).read_text(encoding="utf-8-sig").splitlines()
            if line.strip()]
    require(all(isinstance(row, dict) for row in rows), "record is not an object")
    return rows


def feature_hash(board: chess.Board) -> str:
    return hashlib.sha256(min(board.board_fen(), board.mirror().board_fen()).encode("ascii")).hexdigest()


def state_hash(initial_fen: str, history: list[str]) -> str:
    payload = json.dumps({"initial_fen": initial_fen, "history": history},
                         sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def board_from(initial_fen: str, history: list[str]) -> chess.Board:
    require(isinstance(initial_fen, str) and len(initial_fen.split()) == 6, "incomplete initial FEN")
    require(isinstance(history, list) and all(isinstance(text, str) for text in history), "invalid history")
    board = chess.Board(initial_fen)
    require(board.is_valid(), "invalid orthodox initial board")
    for text in history:
        require(board.outcome(claim_draw=True) is None, "history continues after an outcome/draw claim")
        move = chess.Move.from_uci(text)
        require(move in board.legal_moves, "illegal history move " + text)
        board.push(move)
    return board


def audit_bank(bank: dict) -> dict:
    families = bank.get("master_partition")
    sources = bank.get("sources")
    require(isinstance(families, dict) and len(families) == 80, "bank must declare exactly 80 families")
    require(Counter(families.values()) == Counter({"train": 48, "validation": 16, "test": 16}),
            "bank family split differs from 48/16/16")
    require(isinstance(sources, list) and len(sources) == 160, "bank must contain two variants per family")
    grouped: dict[str, list[dict]] = defaultdict(list)
    identities: dict[str, str] = {}
    seed_ids = set()
    for source in sources:
        family = source.get("family_id")
        require(family in families and source.get("partition") == families[family], "source crosses family split")
        require(source.get("seed_id") not in seed_ids, "duplicate source seed ID")
        seed_ids.add(source["seed_id"])
        require(source.get("kind") in KINDS, "invalid source kind")
        board = board_from(source["initial_fen"], source["history"])
        require(board.outcome(claim_draw=True) is None, "source is already terminal")
        if source["kind"] == "selfplay_start":
            require(source["initial_fen"] == chess.STARTING_FEN, "normal family lost standard-start history")
        else:
            require(6 <= len(board.piece_map()) <= 12, "synthetic source is outside 6..12 pieces")
        identity = feature_hash(board)
        require(identity not in identities or identities[identity] == family, "source feature crosses families")
        identities[identity] = family
        parent = board_from(source["parent_initial_fen"], source["parent_history"])
        require(source["parent_feature_hash"] == feature_hash(parent), "parent input hash differs from replay")
        grouped[family].append(source)
    require(set(grouped) == set(families) and all(len(items) == 2 for items in grouped.values()),
            "bank does not contain exactly two variants for every family")
    strata = Counter()
    parents = {}
    for family, items in grouped.items():
        require(len({(item["kind"], item["subtype"], item["parent_feature_hash"]) for item in items}) == 1,
                "two variants disagree about their family parent/type")
        source = items[0]
        key = source["parent_feature_hash"]
        require(key not in parents, "feature-equivalent parents were assigned different family names")
        parents[key] = family
        strata[(families[family], source["kind"], source["subtype"])] += 1
    for partition, normal, endgame in (("train", 24, 6), ("validation", 8, 2), ("test", 8, 2)):
        require(strata[(partition, "selfplay_start", "normal")] == normal, "wrong normal-family stratum")
        for subtype in ("pawn", "rook", "minor", "queen"):
            require(strata[(partition, "synthetic_endgame", subtype)] == endgame, "wrong endgame-family stratum")
    return {"families": len(families), "variants": len(sources), "family_split": dict(Counter(families.values()))}


def audit_row(row: dict, query: dict, partition: str, family_split: dict,
              *, node_budget: int = 10000) -> str:
    board = board_from(row["initial_fen"], row["history"])
    require(row["fen"] == board.fen(en_passant="fen"), "sample FEN differs from original root")
    identity = feature_hash(board)
    require(row["feature_hash"] == identity, "sample input identity differs from pieces")
    require(row["state_hash"] == state_hash(row["initial_fen"], row["history"]), "sample state hash differs")
    require(row["partition"] == partition == family_split[row["family_id"]], "sample crosses family partition")
    require(query["query_id"] == row["query_id"] and query["sampled"] is True,
            "sample lacks an accepted query")
    for name in ("fen", "initial_fen", "history", "feature_hash", "state_hash", "family_id", "game_id", "ply",
                 "partition", "kind", "pv_uci", "bestmove_uci", "depth", "nodes", "requested_nodes"):
        require(row[name] == query[name], "sample/query original-root linkage differs: " + name)
    require(query["completed"] is True and not query.get("excluded_reasons"), "accepted query is incomplete/excluded")
    require(query["score"]["kind"] == "cp" and type(query["score"]["stm"]) is int, "target is not exact integer cp")
    require(not query.get("lowerbound", False) and not query.get("upperbound", False), "bound accepted as target")
    require(type(row["depth"]) is int and row["depth"] >= 1, "zero-depth target")
    require(type(row["nodes"]) is int and 1 <= row["nodes"] <= node_budget
            and row["requested_nodes"] == node_budget, "target node budget differs")
    require(board.outcome(claim_draw=True) is None and not board.is_check(), "terminal/check training sample")
    pv = query["pv_uci"]
    require(isinstance(pv, list) and pv and pv[0] == query["bestmove_uci"], "PV/root move mismatch")
    variation = board.copy(stack=True)
    for index, text in enumerate(pv):
        move = chess.Move.from_uci(text)
        require(move in variation.legal_moves, "illegal query PV")
        if index == 0:
            require(not variation.is_capture(move) and move.promotion is None, "tactical root bypassed quiet filter")
        variation.push(move)
    for key in ("search_stm_cp", "target_white_cp", "classical_static_stm_cp", "classical_static_white_cp"):
        require(type(row[key]) is int, "paired target is not an integer: " + key)
    sign = 1 if board.turn else -1
    require(row["search_stm_cp"] == query["score"]["stm"], "search target was substituted")
    require(row["target_white_cp"] == sign * query["score"]["stm"], "search target perspective differs")
    require(row["classical_static_white_cp"] == sign * row["classical_static_stm_cp"], "static target perspective differs")
    require(abs(row["target_white_cp"]) <= 28000 and abs(row["classical_static_white_cp"]) <= 28000,
            "paired target outside representable range")
    require("classical_stm_cp" not in row, "schema2/3 search rows masquerade as static-only rows")
    return identity


def audit_partitions(partitions: dict[str, list[dict]], queries: dict[str, dict],
                     family_split: dict, exclusions: set[str], *, minima: bool = True) -> dict:
    seen: set[str] = set(exclusions)
    counts = {}
    for partition in ("train", "validation", "test"):
        if partition not in partitions:
            continue
        records = partitions[partition]
        require(bool(records), "empty sample partition")
        require(len({row["query_id"] for row in records}) == len(records), "duplicate sample query ID")
        kinds = Counter()
        families = Counter()
        for row in records:
            require(row["query_id"] in queries, "sample query absent")
            identity = audit_row(row, queries[row["query_id"]], partition, family_split)
            require(identity not in seen, "mirrored input leaked across partitions or prior evidence")
            seen.add(identity)
            kinds[row["kind"]] += 1
            families[row["family_id"]] += 1
        if minima:
            require(len(records) >= (1000 if partition == "train" else 300), "partition below planned sample minimum")
            require(all(kinds[kind] >= 100 for kind in KINDS), "partition source-kind minimum unmet")
            if partition != "train":
                require(sum(count >= 4 for count in families.values()) >= 12, "too few populated held-out families")
        counts[partition] = {"rows": len(records), "source_kinds": dict(kinds), "families": dict(families)}
    return counts


def error_metrics(predictions: list[float] | np.ndarray, targets: list[float] | np.ndarray) -> dict:
    predictions, targets = np.asarray(predictions, dtype=np.float64), np.asarray(targets, dtype=np.float64)
    require(predictions.shape == targets.shape and predictions.ndim == 1 and len(targets) > 0,
            "invalid metric arrays")
    require(np.all(np.isfinite(predictions)) and np.all(np.isfinite(targets)), "nonfinite metric input")
    absolute = np.abs(predictions - targets)
    selected = np.abs(targets) >= 50
    return {"count": len(targets), "mae_cp": float(np.mean(absolute)),
            "rmse_cp": float(np.sqrt(np.mean((predictions - targets) ** 2))),
            "p95_abs_error_cp": float(np.percentile(absolute, 95)),
            "abs_error_ge_100_fraction": float(np.mean(absolute >= 100)),
            "opposite_sign_ref_abs_ge_50_fraction": float(np.mean(predictions[selected] * targets[selected] < 0))
                if np.any(selected) else None}


def family_comparison(rows: list[dict], predictions: dict[str, list[int]], targets: list[int]) -> dict:
    require(len(rows) == len(targets) and all(len(values) == len(rows) for values in predictions.values()),
            "metric row alignment mismatch")
    by_family: dict[str, list[int]] = defaultdict(list)
    kinds = {}
    for index, row in enumerate(rows):
        family = row["family_id"]
        by_family[family].append(index)
        require(family not in kinds or kinds[family] == row["kind"], "family crosses source strata")
        kinds[family] = row["kind"]
    target_array = np.asarray(targets, dtype=np.float64)
    matrix, family_errors = {}, {}
    for model, values in predictions.items():
        array = np.asarray(values, dtype=np.float64)
        per_family = {family: float(np.mean(np.abs(array[indices] - target_array[indices])))
                      for family, indices in by_family.items()}
        family_errors[model] = per_family
        matrix[model] = {"micro": error_metrics(array, target_array), "family_mae_cp": per_family,
                         "family_macro_mae_cp": float(np.mean(list(per_family.values())))}
    paired = []
    for seed in SEEDS:
        a, b = "A-" + str(seed), "B-" + str(seed)
        require(a in family_errors and b in family_errors, "missing paired seed model")
        delta = {family: family_errors[a][family] - family_errors[b][family] for family in by_family}
        paired.append({"seed": seed, "family_delta_a_minus_b_cp": delta,
                       "family_macro_delta_a_minus_b_cp": float(np.mean(list(delta.values())))})
    means = {family: float(np.mean([pair["family_delta_a_minus_b_cp"][family] for pair in paired]))
             for family in by_family}
    rng = np.random.default_rng(20261029)
    strata = [[family for family in sorted(by_family) if kinds[family] == kind] for kind in KINDS]
    require(all(strata), "bootstrap needs both source strata")
    estimates = []
    for _ in range(10000):
        chosen = [means[stratum[index]] for stratum in strata
                  for index in rng.integers(0, len(stratum), size=len(stratum))]
        estimates.append(float(np.mean(chosen)))
    return {"matrix": matrix, "pairs": paired, "bootstrap": {
        "seed": 20261029, "resamples": 10000, "interval": np.percentile(estimates, [2.5, 97.5]).tolist(),
        "mean_delta_a_minus_b_cp": float(np.mean(list(means.values()))), "families": len(by_family)}}


def audit_runtime(engine: Path, models: list[dict], partitions: dict[str, list[dict]]) -> dict:
    from neural_training_integration import special_positions
    fens = [row["fen"] for rows in partitions.values() for row in rows] + special_positions()
    reports = []
    model_tests = engine.with_name("regicide_neural_model_tests" + engine.suffix)
    for entry in models:
        path = Path(entry["path"])
        require(sha256(path) == entry["sha256"], "model hash changed before runtime audit")
        model = precision.load_model(path)
        require(model["input_scale"] == 32 and model["clip"] == 4064
                and model["output_scale"] == 256, "export precision differs from S32 plan")
        scores = base.evaluate_engine(engine, fens, path)
        require(len(scores) == len(fens), "C++ evaluation output count differs")
        for fen, triple in zip(fens, scores, strict=True):
            board = chess.Board(fen)
            expected = precision.quantized_white(board, model) * (1 if board.turn else -1)
            require(triple[1] == triple[2] == expected, "Python/full/incremental mismatch in " + entry["model_id"])
        original_rows = [row for records in partitions.values() for row in records]
        require(all(triple[0] == row["classical_static_stm_cp"] for row, triple in zip(original_rows, scores)),
                "static target differs from the frozen C++ evaluator")
        completed = subprocess.run([str(model_tests), str(path)], capture_output=True, text=True, timeout=120)
        require(completed.returncode == 0 and "0 failures" in completed.stdout,
                "loaded model special-move/undo/search regression failed: " + completed.stdout + completed.stderr)
        require(sha256(path) == entry["sha256"], "runtime test mutated a model")
        reports.append({"model_id": entry["model_id"], "positions": len(fens),
                        "cpp_special_move_undo_search_test": completed.stdout.splitlines()})
    return {"engine_sha256": sha256(engine), "models": reports}


def checked_files(files: dict) -> dict:
    result = {}
    for key, entry in files.items():
        require(isinstance(entry, dict) and set(entry) >= {"path", "sha256"}, "malformed frozen file")
        path = Path(entry["path"])
        require(path.is_absolute() and sha256(path) == entry["sha256"], "frozen bytes changed: " + key)
        result[str(path)] = entry["sha256"]
    return result


def audit_dataset(directory: Path, bank: dict, prior_features: set[str]) -> tuple[dict, dict[str, list[dict]]]:
    manifest, plan = read_json(directory / "manifest.json"), read_json(directory / "plan.json")
    require(manifest["schema_version"] == plan["schema_version"] == 3, "wrong paired dataset schema")
    require(manifest["status"] == "complete" and manifest["error"] is None, "incomplete dataset")
    require(manifest["target_mapping"] == plan["target_mapping"] ==
            {"A": "classical_static_white_cp", "B": "target_white_cp"}, "paired target mapping changed")
    for filename, expected in manifest["files"].items():
        require(Path(filename).name == filename and sha256(directory / filename) == expected,
                "dataset artifact changed: " + filename)
    require(read_json(directory / "bank.json") == bank and plan["master_partition"] == bank["master_partition"],
            "data generation used a different master bank")
    config = plan["config"]
    for key, value in {"nodes": 10000, "max_plies": 96, "sample_every": 3, "search_timeout": 10,
                       "quiet": True, "hash_mib": 16}.items():
        require(config[key] == value, "generation setting changed: " + key)
    requested = plan["requested_partitions"]
    require(requested in (["train", "validation"], ["test"]), "unplanned data stage")
    exclusions = set(plan["excluded_features"])
    require(exclusions == set(bank["exclusions"]["feature_hashes"]) | prior_features,
            "stage excludes inputs other than the fixed prior sources and earlier stage")
    queries_list, games = read_rows(directory / "queries.jsonl"), read_rows(directory / "games.jsonl")
    queries = {row["query_id"]: row for row in queries_list}
    require(len(queries) == len(queries_list), "duplicate query ID")
    sources = [source for source in bank["sources"] if source["partition"] in requested]
    require(len(games) == len(sources), "not every frozen source was processed")
    query_ids = []
    for game, source in zip(games, sources, strict=True):
        for key in ("seed_id", "family_id", "partition", "kind", "subtype", "initial_fen"):
            require(game[key] == source[key], "generated game differs from frozen source")
        history = game["moves_uci"]
        offset = len(source["history"])
        require(game["initial_history"] == source["history"] == history[:offset], "source prefix changed")
        board = board_from(game["initial_fen"], history)
        require(game["final_fen"] == board.fen(en_passant="fen"), "game final board differs")
        require(game["generated_plies"] == len(history) - offset <= 96, "game ply count changed")
        outcome = board.outcome(claim_draw=True)
        if outcome is None:
            require(game["termination"] == "max_plies" and game["generated_plies"] == 96
                    and game["result"] is None, "unfinished data trajectory was assigned a result")
        else:
            require(game["termination"] == "outcome" and game["result"] == outcome.result(), "data outcome differs")
        require(len(game["query_ids"]) == game["generated_plies"], "game query/move count differs")
        for ply, query_id in enumerate(game["query_ids"]):
            query_ids.append(query_id)
            query = queries[query_id]
            require(query["game_id"] == game["game_id"] and query["ply"] == ply
                    and query["history"] == history[:offset + ply], "query is not its original game root")
            require(query["bestmove_uci"] == history[offset + ply], "teacher trajectory differs from own bestmove")
    require(query_ids == list(queries), "queries reordered, missing or extra")
    controls = read_rows(directory / "controls.jsonl")
    control_map = {row["query_id"]: row for row in controls}
    require(len(control_map) == len(controls), "duplicate static control")
    parts = {name: read_rows(directory / (name + ".jsonl")) for name in requested}
    for name, rows in parts.items():
        entry = manifest["partitions"][name]
        require(entry["count"] == len(rows) and entry["sha256"] == sha256(directory / (name + ".jsonl")),
                "partition count/hash mismatch")
        for row in rows:
            control = control_map.get(row["query_id"])
            require(control is not None and control["fen"] == row["fen"]
                    and control["engine_sha256"] == plan["engine"]["sha256"], "missing static control provenance")
            for key in ("classical_static_stm_cp", "classical_static_white_cp"):
                require(control[key] == row[key] == queries[row["query_id"]][key], "static label was substituted")
    counts = audit_partitions(parts, queries, bank["master_partition"], exclusions)
    accepted = {row["query_id"] for records in parts.values() for row in records}
    require(accepted == {query["query_id"] for query in queries_list if query["sampled"]},
            "accepted query missing from paired rows")
    require(manifest["stats"]["searches"] == len(queries) and manifest["stats"]["games"] == len(games)
            and manifest["stats"]["actual_nodes"] == sum(query["nodes"] or 0 for query in queries_list),
            "dataset resource accounting mismatch")
    audit_teacher_uci(directory / "uci.jsonl", queries_list)
    return {"partitions": counts, "searches": len(queries), "nodes": manifest["stats"]["actual_nodes"],
            "games": len(games), "controls": len(controls), "manifest_sha256": sha256(directory / "manifest.json")}, parts


def check_metric(recorded: dict, predictions: list[int], targets: list[int], rows: list[dict]) -> None:
    expected = error_metrics(predictions, targets)
    for name in ("count", "mae_cp", "rmse_cp"):
        require(math.isclose(recorded[name], expected[name], rel_tol=1e-12, abs_tol=1e-9), "recorded metric differs: " + name)
    by_family = defaultdict(list)
    for prediction, target, row in zip(predictions, targets, rows, strict=True):
        by_family[row["family_id"]].append(abs(prediction-target))
    per_family = {key: float(np.mean(values)) for key, values in by_family.items()}
    require(recorded["family_mae_cp"] == per_family, "family MAE differs from aligned row predictions")
    require(math.isclose(recorded["family_macro_mae_cp"], float(np.mean(list(per_family.values()))),
                         rel_tol=1e-12, abs_tol=1e-9), "family macro average differs")
    for recorded_key, our_key in (("p95_absolute_error_cp", "p95_abs_error_cp"),
                                  ("absolute_error_ge100_fraction", "abs_error_ge_100_fraction"),
                                  ("opposite_sign_margin50_fraction", "opposite_sign_ref_abs_ge_50_fraction")):
        require(recorded[recorded_key] == expected[our_key], "distribution metric differs: " + recorded_key)


def audit_training(root: Path, frozen: dict, parts: dict[str, list[dict]]) -> tuple[dict, list[dict]]:
    document = read_json(root / "training/models.json")
    require(document["freeze_sha256"] == sha256(root / "freeze.json")
            and document["initial_sha256"] == frozen["initial"]["sha256"], "model manifest source/initializer differs")
    model_freeze = read_json(root / "training/model-freeze.json")
    model_files = checked_files(model_freeze["files"])
    require(model_freeze["holdout_did_not_exist"] is True, "holdout existed before model freeze")
    if (root / "heldout/manifest.json").is_file():
        heldout = read_json(root / "heldout/manifest.json")
        require(dt.datetime.fromisoformat(heldout["finished_utc"]) > dt.datetime.fromisoformat(model_freeze["created_utc"]),
                "holdout was published before model freeze")
    models = document["models"]
    require(len(models) == 6 and {(item["arm"], item["seed"]) for item in models}
            == {(arm, seed) for arm in ("A", "B") for seed in SEEDS}, "not all three model pairs")
    initial = precision.load_float_parameters(frozen["initial"]["path"])
    require(all(np.array_equal(initial[key], base.bootstrap_float()[key]) for key in precision.PARAMETER_KEYS),
            "initializer is not the own untrained arrays")
    init_model = precision.quantize(initial, 32)
    indexes, _ = base.pack_records(parts["train"])
    input_hash = hashlib.sha256(np.asarray(indexes, dtype="<i8").tobytes()).hexdigest()
    report = []
    for item in models:
        model_id = f"{item['arm']}-{item['seed']}"
        require(item["model_id"] == model_id and sha256(item["path"]) == item["sha256"], "model identity/hash differs")
        require(model_files.get(str(Path(item["path"]))) == item["sha256"], "final model absent from freeze")
        metrics = read_json(root / "training" / model_id / "metrics.json")
        history = json.loads((root / "training" / model_id / "history.json").read_text(encoding="utf-8"))
        target = "classical_static_white_cp" if item["arm"] == "A" else "target_white_cp"
        require(metrics["target_key"] == target and metrics["initial_sha256"] == frozen["initial"]["sha256"],
                "arm target/initialization changed")
        cfg = metrics["config"]
        require(cfg == {"candidate_id": model_id, "input_scale": 32, "epochs": 120, "input_bias_lr": .00002,
                        "output_lr": .005, "seed": item["seed"], "batch_size": 128}, "training configuration changed")
        require(len(history) == 120 and [row["epoch"] for row in history] == list(range(1, 121)), "not 120 final epochs")
        require(metrics["wall_seconds"] <= 900, "training exceeded planned model budget")
        rng, order = np.random.default_rng(item["seed"]), hashlib.sha256()
        for _ in range(120):
            order.update(rng.permutation(len(parts["train"])).astype("<i8").tobytes())
        require(metrics["feature_sha256"] == input_hash and metrics["batch_order_sha256"] == order.hexdigest(),
                "arm input/batch order is not the fixed paired order")
        model = precision.load_model(item["path"])
        require(precision.model_checksum(model) == item["checksum"], "model integer checksum differs")
        parameters = precision.load_float_parameters(root / "training" / model_id / "float_weights.npz")
        quantized = precision.quantize(parameters, 32)
        require(all(np.array_equal(model[key], quantized[key]) for key in precision.PARAMETER_KEYS),
                "published checkpoint differs from final shadow arrays")
        ys = [row[target] for row in parts["train"]]
        check_metric(metrics["initial_train"], precision.predict(init_model, indexes).tolist(), ys, parts["train"])
        check_metric(metrics["final_train"], precision.predict(model, indexes).tolist(), ys, parts["train"])
        for partition, records in parts.items():
            x, _ = base.pack_records(records)
            prediction = precision.predict(model, x).tolist()
            for kind, target_key in TARGETS.items():
                check_metric(metrics["metrics"][partition][kind], prediction, [row[target_key] for row in records], records)
        require(metrics["changed_integer_parameters"] == {key: int(np.count_nonzero(model[key] != init_model[key]))
                                                          for key in precision.PARAMETER_KEYS}, "parameter change count differs")
        report.append({"model_id": model_id, "initial_mae_own_target_cp": metrics["initial_train"]["mae_cp"],
                       "final_mae_own_target_cp": metrics["final_train"]["mae_cp"],
                       "wall_seconds": metrics["wall_seconds"], "batch_order_sha256": order.hexdigest()})
    return {"models": report, "fixed_epochs": 120, "input_sha256": input_hash, "frozen_files": len(model_files)}, models


def audit_evaluation(root: Path, frozen: dict, models: list[dict], rows: list[dict]) -> dict:
    comparison = read_json(root / "evaluation/comparison.json")
    records = read_rows(root / "evaluation/predictions.jsonl")
    refs = read_rows(root / "evaluation/reference_queries.jsonl")
    require(len(records) == len(refs) == len(rows) == comparison["holdout_rows"], "reference/prediction count differs")
    descriptors = models + [{"model_id": "M0", **frozen["m0"]}, {"model_id": "previous-s32", **frozen["previous_model"]}]
    x, _ = base.pack_records(rows)
    predictions = {item["model_id"]: precision.predict(precision.load_model(item["path"]), x).tolist() for item in descriptors}
    valid = []
    references = []
    for index, (row, reference, record) in enumerate(zip(rows, refs, records, strict=True)):
        require(reference["row_query_id"] == record["row_query_id"] == row["query_id"], "reference/prediction row order differs")
        require(reference["fen"] == record["fen"] == row["fen"] and reference["history"] == row["history"]
                and reference["initial_fen"] == row["initial_fen"], "reference is not the original held-out root")
        board = board_from(row["initial_fen"], row["history"])
        require(reference["requested_nodes"] == 100000, "larger reference budget changed")
        value = reference["reference_white_cp"]
        if value is not None:
            require(reference["completed"] is True and reference["score"]["kind"] == "cp"
                    and not reference["excluded_reasons"] and not reference.get("lowerbound")
                    and not reference.get("upperbound") and 0 < reference["nodes"] <= 100000
                    and reference["depth"] >= 1, "invalid exact common-reference score")
            require(value == reference["score"]["stm"] * (1 if board.turn else -1), "common reference perspective differs")
            variation = board.copy(stack=True)
            for text in reference["pv_uci"]:
                move = chess.Move.from_uci(text)
                require(move in variation.legal_moves, "illegal larger-reference PV")
                variation.push(move)
            valid.append(index)
        require(record["static_white_cp"] == row["classical_static_white_cp"]
                and record["search10k_white_cp"] == row["target_white_cp"]
                and record["search100k_white_cp"] == value, "published common targets differ")
        require(record["predictions_white_cp"] == {key: values[index] for key, values in predictions.items()},
                "published model prediction differs from frozen integer model")
        references.append(value)
    ref_rows = [rows[index] for index in valid]
    for model_id, values in predictions.items():
        for key, target_key in TARGETS.items():
            check_metric(comparison["matrix"][model_id][key], values, [row[target_key] for row in rows], rows)
        check_metric(comparison["matrix"][model_id]["search100k"], [values[index] for index in valid],
                     [references[index] for index in valid], ref_rows)
    own = family_comparison(ref_rows, {key: [values[index] for index in valid] for key, values in predictions.items()},
                            [references[index] for index in valid])
    delta = comparison["paired_delta"]
    require(delta["mean_delta_cp"] == own["bootstrap"]["mean_delta_a_minus_b_cp"]
            and delta["bootstrap_percentile95_cp"] == own["bootstrap"]["interval"], "family-cluster delta/bootstrap differs")
    for pair in own["pairs"]:
        require(delta["seed_family_deltas_cp"][str(pair["seed"])] == pair["family_delta_a_minus_b_cp"],
                "paired seed family deltas differ")
    require(comparison["reference_all_planned_attempted"] is True and comparison["reference_valid_cp"] == len(valid),
            "not all references attempted or coverage misreported")
    audit_teacher_uci(root / "evaluation/reference_uci.jsonl", refs)
    return {"rows": len(rows), "valid_cp": len(valid), "coverage": len(valid)/len(rows),
            "all_eight_model_matrices_recomputed": True, "paired_delta": own["bootstrap"]}


def position_command(line: str) -> tuple[str, list[str]]:
    fields = line.split()
    require(fields and fields[0] == "position", "missing UCI position")
    if fields[1] == "startpos":
        initial, offset = chess.STARTING_FEN, 2
    else:
        require(fields[1] == "fen" and len(fields) >= 8, "malformed UCI initial FEN")
        initial, offset = " ".join(fields[2:8]), 8
    require(len(fields) == offset or fields[offset] == "moves", "malformed UCI history marker")
    return initial, fields[offset+1:] if len(fields) > offset else []


def parse_info(line: str) -> dict:
    fields, result = line.split(), {}
    for key in ("depth", "seldepth", "nodes", "time"):
        if key in fields:
            result[key] = int(fields[fields.index(key)+1])
    if "score" in fields:
        offset = fields.index("score")
        result["score"] = {"kind": fields[offset+1], "stm": int(fields[offset+2])}
    if "pv" in fields:
        result["pv_uci"] = fields[fields.index("pv")+1:]
    return result


def audit_teacher_uci(path: Path, queries: list[dict]) -> dict:
    raw = read_rows(path)
    require([row["seq"] for row in raw] == list(range(1, len(raw)+1)), "raw UCI sequence has gaps/reordering")
    previous_end = 0
    for query in queries:
        begin, end = query["uci_begin_seq"], query["uci_end_seq"]
        require(previous_end < begin <= end <= len(raw), "query UCI ranges overlap or are empty")
        previous_end = end
        part = raw[begin-1:end]
        sends = [row["line"] for row in part if row["direction"] == "send"]
        require(sends.count("ucinewgame") == 1 and sends.count(f"go nodes {query['requested_nodes']}") == 1,
                "teacher query lacks a fresh game and exact fixed-node request")
        positions = [line for line in sends if line.startswith("position ")]
        require(len(positions) == 1, "query does not have exactly one original root")
        initial, history = position_command(positions[0])
        require(initial == query["initial_fen"] and history == query["history"], "raw teacher position/history differs")
        receives = [row["line"] for row in part if row["direction"] == "recv"]
        best = [line for line in receives if line.startswith("bestmove ")]
        require(len(best) == 1 and best[0].split()[1] == query["bestmove_uci"], "raw teacher bestmove differs")
        info = {}
        for line in receives:
            if line.startswith("info depth "):
                info.update(parse_info(line))
        for key in ("score", "depth", "seldepth", "nodes", "pv_uci"):
            require(info.get(key, [] if key == "pv_uci" else None) == query[key], "raw teacher report differs: " + key)
    require(sum(row["direction"] == "send" and row["line"].startswith("go ") for row in raw) == len(queries),
            "unaccounted raw teacher searches")
    return {"queries": len(queries), "raw_lines": len(raw)}


def audit_monitor(directory: Path, document: dict) -> dict:
    summary, raw = read_json(directory / "monitor.json"), read_rows(directory / "monitor.jsonl")
    require(summary["contaminated"] is False and summary["valid_for_comparison"] is True
            and summary["external_cpu_increase_ticks"] == 0, "native engine CPU guard invalidated block")
    require(summary["poll_seconds"] == .5 and summary["max_allowed_snapshot_gap_seconds"] == 2
            and summary["external_cpu_tolerance_ticks"] == 0, "CPU guard policy changed")
    require(len(raw) == summary["snapshot_count"] and raw[0]["phase"] == "initial"
            and raw[-1]["phase"] == "finished", "native snapshot coverage missing")
    registered = {(item["pid"], item["creation_ticks"]): role
                  for role, item in summary["transport_engines"].items()}
    require(len(registered) == 2 and set(registered.values()) == {"baseline", "candidate"}, "guard transports not two actual roles")
    for role, item in summary["transport_engines"].items():
        require(item["pid"] == document["engines"][role]["pid"]
                and item["command"] == document["engines"][role]["command"], "guard process differs from UCI transport")
    first_external, previous, seen_owned, phases, max_gap = None, None, set(), [], 0.0
    previous_elapsed, idle = None, None
    for row in raw:
        require(row["contaminated"] is False, "contaminated raw monitor snapshot")
        gap = row["snapshot_duration_seconds"] if previous_elapsed is None else row["elapsed_seconds"]-previous_elapsed
        require(0 <= gap <= 2 and abs(gap-row["gap_seconds"]) < 1e-7
                and row["snapshot_duration_seconds"] <= 2, "uncovered native snapshot gap")
        previous_elapsed, max_gap = row["elapsed_seconds"], max(max_gap, gap)
        external = {}
        for process in row["processes"]:
            identity = (process["pid"], process["creation_ticks"])
            require(not process.get("error"), "unreadable native engine CPU evidence")
            if process["owned"]:
                require(identity in registered, "excluded owned process is not an actual UCI engine")
                seen_owned.add(identity)
            else:
                require(identity not in registered and not process["exited_during_snapshot"], "invalid external process identity")
                external[identity] = process["cpu_ticks"]
        if first_external is None:
            first_external = external.copy()
        require(set(external) == set(first_external), "external engine identity set changed")
        delta = 0 if previous is None else sum(external[key]-previous[key] for key in external)
        require(delta == row["external_cpu_delta_ticks"] == 0, "external engine consumed native CPU ticks")
        previous = external
        phases.append(row["phase"])
        if row["phase"] == "idle-complete":
            require(idle is None and not seen_owned, "engines launched before idle completion")
            idle = row
    require(idle is not None and idle["elapsed_seconds"]-raw[0]["elapsed_seconds"] >= 10, "missing ten-second idle guard")
    require(seen_owned == set(registered), "a declared UCI engine has no sampled native identity")
    require(abs(summary["max_snapshot_gap_seconds"]-max_gap) < 1e-7, "maximum monitor gap differs")
    game_phases = [phase for phase in phases if phase in ("before-game", "after-game")]
    require(game_phases == [phase for _ in document["games"] for phase in ("before-game", "after-game")],
            "native guard does not bracket every game")
    return {"snapshots": len(raw), "external_processes": len(first_external), "external_cpu_increase_ticks": 0,
            "maximum_gap_seconds": max_gap, "scope": "Known engine processes observed by the guard; not host-wide zero load"}


def audit_match_uci(directory: Path, document: dict) -> dict:
    raw = read_rows(directory / "uci.jsonl")
    require([row["seq"] for row in raw] == list(range(1, len(raw)+1)), "match raw UCI sequence changed")
    positions, pending, current_model, model_ack = {}, {}, {}, {}
    expected = [(game, move) for game in document["games"] for move in game["search_moves"]]
    index = 0
    for row in raw:
        role, line = row["role"], row["line"]
        if row["direction"] == "send":
            if line.startswith("setoption name NeuralModelFile value "):
                current_model[role] = line.split(" value ", 1)[1]
                model_ack[role] = None
            elif line.startswith("position "):
                positions[role] = position_command(line)
            elif line.startswith("go "):
                require(line == "go movetime 100" and role not in pending, "raw match limit differs or concurrent search in one role")
                require(index < len(expected), "unrecorded raw match search")
                game, move = expected[index]
                require(move["role"] == role and game["uci_seq_before"] < row["seq"] <= game["uci_seq_after"],
                        "raw UCI search differs from recorded game/turn order")
                initial, history = positions[role]
                require(initial == game["initial_fen"]
                        and history == game["uci_moves"][:move["ply"]-1], "raw match history differs from full game prefix")
                require(current_model.get(role) == game["models"][role]["path"]
                        and model_ack.get(role) == game["models"][role]["checksum"], "search used wrong or unacknowledged model")
                pending[role] = {"game": game, "move": move, "info": {}}
        elif row["direction"] == "recv":
            if line.startswith("info string neural model checksum "):
                model_ack[role] = int(line.split()[-1])
            elif line.startswith("info depth ") and role in pending:
                pending[role]["info"].update(parse_info(line))
            elif line.startswith("bestmove "):
                require(role in pending, "raw bestmove lacks a recorded search")
                query = pending.pop(role)
                move, info = query["move"], query["info"]
                require(line.split()[1] == move["move"], "raw selected move differs from played move")
                for recorded, raw_key in (("depth", "depth"), ("seldepth", "seldepth"), ("nodes", "nodes"), ("pv", "pv_uci")):
                    require(move[recorded] == info.get(raw_key), "raw match info differs: " + recorded)
                require(abs(move["engine_time_seconds"]-info["time"]/1000) < 1e-9, "raw engine search time differs")
                index += 1
    require(not pending and index == len(expected), "raw UCI search/record count mismatch")
    for game in document["games"]:
        for role, ack in game["model_acks"].items():
            observed = raw[ack["seq"]-1]
            require(all(observed[key] == ack[key] for key in observed)
                    and ack["pid"] == document["engines"][role]["pid"], "model acknowledgement differs from actual raw engine")
    return {"searched_moves": index, "raw_lines": len(raw), "all_model_acknowledgements_and_full_histories_checked": True}


def audit_matches(directory: Path, bank: dict, models: list[dict]) -> dict:
    document = read_json(directory / "match.json")
    require(document["completed"] is True and document["valid_for_comparison"] is True
            and not document["errors"] and document["exit_code"] == 0, "100ms block incomplete or invalid")
    require(document["limits"] == {"nodes": None, "depth": None, "movetime_ms": 100}
            and document["max_plies"] == 500 and document["draw_policy"] == "claim", "match rules/time controls changed")
    require(len(document["games"]) == document["games_requested"] == 96, "not all 96 planned games completed")
    model_map = {(item["arm"], item["seed"]): item for item in models}
    points, per_seed, per_family, plies = [], defaultdict(list), defaultdict(list), 0
    for number, game in enumerate(document["games"]):
        opening_index, slot, color = number//6, (number%6)//2, number%2
        opening = bank["match_openings_100"][opening_index]
        seed = SEEDS[(opening_index+slot)%3]
        candidate_white = ((opening_index+slot)%2 == 0) if color == 0 else ((opening_index+slot)%2 != 0)
        require(game["seed"] == seed and game["family_id"] == opening["family_id"]
                and game["opening_uci_moves"] == opening["history"]
                and game["white"] == ("candidate" if candidate_white else "baseline"), "match schedule/colors differ")
        for role, arm in (("baseline", "A"), ("candidate", "B")):
            require(game["models"][role] == model_map[arm, seed], "game used a different model/seed")
            options = game["requested_options"][role]
            require(options["Hash"] == 16 and options["Move Overhead"] == 20
                    and options["UseNeuralEvaluation"] is True and options["UseIncrementalNeuralEvaluation"] is True,
                    "game neural resource options changed")
        replay = audit_game(game)
        value = replay["points"]
        points.append(value)
        per_seed[seed].append(value)
        per_family[game["family_id"]].append(value)
        plies += replay["plies"]
    summary = document["summary"]
    for key, expected in {"B_wins": points.count(1), "B_draws": points.count(.5), "B_losses": points.count(0),
                          "B_points": sum(points), "B_score": sum(points)/96, "completed_games": 96}.items():
        require(summary[key] == expected, "recorded match score differs: " + key)
    for seed, values in per_seed.items():
        require(summary["by_seed"][str(seed)]["B_points"] == sum(values)
                and summary["by_seed"][str(seed)]["completed_games"] == len(values) == 32, "per-seed score differs")
    scores = [sum(per_family[item["family_id"]])/6 for item in bank["match_openings_100"]]
    generator = random.Random(20261030)
    samples = sorted(sum(generator.choices(scores, k=16))/16 for _ in range(10000))
    interval = [samples[int(.025*9999)], samples[int(.975*9999)]]
    require(summary["opening_family_bootstrap95"]["interval"] == interval, "match family bootstrap differs")
    screening = sum(points) >= 53 and sum(sum(values)/32 > .5 for values in per_seed.values()) >= 2
    require(summary["screening_passed"] is screening, "expansion screening condition differs")
    pgn_games = audit_pgn(directory / "match.pgn", document)
    return {"completed_games": 96, "B_wins": points.count(1), "B_draws": points.count(.5), "B_losses": points.count(0),
            "B_points": sum(points), "B_score": sum(points)/96, "family_bootstrap95": interval,
            "screening_passed": screening, "actual_plies": plies, "pgn_games": pgn_games,
            "raw_uci": audit_match_uci(directory, document), "native_cpu_monitor": audit_monitor(directory, document)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--engine", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--data-only", action="store_true")
    parser.add_argument("--match-dir", type=Path)
    args = parser.parse_args()
    result = audit_experiment(args.root.resolve(), engine=args.engine.resolve() if args.engine else None,
                              data_only=args.data_only, match_dir=args.match_dir.resolve() if args.match_dir else None)
    with args.output.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(result, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))


def audit_experiment(root: Path, *, engine: Path | None = None, data_only: bool = False,
                     match_dir: Path | None = None) -> dict:
    frozen = read_json(root / "freeze.json")
    before = checked_files(frozen["files"])
    bank = read_json(root / "bank.json")
    require(sha256(root / "bank.json") == frozen["bank"]["sha256"], "frozen master bank changed")
    require(frozen["holdout_absent"] is True and frozen["seeds"] == list(SEEDS), "execution freeze protocol differs")
    result = {"schema_version": 1, "status": "PASS", "auditor_sha256": sha256(Path(__file__)),
              "audited_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "freeze_sha256": sha256(root / "freeze.json"),
              "bank": audit_bank(bank)}
    result["trainval"], parts = audit_dataset(root / "trainval", bank, set())
    if not data_only:
        result["training"], models = audit_training(root, frozen, parts)
        if (root / "heldout").is_dir():
            excluded = {row["feature_hash"] for records in parts.values() for row in records}
            result["heldout"], heldout = audit_dataset(root / "heldout", bank, excluded)
            parts.update(heldout)
            if (root / "evaluation/comparison.json").is_file():
                result["evaluation"] = audit_evaluation(root, frozen, models, heldout["test"])
        all_inputs = {row["feature_hash"] for records in parts.values() for row in records}
        match_inputs = {feature_hash(board_from(item["initial_fen"], item["history"]))
                        for block in (100, 1000) for item in bank[f"match_openings_{block}"]}
        require(not all_inputs & match_inputs, "generated data transposed into a frozen match starting input")
        result["match_start_overlap_with_generated_inputs"] = 0
        if engine:
            require(sha256(engine) == frozen["engine"]["sha256"], "runtime differs from execution freeze")
            result["runtime"] = audit_runtime(engine, models, parts)
        if match_dir:
            result["matches100ms"] = audit_matches(match_dir, bank, models)
    require(before == checked_files(frozen["files"]), "frozen inputs changed while independently auditing")
    result["frozen_input_files_unchanged"] = len(before)
    return result


if __name__ == "__main__":
    main()
