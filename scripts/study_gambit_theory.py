"""Study opening branches with stable, deeper Regicide search labels.

The catalog supplies exercises and candidate moves, never answer evaluations.
This offline experiment learns candidate order only. It does not activate a
runtime policy, establish objective opening theory, or measure playing strength.
All score labels use the side to move at the exercise root, including defenses.
"""
from __future__ import annotations

import argparse
from collections import Counter
import copy
import hashlib
import json
import math
from pathlib import Path

import chess

import gambit_learning as learning
from train_gambit_policy import fit
from gambit_research_core import digest, replay, write_json

SCHEMA = "regicide.gambit-theory-study.v1"
MODEL_SCHEMA = "regicide.gambit-theory-policy.v1"
REPORT_SCHEMA = "regicide.gambit-theory-study-report.v1"
SPLITS = ("train", "validation", "holdout")
ROOT = Path(__file__).resolve().parents[1]


def position_key(board):
    return " ".join(board.fen(en_passant="legal").split()[:4])


def source_hashes():
    names = ("scripts/study_gambit_theory.py", "scripts/gambit_learning.py",
             "scripts/train_gambit_policy.py", "scripts/dynamic_play.py")
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in names}


def _integer(value, name, minimum=1):
    if type(value) is not int or value < minimum:
        raise ValueError(name + " must be an integer >= " + str(minimum))
    return value


def _cases(curriculum):
    cases = curriculum.get("cases") if isinstance(curriculum, dict) else None
    if not isinstance(cases, list) or not cases:
        raise ValueError("Curriculum must contain cases")
    identities, families, footprints = set(), {}, {name: set() for name in SPLITS}
    validated = []
    for case in cases:
        if not isinstance(case, dict) or case.get("split") not in SPLITS:
            raise ValueError("Every case needs a predeclared split")
        if not isinstance(case.get("case_id"), str) or case["case_id"] in identities:
            raise ValueError("Case IDs must be unique strings")
        identities.add(case["case_id"])
        family = case.get("family_id")
        if not isinstance(family, str) or (family in families and families[family] != case["split"]):
            raise ValueError("Theory family must belong to exactly one split")
        families[family] = case["split"]
        board = replay(case.get("history_uci"))
        if (case.get("fen") not in (board.fen(en_passant="fen"), board.fen(en_passant="legal"))
                or position_key(board) != case.get("position_key")
                or ("white" if board.turn else "black") != case.get("turn")):
            raise ValueError("Curriculum board/history mismatch")
        candidates = case.get("book_candidates")
        if not isinstance(candidates, list) or any(
                not isinstance(uci, str) or chess.Move.from_uci(uci) not in board.legal_moves
                for uci in candidates):
            raise ValueError("Book candidates must be legal moves")
        footprint = {position_key(board)}
        for move in list(board.legal_moves):
            child = board.copy(stack=True)
            child.push(move)
            footprint.add(position_key(child))
        footprints[case["split"]].update(footprint)
        validated.append((copy.deepcopy(case), board))
    for index, a in enumerate(SPLITS):
        for b in SPLITS[index + 1:]:
            if footprints[a] & footprints[b]:
                raise ValueError("Cross-split root or immediate-child transposition leakage")
    return validated


def _usable(query, minimum_depth):
    return (isinstance(query, dict) and query.get("status") == "complete"
            and type(query.get("cp")) is int and query.get("mate") is None
            and type(query.get("depth")) is int and query["depth"] >= minimum_depth
            and not query.get("lowerbound") and not query.get("upperbound"))


def _candidate_moves(board, case, baseline, seed, limit):
    native = baseline.get("bestmove_uci")
    if not isinstance(native, str) or chess.Move.from_uci(native) not in board.legal_moves:
        return []
    # Reserve a slot for the native baseline even at a large book branch point.
    book = list(dict.fromkeys(case["book_candidates"]))
    chosen = book[:limit if native in book[:limit] else max(0, limit - 1)]
    if native not in chosen:
        chosen.append(native)
    for move in sorted(board.legal_moves, key=lambda move: (
            digest([seed, case["case_id"], move.uci()]), move.uci())):
        if len(chosen) >= limit:
            break
        if move.uci() not in chosen:
            chosen.append(move.uci())
    return chosen


def study(curriculum, analyzer, *, seed=20261105, candidate_limit=4,
          deep_multiplier=4, stability_cp=80):
    """Create independent two-budget labels; exclude rather than invent labels.

    A stable label is only an agreement between two bounded native searches.
    It is not a claim that either search has found the objective best defense.
    """
    _integer(seed, "seed", 0)
    _integer(candidate_limit, "candidate_limit", 2)
    _integer(deep_multiplier, "deep_multiplier", 2)
    _integer(stability_cp, "stability_cp", 0)
    cases = _cases(curriculum)
    nodes = _integer(getattr(analyzer, "nodes", 25000), "analyzer.nodes")
    minimum_depth = max(4, _integer(getattr(analyzer, "minimum_depth", 4), "minimum_depth"))
    groups, excluded = [], []
    for case, board in cases:
        if board.is_game_over(claim_draw=True):
            excluded.append({"case_id": case["case_id"], "reason": "terminal_or_claimable_root"})
            continue
        root_query = analyzer.analyze(board, nodes=nodes)
        if not _usable(root_query, minimum_depth):
            excluded.append({"case_id": case["case_id"], "reason": "unreliable_native_root_baseline",
                             "query_id": root_query.get("query_id")})
            continue
        moves = _candidate_moves(board, case, root_query, seed, candidate_limit)
        if not moves:
            excluded.append({"case_id": case["case_id"], "reason": "no_legal_native_baseline",
                             "query_id": root_query.get("query_id")})
            continue
        rows = []
        for order, uci in enumerate(moves):
            move = chess.Move.from_uci(uci)
            child = board.copy(stack=True)
            child.push(move)
            shallow = analyzer.analyze(child, nodes=nodes)
            deep = analyzer.analyze(child, nodes=nodes * deep_multiplier)
            evidence = {"case_id": case["case_id"], "uci": uci,
                        "shallow_query_id": shallow.get("query_id"),
                        "deep_query_id": deep.get("query_id"),
                        "shallow": copy.deepcopy(shallow), "deep": copy.deepcopy(deep)}
            if not (_usable(shallow, minimum_depth) and _usable(deep, minimum_depth)):
                excluded.append({**evidence, "reason": "non_cp_bound_incomplete_or_shallow_label"})
                continue
            if abs(deep["cp"] - shallow["cp"]) > stability_cp:
                excluded.append({**evidence, "reason": "unstable_native_label"})
                continue
            if deep["depth"] < shallow["depth"]:
                excluded.append({**evidence, "reason": "deeper_budget_completed_lower_depth"})
                continue
            rows.append({"uci": uci, "features": learning.features(board, move),
                         "owner_cp": -deep["cp"], "mover_cp": -deep["cp"],
                         "original_order": order, "graph_candidate": uci in case["book_candidates"],
                         "native_baseline": uci == root_query.get("bestmove_uci"),
                         "capture": board.is_capture(move), "check": board.gives_check(move),
                         "heuristic_points": 0, "native_depth": deep["depth"],
                         "native_nodes": deep.get("nodes"),
                         "stability_delta_cp": abs(deep["cp"] - shallow["cp"]),
                         "query_ids": [shallow.get("query_id"), deep.get("query_id")],
                         "evidence": evidence})
        if len(rows) < 2 or not any(row["native_baseline"] for row in rows):
            excluded.append({"case_id": case["case_id"],
                             "reason": "fewer_than_two_stable_labels_or_unstable_baseline",
                             "stable_candidates": [row["uci"] for row in rows]})
            continue
        groups.append({**case, "query_id": case["case_id"], "candidates": rows,
                       "native_baseline_uci": root_query["bestmove_uci"],
                       "root_query_id": root_query.get("query_id"),
                       "target_pov": "root_side_to_move"})
    dataset = {"schema": SCHEMA, "seed": seed, "groups": groups, "excluded": excluded,
               "curriculum_sha256": digest(curriculum), "curriculum_source": curriculum.get("source"),
               "split_counts": dict(Counter(group["split"] for group in groups)),
               "case_count": len(cases), "budgets": {"base_nodes": nodes,
                    "deep_nodes": nodes * deep_multiplier, "minimum_depth": minimum_depth,
                    "maximum_stability_delta_cp": stability_cp},
               "target": "negative child side-to-move CP from stable deeper native search; not objective truth",
               "target_pov": "root_side_to_move; field owner_cp is legacy pairwise-fit naming, not gambit owner",
               "engine_identity": copy.deepcopy(getattr(analyzer, "identity", {})),
               "configured_options": copy.deepcopy(getattr(analyzer, "options", {})),
               "source_sha256": source_hashes(), "feature_names": list(learning.FEATURE_NAMES),
               "feature_schema": learning.FEATURE_SCHEMA}
    dataset["semantic_sha256"] = digest(dataset)
    return dataset


def _metrics(groups, model):
    regrets = {name: [] for name in ("learned", "original_order", "native_baseline")}
    selections = []
    for group in groups:
        rows = group["candidates"]
        best = max(row["owner_cp"] for row in rows)
        choices = {
            "learned": max(rows, key=lambda row: (learning._score(row["features"], model), -row["original_order"])),
            "original_order": min(rows, key=lambda row: row["original_order"]),
            "native_baseline": next(row for row in rows if row["native_baseline"]),
        }
        for name, row in choices.items():
            regrets[name].append(best - row["owner_cp"])
        selections.append({"case_id": group["case_id"], "family_id": group["family_id"],
                           "selected": {name: row["uci"] for name, row in choices.items()},
                           "regret_cp": {name: values[-1] for name, values in regrets.items()}})
    methods = {name: {"mean_regret_cp": round(sum(values) / len(values), 5) if values else None,
                      "large_loss_150cp": sum(value >= 150 for value in values),
                      "worst_regret_cp": max(values, default=None)} for name, values in regrets.items()}
    return {"groups": len(groups), "families": sorted({group["family_id"] for group in groups}),
            "methods": methods, "selections": selections}


def _validate_dataset(dataset):
    if not isinstance(dataset, dict) or dataset.get("schema") != SCHEMA:
        raise ValueError("Invalid theory dataset schema")
    expected = digest({key: value for key, value in dataset.items() if key != "semantic_sha256"})
    if dataset.get("semantic_sha256") != expected:
        raise ValueError("Theory dataset checksum mismatch")
    groups = dataset.get("groups")
    if not isinstance(groups, list):
        raise ValueError("Invalid theory groups")
    seen, families, keys = set(), {}, {}
    for group in groups:
        split = group.get("split")
        if split not in SPLITS or group.get("case_id") in seen:
            raise ValueError("Invalid or duplicate dataset group")
        seen.add(group["case_id"])
        family = group.get("family_id")
        if family in families and families[family] != split:
            raise ValueError("Family split leakage")
        families[family] = split
        key = group.get("position_key")
        if key in keys and keys[key] != split:
            raise ValueError("Position split leakage")
        keys[key] = split
        rows = group.get("candidates")
        if not isinstance(rows, list) or len(rows) < 2 or sum(row.get("native_baseline") is True for row in rows) != 1:
            raise ValueError("Each group needs alternatives and one native baseline")
        for row in rows:
            if (type(row.get("owner_cp")) is not int or len(row.get("features", [])) != len(learning.FEATURE_NAMES)
                    or any(type(x) not in (int, float) or not math.isfinite(x) for x in row["features"])):
                raise ValueError("Invalid training label or feature vector")
    return groups


def train_study(dataset):
    """Fit train only, select on validation, then report the untouched holdout.

    Even a passing ranking experiment remains experimental: activation requires
    independent runtime verification and equal-time match evidence elsewhere.
    """
    groups = _validate_dataset(dataset)
    partitions = {split: [group for group in groups if group["split"] == split] for split in SPLITS}
    counts = {split: len(rows) for split, rows in partitions.items()}
    model = {"schema": MODEL_SCHEMA, "policy": "regicide-gambit-theory-order-v1",
             "status": "experimental", "active": False, "trained": False,
             "engine_identity": copy.deepcopy(dataset.get("engine_identity", {})),
             "feature_schema": learning.FEATURE_SCHEMA, "feature_names": list(learning.FEATURE_NAMES),
             "dataset_sha256": dataset["semantic_sha256"], "seed": dataset["seed"],
             "meaning": "candidate ordering only; no runtime activation or strength claim"}
    report = {"schema": REPORT_SCHEMA, "trained": False, "candidate_acceptance": False,
              "active": False, "split_counts": counts, "gates": {},
              "dataset_sha256": dataset["semantic_sha256"],
              "meaning": "regret within collected stable own-search candidates, not objective opening quality or Elo"}
    if min(counts.values(), default=0) < 4:
        report["reason"] = "requires_four_stable_groups_in_each_predeclared_split"
        model["model_id"] = digest(model)
        return {"model": model, "report": report}
    choices = []
    try:
        for regularization in (.001, .01, .05, .2):
            fitted = fit(partitions["train"], seed=dataset["seed"], regularization=regularization)
            metric = _metrics(partitions["validation"], fitted)
            score = metric["methods"]["learned"]
            choices.append((score["mean_regret_cp"], score["large_loss_150cp"], regularization, fitted, metric))
    except ValueError as error:
        report.update(reason="training_pairs_insufficient", detail=str(error))
        model["model_id"] = digest(model)
        return {"model": model, "report": report}
    selected = min(choices, key=lambda row: row[:3])
    fitted, validation = selected[3], selected[4]
    # Do not inspect holdout scores before choosing the hyperparameter.
    holdout = _metrics(partitions["holdout"], fitted)
    gates = {}
    for name, metric in (("validation", validation), ("holdout", holdout)):
        gates[name + "_coverage"] = metric["groups"] >= 8 and len(metric["families"]) >= 2
        for baseline in ("original_order", "native_baseline"):
            learned, comparison = metric["methods"]["learned"], metric["methods"][baseline]
            gates[name + "_regret_vs_" + baseline] = learned["mean_regret_cp"] <= comparison["mean_regret_cp"]
            gates[name + "_large_loss_vs_" + baseline] = learned["large_loss_150cp"] <= comparison["large_loss_150cp"]
    model.update(trained=True, weights=fitted["weights"], means=fitted["means"], scales=fitted["scales"],
                 training={"method": "pairwise_logistic_sgd", "fit_split": "train", "selection_split": "validation",
                           "regularization": fitted["regularization"], "epochs": fitted["epochs"],
                           "training_pairs": fitted["training_pairs"]})
    model["model_id"] = digest(model)
    report.update(trained=True, candidate_acceptance=all(gates.values()), gates=gates,
                  reason="experimental_pending_independent_runtime_and_match_validation",
                  validation=validation, holdout=holdout, training=_metrics(partitions["train"], fitted),
                  validation_choices=[{"regularization": row[2], "mean_regret_cp": row[0],
                                       "large_loss_150cp": row[1]} for row in choices])
    return {"model": model, "report": report}


def write_artifacts(output, dataset, training):
    output = Path(output)
    write_json(output / "dataset.json", dataset)
    write_json(output / "model.json", training["model"])
    write_json(output / "study_report.json", training["report"])


def main(argv=None):
    from gambit_research_core import NativeAnalyzer
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--curriculum", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--nodes", type=int, default=25000)
    parser.add_argument("--wall-seconds", type=float, default=300)
    parser.add_argument("--candidates", type=int, default=4)
    parser.add_argument("--seed", type=int, default=20261105)
    args = parser.parse_args(argv)
    args.output.mkdir(parents=True, exist_ok=False)
    curriculum = json.loads(args.curriculum.read_text(encoding="utf-8"))
    with NativeAnalyzer(args.binary, args.output / "analysis", nodes=args.nodes, wall_seconds=args.wall_seconds) as analyzer:
        dataset = study(curriculum, analyzer, seed=args.seed, candidate_limit=args.candidates)
        training = train_study(dataset)
        write_artifacts(args.output, dataset, training)
    print(json.dumps({"groups": len(dataset["groups"]), "trained": training["report"]["trained"],
                      "candidate_acceptance": training["report"]["candidate_acceptance"], "active": False}))


if __name__ == "__main__":
    main()
