"""Read-only diagnosis of the previously exposed score-preservation candidate.

No training, native/teacher engine invocation, or new held-out labels are used.
A printed search PV can end before quiescence; its endpoint is descriptive and
is never assumed to be the backed-up evaluator input without a native witness.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import chess
import numpy as np

import neural_stm as stm
import train_nnue_ordering as ordering

ROOT = Path(__file__).resolve().parents[1]
PREVIOUS = ROOT / "docs/checks/2026-10-22-nnue-preservation/attempt-002"
DATA = ROOT / "docs/checks/2026-10-22-nnue-leaf/data-001"
OLD_PAIRS = ROOT / "docs/checks/2026-10-22-nnue-generalization/data-001"
WARM = ROOT / "docs/checks/2026-10-22-nnue-ordering/attempt-001/model.nnue"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def summary(values):
    a = np.asarray(values, dtype=np.float64)
    return {"count": len(a), "mean": float(np.mean(a)),
            "mean_absolute": float(np.mean(np.abs(a))),
            "median_absolute": float(np.median(np.abs(a))),
            "maximum_absolute": float(np.max(np.abs(a)))}


def predict(model, board):
    return stm.quantized_white(board, model)


def diagnose(output):
    gate_path = PREVIOUS / "move-choice-gate.json"
    gate = read_json(gate_path)
    model_paths = {"warm": WARM, "candidate": PREVIOUS / "model.nnue"}
    if any(sha(path) != gate[f"{key}_sha256"] for key, path in model_paths.items()):
        raise ValueError("Previously exposed gate/model bindings changed")
    models = {key: stm.load_model(path) for key, path in model_paths.items()}
    inputs = [gate_path, *model_paths.values(), PREVIOUS / "training.json",
              PREVIOUS / "selection.json", DATA / "native-leaf-receipts.jsonl",
              DATA / "selected-leaves.json", Path(__file__), ROOT / "scripts/neural_stm.py",
              ROOT / "scripts/train_nnue_ordering.py"]
    cases = []
    for case in gate["cases"]:
        source = case["source"]
        root = chess.Board(source["fen"])
        sign = 1 if root.turn else -1
        teacher = case["teacher_bestmove_uci"]
        teacher_child = root.copy(stack=False)
        teacher_child.push_uci(teacher)
        old_move = case["searches"]["warm"]["move_uci"]
        old_child = root.copy(stack=False)
        old_child.push_uci(old_move)
        model_details = {}
        for key, model in models.items():
            searched = case["searches"][key]
            child_scores = []
            for move in root.legal_moves:
                child = root.copy(stack=False)
                child.push(move)
                child_scores.append((sign * predict(model, child), move.uci()))
            maximum = max(score for score, _ in child_scores)
            # Ties are retained; no tie ordering is claimed as an actual move.
            static_best = sorted(move for score, move in child_scores if score == maximum)
            endpoint = root.copy(stack=False)
            for move in searched["pv_uci"]:
                endpoint.push_uci(move)
            endpoint_cp = predict(model, endpoint)
            model_details[key] = {
                "searched_move_uci": searched["move_uci"],
                "searched_white_cp": searched["white_cp"],
                "completed_depth": searched["depth"],
                "seldepth": searched["raw_info"]["seldepth"],
                "reported_pv_plies": len(searched["pv_uci"]),
                "static_best_moves_uci": static_best,
                "teacher_in_static_best_set": teacher in static_best,
                "searched_move_in_static_best_set": searched["move_uci"] in static_best,
                "teacher_vs_warm_selected_direct_child_margin_mover_cp": sign * (
                    predict(model, teacher_child) - predict(model, old_child)),
                "reported_pv_endpoint_fen": endpoint.fen(en_passant="fen"),
                "reported_pv_endpoint_static_white_cp": endpoint_cp,
                "endpoint_static_minus_searched_white_cp": endpoint_cp - searched["white_cp"],
                "endpoint_is_backed_up_static_witness": False,
            }
        cases.append({"query_id": source["query_id"], "family_id": source["family_id"],
                      "teacher_bestmove_uci": teacher,
                      "move_changed": model_details["warm"]["searched_move_uci"] != model_details["candidate"]["searched_move_uci"],
                      "mover_quality_gain_cp": case["mover_quality_gain_cp"],
                      "searched_root_score_change_mover_cp": sign * (
                          model_details["candidate"]["searched_white_cp"] - model_details["warm"]["searched_white_cp"]),
                      "models": model_details})
    pairs = {}
    for split in ("train", "tuning"):
        path = DATA / f"pairs-{split}.jsonl"
        old_path = OLD_PAIRS / f"pairs-{split}.jsonl"
        inputs.extend((path, old_path))
        rows = read_rows(path)
        ia, ta, sa, ib, tb, sb = ordering.pack_pairs(rows)
        target = ta - tb
        predictions = {key: stm.predict(model, ia, sa) - stm.predict(model, ib, sb)
                       for key, model in models.items()}
        per_pair = [{"root_id": row["root_id"], "family_id": row["family_id"],
                     "target_gap_white_cp": int(target[i]),
                     "warm_gap_white_cp": int(predictions["warm"][i]),
                     "candidate_gap_white_cp": int(predictions["candidate"][i]),
                     "both_immediate_children_of_root": all(
                         len(row[side]["history"]) == len(row["root_history"]) + 1
                         for side in ("a", "b"))}
                    for i, row in enumerate(rows)]
        pairs[split] = {"count": len(rows), "same_bytes_as_pre_leaf_pair_file": sha(path) == sha(old_path),
                        "metrics": ordering.score_pairs(rows, models["warm"], models["candidate"]),
                        "gap_change_cp": summary(predictions["candidate"] - predictions["warm"]),
                        "wrong_before_and_after": int(np.sum((predictions["warm"] * target <= 0) &
                                                              (predictions["candidate"] * target <= 0))),
                        "cases": per_pair}
    receipts = read_rows(DATA / "native-leaf-receipts.jsonl")
    receipt_by_id = {row["id"]: row for row in receipts}
    sample_cases = []
    for receipt in receipts:
        search = receipt["search"]
        root_sign = 1 if chess.Board(receipt["root_fen"]).turn else -1
        for sample in receipt["samples"]:
            leaf_sign = 1 if sample["turn"] == "white" else -1
            root_mover_score = root_sign * leaf_sign * sample["score_cp"]
            sample_cases.append({"root_id": receipt["id"],
                                 "first_move_is_root_bestmove": sample["path_from_root"][0] == search["bestmove"],
                                 "sample_iteration_completed": sample["iteration_depth"] <= search["depth"],
                                 "root_mover_static_equals_root_score": root_mover_score == search["score_cp"],
                                 "phase": sample["phase"], "ply": sample["ply"],
                                 "qply": sample["qply"]})
    accepted_leaf_inputs = {}
    for split in ("train", "tuning"):
        path = DATA / f"{split}.jsonl"
        inputs.append(path)
        rows = [row for row in read_rows(path) if row.get("kind") == "actual_search_static_leaf"]
        comparisons = []
        for row in rows:
            witness = row["witness"]
            receipt = receipt_by_id[row["source_root_id"]]
            search = receipt["search"]
            root_sign = 1 if chess.Board(receipt["root_fen"]).turn else -1
            leaf_sign = 1 if witness["turn"] == "white" else -1
            comparisons.append((witness["path_from_root"][0] == search["bestmove"],
                                witness["iteration_depth"] <= search["depth"],
                                root_sign * leaf_sign * witness["score_cp"] == search["score_cp"]))
        accepted_leaf_inputs[split] = {
            "count": len(rows),
            "first_move_is_root_bestmove": sum(value[0] for value in comparisons),
            "sample_iteration_completed": sum(value[1] for value in comparisons),
            "root_mover_static_equals_root_score": sum(value[2] for value in comparisons),
            "combined_three_conditions": sum(all(value) for value in comparisons),
            "numeric_equality_is_not_a_backed_up_witness": True,
        }
    selection = read_json(PREVIOUS / "selection.json")
    training = read_json(PREVIOUS / "training.json")
    summaries = {}
    wrong_old = [case for case in cases if case["models"]["warm"]["searched_move_uci"] != case["teacher_bestmove_uci"]]
    for key in models:
        margins = [case["models"][key]["teacher_vs_warm_selected_direct_child_margin_mover_cp"] for case in wrong_old]
        summaries[key] = {
            "teacher_bestmove_matches": sum(case["models"][key]["searched_move_uci"] == case["teacher_bestmove_uci"] for case in cases),
            "teacher_in_static_best_set": sum(case["models"][key]["teacher_in_static_best_set"] for case in cases),
            "searched_move_in_static_best_set": sum(case["models"][key]["searched_move_in_static_best_set"] for case in cases),
            "wrong_warm_cases_with_positive_teacher_direct_child_margin": sum(value > 0 for value in margins),
            "wrong_warm_cases_with_negative_teacher_direct_child_margin": sum(value < 0 for value in margins),
            "wrong_warm_case_teacher_direct_child_margins_cp": margins,
            "completed_depth_counts": dict(Counter(case["models"][key]["completed_depth"] for case in cases)),
            "seldepth_summary": summary([case["models"][key]["seldepth"] for case in cases]),
            "reported_pv_endpoint_static_equals_root_score": sum(case["models"][key]["endpoint_static_minus_searched_white_cp"] == 0 for case in cases),
        }
    parameter_changes = {key: {"changed": int(np.count_nonzero(models["candidate"][key] != models["warm"][key])),
                               "total": int(models["warm"][key].size),
                               "change_quantized_units": summary(models["candidate"][key].ravel() - models["warm"][key].ravel())}
                         for key in stm.PARAMETER_KEYS}
    report = {
        "schema_version": 1, "created_utc": datetime.now(timezone.utc).isoformat(),
        "purpose": "Explain score-MSE versus actual move-choice mismatch on prior exposed evidence only",
        "input_sha256": {str(path): sha(path) for path in inputs},
        "training_runs": 0, "native_searches": 0, "teacher_queries": 0,
        "fresh_gate_labels_read": 0,
        "previous_candidate": {"selected_epoch": selection["selected_epoch"],
                               "anchor_mse_before_cp2": selection["initial_validation"]["anchor"]["mse_cp2"],
                               "anchor_mse_after_cp2": selection["selected_validation"]["anchor"]["mse_cp2"],
                               "leaf_tuning_mse_before_cp2": training["leaf_metrics"]["tuning"]["warm"]["mse_cp2"],
                               "leaf_tuning_mse_after_cp2": training["leaf_metrics"]["tuning"]["ordering"]["mse_cp2"],
                               "preservation_weight": selection["preservation_weight"],
                               "training_output_drift": selection["preservation_targets"]["selected_drift"]},
        "old_gate": {"positions": len(cases), "move_changes": sum(case["move_changed"] for case in cases),
                     "mean_teacher_quality_gain_cp": float(np.mean([case["mover_quality_gain_cp"] for case in cases])),
                     "searched_root_score_change_mover_cp": summary([case["searched_root_score_change_mover_cp"] for case in cases]),
                     "models": summaries, "cases": cases},
        "prior_pair_learning": pairs,
        "prior_leaf_reservoir": {"search_roots": len(receipts), "sample_occurrences": len(sample_cases),
                                  "sample_first_move_is_root_bestmove": sum(row["first_move_is_root_bestmove"] for row in sample_cases),
                                  "sample_iteration_completed": sum(row["sample_iteration_completed"] for row in sample_cases),
                                  "sample_root_mover_static_equals_root_score": sum(row["root_mover_static_equals_root_score"] for row in sample_cases),
                                  "sample_combined_three_conditions": sum(row["first_move_is_root_bestmove"] and row["sample_iteration_completed"] and row["root_mover_static_equals_root_score"] for row in sample_cases),
                                  "phase_counts": dict(Counter(row["phase"] for row in sample_cases)),
                                  "ply_summary": summary([row["ply"] for row in sample_cases]),
                                  "sampling_policy": "Reservoir occurrences across all evaluated branches, not paired winning/refuting backed-up leaves",
                                  "numeric_equality_is_not_a_backed_up_witness": True},
        "accepted_prior_leaf_point_inputs": accepted_leaf_inputs,
        "quantized_parameter_changes": parameter_changes,
        "supported_findings": [
            "Point MSE improved without directly optimizing the argmax of backed-up competing root moves.",
            "The actual-leaf data addition kept the earlier immediate-child pair files byte-identical.",
            "Reservoir leaves cover many alternatives; a sample need not determine the root score or chosen move.",
            "Printed native PV endpoints are not automatically the quiescence evaluator endpoint; no endpoint is used as a training label in this diagnosis.",
            "Preservation permits nonzero output changes while limiting drift; root choice changes are sparse in the previously exposed gate.",
        ],
        "recommended_controlled_change": {
            "loss": "Keep point teacher MSE, pair-gap MSE, and old-training warm-output preservation MSE",
            "pair_inputs": "Use actual native evaluator calls that back up competing forced-root-move search scores, with full histories and legal endpoint witnesses",
            "comparison_target": "Teacher root-mover quality difference between the competing root moves; orient leaf scores consistently to white before loss",
            "checks": "Fixed-depth full-window TT-off completed search; reject bounds, terminal/draw-adjusted, missing static witness, and score/end-point mismatches; separate fresh gate before teacher queries",
            "retained_guards": "Current architecture, epoch-zero anchor limit, tuning-only checkpoint selection, early stopping, best restore, integer step acceptance, frozen fresh gate",
        },
        "limits": [
            "No causal contribution is assigned to preservation strength, teacher budget, or architecture from this single candidate.",
            "Immediate-child margins do not prove an alternative root-search margin; forced-move backed-up leaf collection is needed for that comparison.",
            "Teacher recommendation matches and centipawn deltas are finite-screen observations, not Elo or playing-strength proof.",
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"output": str(output), "old_gate": {key: value for key, value in report["old_gate"].items() if key not in ("cases", "models")}, "models": summaries,
                      "pairs": {key: {field: value[field] for field in ("count", "same_bytes_as_pre_leaf_pair_file", "wrong_before_and_after", "gap_change_cp")} for key, value in pairs.items()},
                      "leaf_reservoir": report["prior_leaf_reservoir"]}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    diagnose(parser.parse_args().output)
