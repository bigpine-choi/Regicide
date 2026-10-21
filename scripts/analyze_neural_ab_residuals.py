"""Post-hoc offline diagnosis of the preserved neural target A/B experiment.

Reads immutable attempt outputs, never invokes an engine or changes a model.
The former holdout is development evidence after this analysis, not a fresh test.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

for _name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ[_name] = "1"

import chess
import numpy as np

import neural_precision as precision
import neural_training as base


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def rows(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line]


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    with Path(path).open("x", encoding="utf-8", newline="\n") as f:
        json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)
        f.write("\n")


def dist(values):
    a = np.asarray(values, dtype=float)
    if not len(a):
        return {"count": 0}
    return {"count": len(a), "mean": float(a.mean()), "mean_abs": float(np.abs(a).mean()),
            "std": float(a.std()), "min": float(a.min()), "max": float(a.max()),
            "p05": float(np.quantile(a, .05)), "p50": float(np.quantile(a, .5)),
            "p95": float(np.quantile(a, .95)), "p95_abs": float(np.quantile(np.abs(a), .95)),
            "abs_ge100_fraction": float(np.mean(np.abs(a) >= 100)),
            "abs_ge300_fraction": float(np.mean(np.abs(a) >= 300)),
            "abs_ge500_fraction": float(np.mean(np.abs(a) >= 500))}


def corr(a, b):
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    return float(np.corrcoef(a, b)[0, 1]) if len(a) > 1 and a.std() > 0 and b.std() > 0 else None


def metrics(pred, target, records):
    pred, target = np.asarray(pred, dtype=float), np.asarray(target, dtype=float)
    err = pred-target
    families = defaultdict(list)
    for row, e in zip(records, err, strict=True):
        families[row["family_id"]].append(float(abs(e)))
    margin = np.abs(target) >= 50
    order = np.argsort(err**2)[::-1]
    total = float(np.sum(err**2))
    stm = np.asarray([1 if chess.Board(r["fen"]).turn else -1 for r in records])
    result = {"count": len(err), "families": len(families), "mae_cp": float(np.abs(err).mean()),
              "rmse_cp": float(np.sqrt(np.mean(err**2))), "mse_cp2": float(np.mean(err**2)),
              "bias_white_cp": float(err.mean()), "bias_stm_cp": float(np.mean(err*stm)),
              "p95_abs_cp": float(np.quantile(np.abs(err), .95)),
              "absolute_error_ge100_fraction": float(np.mean(np.abs(err) >= 100)),
              "absolute_error_ge300_fraction": float(np.mean(np.abs(err) >= 300)),
              "family_macro_mae_cp": float(np.mean([np.mean(v) for v in families.values()])),
              "sign_eligible_count": int(margin.sum()),
              "opposite_sign_margin50_fraction": float(np.mean(pred[margin]*target[margin] < 0)) if margin.any() else None,
              "top_1_percent_sse_fraction": float(np.sum(err[order[:max(1, int(np.ceil(len(err)*.01)))]]**2)/total) if total else 0,
              "top_5_percent_sse_fraction": float(np.sum(err[order[:max(1, int(np.ceil(len(err)*.05)))]]**2)/total) if total else 0}
    return result


def bucket(value, edges):
    for lo, hi, name in edges:
        if lo <= value < hi:
            return name
    raise ValueError(value)


def group_keys(row):
    b = chess.Board(row["fen"])
    return ["all", "kind:"+row["kind"], "subtype:"+row["subtype"],
            "turn:"+("white" if b.turn else "black"),
            "depth:"+bucket(row["depth"], [(0, 3, "1-2"), (3, 5, "3-4"), (5, 7, "5-6"), (7, 100, "7+")]),
            "pieces:"+bucket(len(b.piece_map()), [(0, 8, "2-7"), (8, 13, "8-12"), (13, 21, "13-20"), (21, 33, "21-32")]),
            "search10k_abs:"+bucket(abs(row["target_white_cp"]), [(0, 100, "0-99"), (100, 300, "100-299"), (300, 600, "300-599"), (600, 28001, "600+")]),
            "target_delta_abs:"+bucket(abs(row["target_white_cp"]-row["classical_static_white_cp"]), [(0, 50, "0-49"), (50, 100, "50-99"), (100, 300, "100-299"), (300, 28001, "300+")])]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()
    root = args.root.resolve()
    output = args.output.resolve()
    if output.exists():
        raise FileExistsError(output)
    input_paths = [root/"trainval/train.jsonl", root/"trainval/validation.jsonl", root/"heldout/test.jsonl",
                   root/"training/models.json", root/"training/model-freeze.json", root/"freeze.json",
                   root/"evaluation/predictions.jsonl", root/"evaluation/reference_queries.jsonl",
                   root/"evaluation/comparison.json", root/"trainval/input_collisions.json", root/"heldout/input_collisions.json"]
    model_document = read(root/"training/models.json")
    for model in model_document["models"]:
        d = Path(model["path"]).parent
        input_paths.extend(d/name for name in ["model.nnue", "float_weights.npz", "history.json", "metrics.json"])
    before = {str(p): sha(p) for p in input_paths}
    parts = {"train": rows(root/"trainval/train.jsonl"), "validation": rows(root/"trainval/validation.jsonl"), "test": rows(root/"heldout/test.jsonl")}
    stored_predictions = {r["row_query_id"]: r for r in rows(root/"evaluation/predictions.jsonl")}
    comparison = read(root/"evaluation/comparison.json")
    predictions, model_details, checks = {}, {}, []
    for item in model_document["models"]:
        mid, arm = item["model_id"], item["arm"]
        d = Path(item["path"]).parent
        model = precision.load_model(d/"model.nnue")
        floats = precision.load_float_parameters(d/"float_weights.npz")
        quantized = precision.quantize(floats, 32)
        if any(not np.array_equal(model[k], quantized[k]) for k in precision.PARAMETER_KEYS):
            raise AssertionError("Float requantization mismatch")
        history, saved = read(d/"history.json"), read(d/"metrics.json")
        details = {"arm": arm, "seed": item["seed"], "curves": {}, "gradient_clipped_batches_total": sum(e["gradient_clipped_batches"] for e in history),
                   "gradient_max_norm": max(e["max_batch_gradient_global_norm"] for e in history),
                   "integer_parameters_changed": saved["changed_integer_parameters"], "partitions": {}}
        for epoch in [1, 10, 30, 60, 90, 100, 110, 120]:
            details["curves"][str(epoch)] = history[epoch-1]
        details["last_30_epoch_mse_change_fraction"] = (history[-1]["fake_quantized_train"]["mse_cp2"] / history[89]["fake_quantized_train"]["mse_cp2"] - 1)
        details["last_30_epoch_mae_change_cp"] = history[-1]["fake_quantized_train"]["mae_cp"]-history[89]["fake_quantized_train"]["mae_cp"]
        for name, records in parts.items():
            x, _ = base.pack_records(records)
            pred = precision.predict(model, x)
            fp = base.forward(floats, x)[0]
            target = np.asarray([r["classical_static_white_cp"] if arm == "A" else r["target_white_cp"] for r in records])
            result = {"integer_own_target": metrics(pred, target, records), "shadow_float_own_target": metrics(fp, target, records),
                      "integer_minus_float": dist(pred-fp),
                      "quantized_minus_float_mae_cp": float(np.mean(np.abs(pred-target))-np.mean(np.abs(fp-target)))}
            _, cache = precision._integer_forward(model, x)
            sums = cache["sums"]
            result["hidden_units"] = {"inactive_fraction": float(np.mean(sums <= 0)), "upper_clip_fraction": float(np.mean(sums >= model["clip"])),
                                      "per_unit_upper_clip_fraction": np.mean(sums >= model["clip"], axis=(0,1)).tolist(),
                                      "per_unit_inactive_fraction": np.mean(sums <= 0, axis=(0,1)).tolist()}
            details["partitions"][name] = result
            predictions[mid, name] = pred
            if name != "test":
                targetname = "static" if arm == "A" else "search10k"
                expected = saved["metrics"][name][targetname]["mae_cp"]
                assert abs(expected-result["integer_own_target"]["mae_cp"]) < 1e-9
            else:
                assert np.array_equal(pred, [stored_predictions[r["query_id"]]["predictions_white_cp"][mid] for r in records])
        model_details[mid] = details
    checks.append("All final shadow arrays re-quantize exactly to saved integer models; all six stored test prediction arrays and train/validation own-target MAE recompute exactly")
    summary, row_details = {}, []
    seeds = [20261020, 20261021, 20261022]
    for name, records in parts.items():
        static = np.asarray([r["classical_static_white_cp"] for r in records], dtype=float)
        target = np.asarray([r["target_white_cp"] for r in records], dtype=float)
        delta = target-static
        stm = np.asarray([1 if chess.Board(r["fen"]).turn else -1 for r in records])
        apred = np.mean([predictions[f"A-{s}", name] for s in seeds], axis=0)
        bpred = np.mean([predictions[f"B-{s}", name] for s in seeds], axis=0)
        learned = bpred-apred
        groups = defaultdict(list)
        for i, row in enumerate(records):
            for g in group_keys(row):
                groups[g].append(i)
        group_results = {}
        for g, ids in sorted(groups.items()):
            idx = np.asarray(ids)
            rr = [records[i] for i in ids]
            xx, yy = delta[idx], learned[idx]
            eligible = np.abs(xx) >= 50
            gd = {"count": len(ids), "families": len(set(r["family_id"] for r in rr)),
                  "target_delta_white_cp": dist(xx), "target_delta_stm_cp": dist(xx*stm[idx]),
                  "B_minus_A_prediction_cp": dist(yy), "desired_correction_vs_learned_correlation": corr(xx, yy),
                  "desired_vs_learned_through_origin_slope": float(xx@yy/(xx@xx)) if xx@xx else None,
                  "correction_sign_alignment_delta_ge50": float(np.mean(xx[eligible]*yy[eligible] > 0)) if eligible.any() else None,
                  "A_static": metrics(apred[idx], static[idx], rr), "B_static": metrics(bpred[idx], static[idx], rr),
                  "A_search10k": metrics(apred[idx], target[idx], rr), "B_search10k": metrics(bpred[idx], target[idx], rr)}
            seed_metrics = {}
            for arm in ("A", "B"):
                for refname, refvalues in (("static", static), ("search10k", target)):
                    seed_metrics[f"{arm}_{refname}"] = [metrics(predictions[f"{arm}-{s}",name][idx], refvalues[idx], rr) for s in seeds]
            if name == "test":
                valid = [i for i in ids if stored_predictions[records[i]["query_id"]]["search100k_white_cp"] is not None]
                vi = np.asarray(valid, dtype=int)
                refs = np.asarray([stored_predictions[records[i]["query_id"]]["search100k_white_cp"] for i in valid], dtype=float)
                vr = [records[i] for i in valid]
                if valid:
                    gd["reference100k_count"] = len(valid)
                    gd["A_search100k"] = metrics(apred[vi], refs, vr)
                    gd["B_search100k"] = metrics(bpred[vi], refs, vr)
                    gd["label100k_minus_10k_cp"] = dist(refs-target[vi])
                    gd["label100k_minus_10k_stm_cp"] = dist((refs-target[vi])*stm[vi])
                    robust = (np.abs(refs) >= 50)&(np.abs(target[vi]) >= 50)
                    gd["label_sign_flip_both_margin50_eligible_count"] = int(robust.sum())
                    gd["label_sign_flip_both_margin50_count"] = int(np.sum(refs[robust]*target[vi][robust] < 0))
                    gd["label_sign_flip_both_margin50_fraction"] = float(np.mean(refs[robust]*target[vi][robust] < 0)) if robust.any() else None
                    for arm in ("A", "B"):
                        seed_metrics[f"{arm}_search100k"] = [metrics(predictions[f"{arm}-{s}",name][vi], refs, vr) for s in seeds]
            gd["seed_metrics"] = seed_metrics
            gd["seed_mean_metrics"] = {key: {m: float(np.mean([v[m] for v in vv])) if vv[0][m] is not None else None for m in vv[0]} for key, vv in seed_metrics.items()}
            group_results[g] = gd
        total_sse = group_results["all"]["count"]*group_results["all"]["seed_mean_metrics"]["B_search10k"]["mse_cp2"]
        for gd in group_results.values():
            gd["fraction_of_partition_rows"] = gd["count"]/len(records)
            gd["fraction_of_B_search10k_squared_error"] = gd["count"]*gd["seed_mean_metrics"]["B_search10k"]["mse_cp2"]/total_sse
        summary[name] = {"groups": group_results,
                         "family_counts": dict(sorted((k, sum(r["family_id"] == k for r in records)) for k in set(r["family_id"] for r in records)))}
        for i, row in enumerate(records):
            record = {"partition": name, "query_id": row["query_id"], "family_id": row["family_id"], "kind": row["kind"], "subtype": row["subtype"],
                      "fen": row["fen"], "depth": row["depth"], "pieces": len(chess.Board(row["fen"]).piece_map()),
                      "turn": "white" if stm[i] == 1 else "black", "static_cp": float(static[i]), "search10k_cp": float(target[i]),
                      "delta_cp": float(delta[i]), "A_mean_cp": float(apred[i]), "B_mean_cp": float(bpred[i]),
                      "B_own_residual_cp": float(bpred[i]-target[i]),
                      "A_by_seed": [float(predictions[f"A-{s}",name][i]) for s in seeds], "B_by_seed": [float(predictions[f"B-{s}",name][i]) for s in seeds]}
            if name == "test":
                record["search100k_cp"] = stored_predictions[row["query_id"]]["search100k_white_cp"]
            row_details.append(record)
    # Family-level contribution is essential because few synthetic families can
    # dominate squared-error training while micro/macro reference signs differ.
    family_details = {}
    for name, records in parts.items():
        per = []
        b = np.mean([predictions[f"B-{s}",name] for s in seeds], axis=0)
        a = np.mean([predictions[f"A-{s}",name] for s in seeds], axis=0)
        target = np.asarray([r["target_white_cp"] for r in records])
        total_sse = np.sum((b-target)**2)
        for fam in sorted(set(r["family_id"] for r in records)):
            ii = np.asarray([i for i,r in enumerate(records) if r["family_id"] == fam])
            rr = [records[i] for i in ii]
            q = {"family_id": fam, "kind": rr[0]["kind"], "count": len(ii),
                 "A_search10k": metrics(a[ii],target[ii],rr), "B_search10k": metrics(b[ii],target[ii],rr),
                 "B_own_training_sse_fraction": float(np.sum((b[ii]-target[ii])**2)/total_sse)}
            per.append(q)
        family_details[name] = per
    after = {str(p): sha(p) for p in input_paths}
    if before != after:
        raise AssertionError("Preserved inputs changed while reading")
    output.mkdir(parents=True)
    write(output/"summary.json", {"schema_version": 1, "created_utc": datetime.now(timezone.utc).isoformat(),
          "scope": "Offline post-hoc exploratory analysis, no model training or engine execution", "checks": checks,
          "averaging": "seed_mean_metrics averages the per-model metrics of the three paired batch-order seeds. Other grouped prediction metrics and row-residuals use mean predictions only for descriptive visualization; they are not an evaluated ensemble.",
          "limits": ["Own target MAE values of A and B use different labels and cannot choose a winner", "100k reference shares B's teacher and is not objective truth", "Search-data strata are not randomized interventions", "Only terminal float weights and integer models exist; no validation curves by epoch", "These old heldout rows are now development evidence"],
          "models": model_details, "partitions": summary, "families": family_details,
          "input_hashes_before_and_after_equal": before == after, "input_hashes": before,
          "script_sha256": sha(Path(__file__))})
    with (output/"row-residuals.jsonl").open("x", encoding="utf-8", newline="\n") as f:
        for record in row_details:
            f.write(json.dumps(record, ensure_ascii=False, sort_keys=True)+"\n")
    write(output/"largest-train-B-residuals.json", sorted([r for r in row_details if r["partition"] == "train"], key=lambda r: abs(r["B_own_residual_cp"]), reverse=True)[:30])
    print(json.dumps({"output": str(output), "rows": len(row_details), "models": len(model_details), "preserved_hashes": len(before), "input_hashes_equal": before == after}))


if __name__ == "__main__":
    main()
