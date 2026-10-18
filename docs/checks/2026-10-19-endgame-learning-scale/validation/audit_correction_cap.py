"""Post-freeze read-only oracle lower-bound audit; no training/search/selection."""
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import train_neural as TRAIN  # establishes one-thread arithmetic before NumPy
import numpy as np
from neural_model import CORRECTION_CAP, SCALE, load_model, predict_z

RUN = ROOT / "build/learning-cycle/runs/000006-39915e221a26"
CURVE = RUN / "curve"
OUTPUT = ROOT / "build/endgame-learning-scale/correction-cap-audit.json"
curve = json.loads((CURVE / "report.json").read_text())
assert curve["status"] == "complete" and CORRECTION_CAP == 100 and SCALE == 600
frozen = {}
panels, batches = {}, {}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def freeze(path):
    path = Path(path)
    frozen[path] = sha(path)


def add_source(label, manifest, splits):
    manifest = Path(manifest)
    document = json.loads(manifest.read_text())
    freeze(manifest)
    rows_path = manifest.parent / document.get("positions_path", document.get("samples_path"))
    rows = list(TRAIN.json_lines(rows_path))
    for filename, expected in document["files_sha256"].items():
        source = manifest.parent / filename
        assert sha(source) == expected
        freeze(source)
    data, metadata = TRAIN.load_dataset(manifest, allow_empty_splits=True, model_version=2)
    for split in splits:
        x, hce, target = data[split]
        selected_rows = [(index, row) for index, row in enumerate(rows) if row["split"] == split]
        assert len(selected_rows) == len(target) > 0
        teacher = np.array([row["teacher_cp_stm"] for _, row in selected_rows], dtype=np.float64)
        # The trainer uses math.tanh per row; NumPy's vector tanh can differ by
        # one last bit. Preserve the exact trainer targets for all MSE checks.
        assert np.allclose(target, np.tanh(teacher / SCALE), rtol=0, atol=2e-16)
        low, high = np.tanh((hce - CORRECTION_CAP) / SCALE), np.tanh((hce + CORRECTION_CAP) / SCALE)
        oracle = np.clip(target, low, high)
        base = np.tanh(hce / SCALE)
        mse_floor = float(np.mean((oracle - target) ** 2))
        mse_hce = float(np.mean((base - target) ** 2))
        outside = np.logical_or(target < low, target > high)
        gap_cp = np.maximum(0.0, np.abs(teacher - hce) - CORRECTION_CAP)
        per_position = []
        for local, (source_index, row) in enumerate(selected_rows):
            per_position.append({"source_row_index": source_index, "game_id": row["game_id"],
                "family": row["family"], "ply": row["ply"], "fen": row["fen"],
                "hce_cp_stm": int(hce[local]), "teacher_cp_stm": int(teacher[local]),
                "teacher_minus_hce_cp": int(teacher[local] - hce[local]),
                "target_tanh": float(target[local]), "reachable_low_tanh": float(low[local]),
                "reachable_high_tanh": float(high[local]), "independent_oracle_tanh": float(oracle[local]),
                "oracle_squared_error": float((oracle[local] - target[local]) ** 2),
                "target_outside_transformed_interval": bool(outside[local]),
                "minimum_cp_gap_outside_cap": float(gap_cp[local])})
        name = label + "/" + split
        panels[name] = {"source_manifest_sha256": sha(manifest), "source_positions_sha256": sha(rows_path),
            "samples": len(target), "games": metadata["counts"][split]["games"],
            "families": metadata["counts"][split]["families"],
            "classical_only_mse": mse_hce, "independent_oracle_mse_infimum": mse_floor,
            "floor_fraction_of_hce_mse": mse_floor / mse_hce if mse_hce else None,
            "largest_possible_hce_mse_reduction_fraction_under_cap": (mse_hce - mse_floor) / mse_hce if mse_hce else None,
            "teacher_cp_outside_hce_plus_minus_100": int(np.sum(np.abs(teacher - hce) > CORRECTION_CAP)),
            "target_outside_transformed_interval": int(np.sum(outside)),
            "minimum_cp_gap_outside_cap": {"mean": float(np.mean(gap_cp)), "max": float(np.max(gap_cp))},
            "per_position_bounds": per_position}
        batches[name] = data[split]


freeze(CURVE / "report.json")
for filename in ("classical_evaluation.py", "neural_model.py", "train_neural.py"):
    freeze(ROOT / "scripts" / filename)
add_source("bootstrap", CURVE / "sources/bootstrap/manifest.json", ["train", "validation", "test"])
add_source("new_endgame", CURVE / "sources/endgame/manifest.json", ["train", "validation", "test"])
for arm in curve["arms"]:
    add_source("arm_added_" + str(arm["added_train"]), CURVE / f"added-{arm['added_train']:06d}/dataset/manifest.json", ["train"])

models = []
for arm in curve["arms"]:
    for trained in arm["runs"]:
        path = CURVE / trained["model_path"]
        freeze(path)
        assert sha(path) == trained["model_sha256"]
        model = load_model(path)
        assert model.version == 2
        own_train = "arm_added_" + str(arm["added_train"]) + "/train"
        results = {}
        for panel in ("bootstrap/train", "new_endgame/train", "new_endgame/validation", "new_endgame/test", own_train):
            x, hce, target = batches[panel]
            correction = CORRECTION_CAP * np.tanh(predict_z(x, model))
            predicted = np.tanh((hce + correction) / SCALE)
            actual = float(np.mean((predicted - target) ** 2))
            assert np.all(correction >= -CORRECTION_CAP) and np.all(correction <= CORRECTION_CAP)
            floor, baseline = panels[panel]["independent_oracle_mse_infimum"], panels[panel]["classical_only_mse"]
            assert actual + 1e-15 >= floor
            result = {"samples": len(target), "actual_mse": actual, "oracle_floor_mse": floor,
                "excess_mse_above_oracle_floor": actual - floor,
                "actual_minus_hce_mse": actual - baseline,
                "fraction_of_oracle_headroom_realized": (baseline - actual) / (baseline - floor) if baseline != floor else None,
                "correction_cp": {"min": float(np.min(correction)), "max": float(np.max(correction)),
                    "mean": float(np.mean(correction)), "mean_absolute": float(np.mean(np.abs(correction))),
                    "abs_ge_95_count": int(np.sum(np.abs(correction) >= 95))},
                "actual_normalized_predictions_sha256": hashlib.sha256(np.asarray(predicted, dtype='<f8').tobytes()).hexdigest()}
            if panel.startswith("new_endgame/") and not panel.endswith("/train"):
                split = panel.split("/")[-1]
                recorded = trained["independent_endgame"][split]["selected_mse"]
                assert abs(actual - recorded) < 1e-14
                result["recorded_curve_mse_matches"] = True
                result["actual_normalized_predictions"] = [float(value) for value in predicted]
            results[panel] = result
        models.append({"added_train": arm["added_train"], "seed": trained["seed"],
            "model_sha256": sha(path), "selected_epoch": trained["selected_epoch"], "panels": results})

for path, expected in frozen.items():
    assert sha(path) == expected, "read-only audit input changed: " + str(path)

candidate = curve["candidate"]
selected = next(row for row in models if row["added_train"] == candidate["added_train"] and row["seed"] == candidate["seed"])
report = {"schema": "regicide.endgame.correction-cap-audit.v1", "status": "complete",
    "study_run": RUN.relative_to(ROOT / "build/learning-cycle").as_posix(),
    "source_commit": json.loads((RUN / "cycle.json").read_text())["checkout"]["commit"],
    "scope": "post-freeze descriptive audit only; no new game, search, label, training, model choice or promotion",
    "cap_cp": CORRECTION_CAP, "normalization_scale_cp": SCALE,
    "method": "target=tanh(teacher_cp/600); interval=[tanh((HCE-100)/600),tanh((HCE+100)/600)]; clip target independently per position and average squared residual",
    "interpretation_limits": [
        "This is an optimistic infimum for an independently adjustable per-position oracle, not an implemented or trained network.",
        "A shared finite neural model may not attain this bound; feature aliasing, generalization and optimization constraints are ignored.",
        "Nonzero floor shows that the cap necessarily prevents exact fitting of some finite teacher labels; teacher labels remain fallible.",
        "The floor does not prove that the cap caused an observed regression or that increasing it would improve chess strength.",
        "Transformed MSE can saturate, so CP-outside-cap and transformed-interval counts are reported separately.",
        "The predeclared largest-arm first-seed candidate remains unchanged; these post-hoc measurements cannot select another model."],
    "frozen_inputs_sha256": {str(path.relative_to(ROOT)): expected for path, expected in frozen.items()},
    "helper_sha256": sha(__file__), "panels": panels, "models": models,
    "predeclared_candidate": {key: candidate[key] for key in ("added_train", "seed", "model_sha256", "selected_epoch")},
    "candidate_new_holdout": {key: selected["panels"][key] for key in ("new_endgame/validation", "new_endgame/test")}}
OUTPUT.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
print(json.dumps({"status": "complete", "panels": {name: {key: value[key] for key in (
    "samples", "classical_only_mse", "independent_oracle_mse_infimum", "floor_fraction_of_hce_mse",
    "target_outside_transformed_interval")} for name, value in panels.items()},
    "candidate_holdout": {name: {key: value[key] for key in ("actual_mse", "oracle_floor_mse", "excess_mse_above_oracle_floor", "fraction_of_oracle_headroom_realized")}
                          for name, value in report["candidate_new_holdout"].items()}}, indent=2))
