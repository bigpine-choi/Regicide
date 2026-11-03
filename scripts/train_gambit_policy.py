"""Reproducible own-search data and a small gambit proposal-ranking experiment.

Catalog moves seed positions and proposals, never provide target scores.  Each
target comes from a fresh fixed-node Regicide search after the candidate move,
with the GUI's classical activity profile.  The model never selects a move or
changes an evaluation; runtime native verification remains mandatory.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import copy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import random
import sys
import time

import chess

import dynamic_play
import gambit_learning as learning
import gambit_knowledge as knowledge
from neural_search_data.engine import EngineTeacher
from runtime_engine import resolve_runtime_engine

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "regicide.gambit-ranking-dataset.v1"
TRAIN_FAMILIES = ("kings", "danish", "albin", "scotch", "vienna", "englund", "blackmar-diemer", "from", "benko")
VALIDATION_FAMILIES = ("smith-morra", "budapest")
HOLDOUT_FAMILIES = ("evans", "stafford")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                    encoding="utf-8", newline="\n")


def source_hashes():
    names = ("scripts/gambit_learning.py", "scripts/train_gambit_policy.py", "scripts/dynamic_play.py",
             "scripts/neural_search_data/engine.py", "scripts/gambit_knowledge.py")
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in names}


class RuntimeTeacher(EngineTeacher):
    """Reuse proven raw logging/deadline/cleanup, restore GUI runtime defaults."""
    def __enter__(self):
        super().__enter__()
        # The historical data teacher deliberately disables newer features.
        # Restore every setting it touched to the active native default first.
        runtime = {name: self._engine.options[name].default for name in self.options}
        if "UsePieceActivity" not in self._engine.options:
            self.close()
            raise ValueError("Active runtime must advertise UsePieceActivity")
        runtime["UsePieceActivity"] = True
        self._engine.configure(runtime)
        self.options = runtime
        self.runtime_defaults = {name: option.default for name, option in self._engine.options.items()}
        return self


def choose_roots(*, seed=20261104, roots_per_family=12):
    """Predeclare whole-family splits and exclude transposed input overlaps."""
    splits = {**dict.fromkeys(TRAIN_FAMILIES, "train"),
              **dict.fromkeys(VALIDATION_FAMILIES, "validation"),
              **dict.fromkeys(HOLDOUT_FAMILIES, "holdout")}
    available = defaultdict(dict)
    for line in knowledge.repertoire():
        family = line["family_id"]
        if family not in splits:
            continue
        board = chess.Board()
        for ply, uci in enumerate(line["moves"]):
            move = chess.Move.from_uci(uci)
            if move not in board.legal_moves:
                raise ValueError("Invalid knowledge line: " + line["line_id"])
            if (ply >= max(6, line["anchor_ply"] + 1) and ply <= 22
                    and (board.turn == chess.WHITE) == (line["owner"] == "white")
                    and not board.is_game_over(claim_draw=True) and board.legal_moves.count() > 1):
                key = learning.position_key(board)
                entry = available[family].setdefault(key, {
                    "family_id": family, "split": splits[family], "root_fen": chess.STARTING_FEN,
                    "history_uci": [m.uci() for m in board.move_stack],
                    "fen": board.fen(en_passant="fen"), "position_key": key,
                    "turn": "white" if board.turn else "black", "graph_candidates": [],
                    "stages": [], "source_lines": [], "source_urls": []})
                for field, value in (("graph_candidates", uci), ("stages", line["stage"]),
                                     ("source_lines", line["line_id"]), ("source_urls", line["source_url"])):
                    if value not in entry[field]:
                        entry[field].append(value)
            board.push(move)
    memberships = defaultdict(set)
    for family, entries in available.items():
        for key in entries:
            memberships[key].add(splits[family])
    overlaps = {key for key, groups in memberships.items() if len(groups) > 1}
    excluded, result = [], []
    for family in sorted(splits):
        entries = []
        for key, entry in available[family].items():
            if key in overlaps:
                excluded.append({"family_id": family, "position_key": key, "reason": "cross_split_transposition"})
                continue
            board = chess.Board()
            for uci in entry["history_uci"]:
                board.push_uci(uci)
            intersects = False
            for move in list(board.legal_moves):
                board.push(move)
                other_splits = memberships.get(learning.position_key(board), set())
                board.pop()
                if other_splits - {entry["split"]}:
                    intersects = True
                    break
            if intersects:
                excluded.append({"family_id": family, "position_key": key, "reason": "candidate_child_cross_split"})
                continue
            entries.append(entry)
        # Keep the owner's first response after the opponent answers the offer
        # (anchor+1), including recaptures.  Prefer actual book choice points;
        # there is nothing for a ranking model to order at a single-move leaf.
        # This uses only graph coverage, never search scores or holdout fit.
        def order(entry):
            stage = min((0 if s == "accepted" else 1 if s == "declined" else 2 if s == "defense" else 3)
                        for s in entry["stages"])
            return stage, digest([seed, family, entry["position_key"]])
        by_stage = defaultdict(list)
        for entry in sorted(entries, key=order):
            by_stage[min(entry["stages"])].append(entry)
        picked = sorted((entry for entry in entries if len(entry["graph_candidates"]) >= 2),
                        key=lambda entry: digest([seed, family, entry["position_key"]]))[:roots_per_family]
        selected_keys = {entry["position_key"] for entry in picked}
        for stage in by_stage:
            by_stage[stage] = [entry for entry in by_stage[stage] if entry["position_key"] not in selected_keys]
        stage_names = sorted(by_stage)
        while len(picked) < roots_per_family and any(by_stage.values()):
            for stage in stage_names:
                if by_stage[stage] and len(picked) < roots_per_family:
                    picked.append(by_stage[stage].pop(0))
        for entry in picked:
            entry["query_id"] = entry["family_id"] + "-" + digest(entry["position_key"])[:14]
            result.append(entry)
    keys_by_split = {name: {row["position_key"] for row in result if row["split"] == name}
                     for name in ("train", "validation", "holdout")}
    if any(keys_by_split[a] & keys_by_split[b] for a, b in
           (("train", "validation"), ("train", "holdout"), ("validation", "holdout"))):
        raise ValueError("Split transposition leakage")
    return result, {"families": splits, "root_counts": dict(Counter(row["family_id"] for row in result)),
                    "split_counts": dict(Counter(row["split"] for row in result)),
                    "multi_graph_counts": dict(Counter(row["split"] for row in result if len(row["graph_candidates"]) >= 2)),
                    "cross_split_root_intersections": 0, "excluded": excluded,
                    "assignment_sha256": digest([(row["query_id"], row["split"], row["position_key"]) for row in result])}


def candidate_moves(board, entry, root_result, *, seed, max_candidates=6):
    legal = list(board.legal_moves)
    graph = sorted(set(entry["graph_candidates"]), key=lambda uci: (digest([seed, entry["query_id"], uci]), uci))
    candidates = [chess.Move.from_uci(uci) for uci in graph]
    best = root_result.get("bestmove_uci")
    if best:
        move = chess.Move.from_uci(best)
        if move in legal and move not in candidates:
            candidates.append(move)
    geometric = sorted(legal, key=lambda move: (-dynamic_play._proposal_points(board, move.uci()),
                                               digest([seed, entry["query_id"], move.uci()]), move.uci()))
    forcing = sorted((move for move in legal if board.is_capture(move) or board.gives_check(move)),
                     key=lambda move: digest([seed, entry["query_id"], "forcing", move.uci()]))
    for group in (forcing[:2], geometric, sorted(legal, key=lambda move: digest([seed, entry["query_id"], move.uci()]))):
        for move in group:
            if move not in candidates:
                candidates.append(move)
            if len(candidates) >= max_candidates:
                return candidates[:max_candidates]
    return candidates[:max_candidates]


def generate(binary, output, *, seed=20261104, nodes=6000, roots_per_family=12, max_candidates=6, wall_seconds=600):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    roots, split = choose_roots(seed=seed, roots_per_family=roots_per_family)
    manifest = {"schema": SCHEMA, "created_utc": datetime.now(timezone.utc).isoformat(),
                "seed": seed, "nodes": nodes, "roots_per_family": roots_per_family,
                "max_candidates": max_candidates, "source_sha256": source_hashes(),
                "binary_sha256": hashlib.sha256(Path(binary).read_bytes()).hexdigest(),
                "binary": str(Path(binary).resolve()), "splits": split,
                "target": "negative child side-to-move exact CP from fresh own fixed-node search; mate/bound/missing excluded",
                "external_data_role": "CC0 catalog supplies positions/proposals only, not target evaluations",
                "feature_names": list(learning.FEATURE_NAMES), "feature_schema": learning.FEATURE_SCHEMA}
    write(output / "predeclared.json", {**manifest, "roots": roots})
    deadline = time.monotonic() + wall_seconds
    samples, excluded, records = [], [], []
    teacher = RuntimeTeacher(Path(binary), output / "native-uci.jsonl", nodes, deadline, search_timeout=8)
    try:
        with teacher:
            for index, entry in enumerate(roots):
                board = chess.Board()
                for uci in entry["history_uci"]:
                    board.push_uci(uci)
                root_result = teacher.query(board, entry["query_id"] + ":root")
                records.append({"root": entry["query_id"], "role": "root_candidate_generation", **root_result})
                moves = candidate_moves(board, entry, root_result, seed=seed, max_candidates=max_candidates)
                rows = []
                for order, move in enumerate(moves):
                    vector = learning.features(board, move)
                    child = board.copy(stack=True)
                    child.push(move)
                    query = teacher.query(child, entry["query_id"] + ":" + move.uci())
                    records.append({"root": entry["query_id"], "role": "candidate_label", "candidate_uci": move.uci(),
                                    "fen": child.fen(en_passant="fen"), **query})
                    score = query.get("score", {})
                    if (query.get("completed") is not True or query.get("lowerbound") or query.get("upperbound")
                            or score.get("kind") != "cp" or type(score.get("stm")) is not int
                            or type(query.get("depth")) is not int or query["depth"] < 3):
                        excluded.append({"root": entry["query_id"], "uci": move.uci(), "reason": "no_exact_completed_cp",
                                         "score": score, "depth": query.get("depth")})
                        continue
                    rows.append({"uci": move.uci(), "features": vector, "owner_cp": -score["stm"],
                                 "native_depth": query["depth"], "native_nodes": query.get("nodes"),
                                 "query_id": query["query_id"], "original_order": order,
                                 "graph_candidate": move.uci() in entry["graph_candidates"],
                                 "capture": board.is_capture(move), "check": board.gives_check(move),
                                 "heuristic_points": dynamic_play._proposal_points(board, move.uci())})
                if len(rows) >= 2:
                    samples.append({**entry, "candidates": rows})
                else:
                    excluded.append({"root": entry["query_id"], "reason": "fewer_than_two_labels"})
                if (index + 1) % 12 == 0:
                    print(json.dumps({"progress_roots": index + 1, "total": len(roots), "labeled_groups": len(samples)}), flush=True)
    finally:
        manifest.update(engine_name=teacher.identity, configured_options=teacher.options,
                        runtime_defaults=getattr(teacher, "runtime_defaults", {}), cleanup=teacher.cleanup_status,
                        all_closed=teacher.closed)
        write(output / "queries.json", records)
        write(output / "excluded.json", excluded)
    dataset = {**manifest, "groups": samples}
    dataset["semantic_sha256"] = digest({"seed": seed, "nodes": nodes, "groups": samples,
                                         "binary_sha256": manifest["binary_sha256"], "options": manifest["configured_options"]})
    write(output / "dataset.json", dataset)
    return dataset


def normalization(groups):
    vectors = [row["features"] for group in groups for row in group["candidates"]]
    means = [sum(vector[i] for vector in vectors) / len(vectors) for i in range(len(learning.FEATURE_NAMES))]
    scales = [max(.25, math.sqrt(sum((vector[i] - means[i]) ** 2 for vector in vectors) / len(vectors)))
              for i in range(len(means))]
    return means, scales


def fit(groups, *, seed, regularization, epochs=120):
    means, scales = normalization(groups)
    count = len(means)
    pairs = []
    for group in groups:
        rows = group["candidates"]
        for a in range(len(rows)):
            for b in range(a + 1, len(rows)):
                gap = rows[a]["owner_cp"] - rows[b]["owner_cp"]
                if abs(gap) < 15:
                    continue
                better, worse = (rows[a], rows[b]) if gap > 0 else (rows[b], rows[a])
                vector = [(x - y) / scale for x, y, scale in zip(better["features"], worse["features"], scales)]
                pairs.append((vector, min(2., max(.25, abs(gap) / 100.))))
    if not pairs:
        raise ValueError("No meaningful own-search ranking pairs")
    rng = random.Random(seed)
    weights = [0.] * count
    for epoch in range(epochs):
        rng.shuffle(pairs)
        rate = .025 / math.sqrt(1 + epoch / 12.)
        for vector, importance in pairs:
            margin = max(-40., min(40., sum(a * b for a, b in zip(weights, vector))))
            gradient = importance / (1 + math.exp(margin))
            for index, value in enumerate(vector):
                weights[index] += rate * (gradient * value - regularization * weights[index])
    return {"weights": [round(value, 10) for value in weights],
            "means": [round(value, 10) for value in means], "scales": [round(value, 10) for value in scales],
            "training_pairs": len(pairs), "regularization": regularization, "epochs": epochs}


def evaluate(groups, model, *, graph_only=False):
    regrets = {key: [] for key in ("learned", "original_order", "geometric")}
    selections = []
    pair_ok, pair_count = 0, 0
    for group in groups:
        rows = [row for row in group["candidates"] if not graph_only or row["graph_candidate"]]
        if len(rows) < 2:
            continue
        scores = [learning._score(row["features"], model) for row in rows]
        best = max(row["owner_cp"] for row in rows)
        choices = {
            "learned": max(range(len(rows)), key=lambda index: (scores[index], -rows[index]["original_order"])),
            "original_order": min(range(len(rows)), key=lambda index: rows[index]["original_order"]),
            "geometric": max(range(len(rows)), key=lambda index: (rows[index]["heuristic_points"], -rows[index]["original_order"])),
        }
        for key, index in choices.items():
            regrets[key].append(best - rows[index]["owner_cp"])
        for a in range(len(rows)):
            for b in range(a + 1, len(rows)):
                diff = rows[a]["owner_cp"] - rows[b]["owner_cp"]
                if abs(diff) >= 15:
                    pair_count += 1
                    pair_ok += int((scores[a] - scores[b]) * diff > 0)
        selections.append({"root": group["query_id"], "family_id": group["family_id"],
                           "selected": {key: rows[index]["uci"] for key, index in choices.items()},
                           "regret_cp": {key: regrets[key][-1] for key in regrets}})
    summary = {key: {"mean_regret_cp": round(sum(values) / len(values), 3) if values else None,
                     "within_35cp": sum(value <= 35 for value in values),
                     "regret_150cp_or_more": sum(value >= 150 for value in values),
                     "worst_regret_cp": max(values, default=None)} for key, values in regrets.items()}
    return {"groups": len(selections), "families": sorted({row["family_id"] for row in selections}),
            "pairwise_accuracy": round(pair_ok / pair_count, 5) if pair_count else None,
            "pair_count": pair_count, "methods": summary, "selections": selections}


def train(dataset, output, *, model_path=None):
    groups = dataset["groups"]
    partitions = {split: [group for group in groups if group["split"] == split]
                  for split in ("train", "validation", "holdout")}
    if min(len(rows) for rows in partitions.values()) < 4:
        raise ValueError("Training requires four or more groups in every predeclared split")
    candidates = []
    for regularization in (.001, .01, .05, .2):
        model = fit(partitions["train"], seed=dataset["seed"], regularization=regularization)
        value = evaluate(partitions["validation"], model)
        candidates.append((value["methods"]["learned"]["mean_regret_cp"], -value["pairwise_accuracy"], model, value))
    _loss, _accuracy, learned, validation = min(candidates, key=lambda row: (row[0], row[1], row[2]["regularization"]))
    # Holdout is accessed exactly once after the hyperparameter choice above.
    heldout = evaluate(partitions["holdout"], learned)
    graph_validation = evaluate(partitions["validation"], learned, graph_only=True)
    graph_holdout = evaluate(partitions["holdout"], learned, graph_only=True)
    gates = {}
    for name, metric in (("validation", validation), ("holdout", heldout)):
        selected = metric["methods"]["learned"]
        gates[name + "_sample_size"] = metric["groups"] >= 8 and len(metric["families"]) >= 2
        for baseline in ("original_order", "geometric"):
            comparison = metric["methods"][baseline]
            gates[name + "_regret_vs_" + baseline] = selected["mean_regret_cp"] <= comparison["mean_regret_cp"]
            gates[name + "_large_loss_vs_" + baseline] = selected["regret_150cp_or_more"] <= comparison["regret_150cp_or_more"]
    # Actual book candidate subsets must also avoid regression where examples
    # exist.  Sparse coverage is reported and never replaced with all-legal data.
    for name, metric in (("graph_validation", graph_validation), ("graph_holdout", graph_holdout)):
        gates[name + "_sample_size"] = metric["groups"] >= 3
        if metric["groups"]:
            selected = metric["methods"]["learned"]
            for baseline in ("original_order", "geometric"):
                comparison = metric["methods"][baseline]
                gates[name + "_regret_vs_" + baseline] = selected["mean_regret_cp"] <= comparison["mean_regret_cp"]
                gates[name + "_large_loss_vs_" + baseline] = selected["regret_150cp_or_more"] <= comparison["regret_150cp_or_more"]
    accepted = all(gates.values())
    model = {"schema": learning.SCHEMA, "policy": learning.POLICY,
             "feature_schema": learning.FEATURE_SCHEMA, "feature_names": list(learning.FEATURE_NAMES),
             "feature_sources": learning.feature_sources(),
             "engine_identity": {"binary_sha256": dataset["binary_sha256"], "evaluation_id": "classical_activity_v1"},
             **{key: learned[key] for key in ("weights", "means", "scales")},
             "status": "promoted" if accepted else "experimental",
             "acceptance": {"passed": accepted, "gates": gates,
                            "meaning": "small heldout own-search ranking check; not Elo or objective tactical safety"},
             "training": {"method": "pairwise_logistic_sgd", "seed": dataset["seed"],
                          "regularization": learned["regularization"], "epochs": learned["epochs"],
                          "training_pairs": learned["training_pairs"], "dataset_sha256": dataset["semantic_sha256"],
                          "target": dataset["target"], "splits": dataset["splits"],
                          "candidate_generation": "catalog + own root best move + forcing + geometric proposals"},
             "created_utc": datetime.now(timezone.utc).isoformat()}
    model["model_id"] = learning.model_digest(model)
    report = {"schema": "regicide.gambit-ranking-training.v1", "accepted": accepted,
              "model_id": model["model_id"], "gates": gates,
              "training": evaluate(partitions["train"], learned), "validation": validation,
              "holdout": heldout, "graph_validation": graph_validation, "graph_holdout": graph_holdout,
              "validation_choices": [{"regularization": row[2]["regularization"], "mean_regret_cp": row[0],
                                      "pairwise_accuracy": -row[1]} for row in candidates],
              "candidate_kinds": dict(Counter("capture" if row["capture"] else "check" if row["check"] else "quiet"
                                              for group in groups for row in group["candidates"])),
              "source_sha256": source_hashes()}
    write(Path(output) / "training-result.json", report)
    write(Path(output) / "policy.json", model)
    if model_path is not None:
        # Experimental artifacts remain explicit.  Runtime refuses to activate
        # them even if copied into the common path for inspectability.
        write(model_path, model)
    return model, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--dataset", type=Path, help="Reuse existing source-labeled data without new searches")
    parser.add_argument("--model", type=Path, default=learning.DEFAULT_MODEL)
    parser.add_argument("--seed", type=int, default=20261104)
    parser.add_argument("--nodes", type=int, default=6000)
    parser.add_argument("--roots-per-family", type=int, default=12)
    parser.add_argument("--candidates", type=int, default=6)
    parser.add_argument("--wall-seconds", type=int, default=600)
    args = parser.parse_args()
    if args.dataset:
        if args.output.exists():
            raise ValueError("Use a new output directory for every training run")
        args.output.mkdir(parents=True)
        dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    else:
        dataset = generate(args.binary or resolve_runtime_engine(), args.output, seed=args.seed,
                           nodes=args.nodes, roots_per_family=args.roots_per_family,
                           max_candidates=args.candidates, wall_seconds=args.wall_seconds)
    model, report = train(dataset, args.output, model_path=args.model)
    print(json.dumps({"model_id": model["model_id"], "status": model["status"], "gates": report["gates"],
                      "groups": dict(Counter(row["split"] for row in dataset["groups"]))}, sort_keys=True))


if __name__ == "__main__":
    main()
