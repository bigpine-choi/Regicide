"""Reproduce descriptive diagnostics of the rejected V1 model; no model tuning.

Run from the repository root. This helper only reads the immutable old public
corpus/model and writes a new diagnostic JSON. Test labels are examined after
the old rejection for explanation, never for choosing a new model checkpoint.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name("old-model-diagnostics.json"))
    args = parser.parse_args()
    root = args.root.resolve()
    sys.path.insert(0, str(root / "scripts"))
    import train_neural as training  # Thread limits are set before NumPy import.
    import neural_model as neural
    from classical_evaluation import classical_cp_stm
    import chess
    import numpy as np
    base = root / "docs/checks/2026-10-18-learning-cycle/store/runs/000002-95a3500d5501"
    model_path = base / "model/small-v1.nn"
    corpus_path = base / "corpus/positions.jsonl"
    training_path = base / "model/report.json"
    expected = "4b9dc188bb289d83f6563e9a996c27eaca4aef886c3421a8125c06dd108c3256"
    if sha(model_path) != expected:
        raise ValueError("This diagnostic requires the original rejected model bytes")
    model = neural.load_model(model_path)
    if model.version != 1:
        raise ValueError("This diagnostic is specifically about the original V1 format")
    old_report = json.loads(training_path.read_text(encoding="utf-8"))
    rows = [json.loads(line) for line in corpus_path.read_text(encoding="utf-8").splitlines()]
    train = [row for row in rows if row["split"] == "train"]
    train_x = np.stack([neural.encode_features(row["fen"]) for row in train])
    unseen = np.flatnonzero(train_x.sum(axis=0) == 0)
    initial = neural.initialize_model(old_report["seed"])
    report = {
        "schema": "regicide.rejected-v1.diagnostics.v1", "status": "complete",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "Descriptive post-rejection evidence; causal contribution of individual factors is not isolated; no hyperparameter/model sweep",
        "input_sha256": {str(path.relative_to(root)).replace("\\", "/"): sha(path)
                         for path in (model_path, corpus_path, training_path)},
        "implementation_sha256": {str(path.relative_to(root)).replace("\\", "/"): sha(path)
                                  for path in (root / "scripts/neural_model.py", root / "scripts/train_neural.py",
                                               root / "scripts/classical_evaluation.py")},
        "helper_sha256": sha(__file__),
        "capacity": {"parameters": sum(value.size for value in model.parameters().values()),
                     "training_samples": len(train), "training_games": len({row["game_id"] for row in train}),
                     "parameters_per_training_sample": sum(value.size for value in model.parameters().values()) / len(train),
                     "never_active_training_features": len(unseen),
                     "never_active_columns_are_unchanged_initial_random_weights": bool(np.array_equal(model.w1[:, unseen], initial.w1[:, unseen]))},
        "architecture_observation": "V1 replaces all classical positional heuristics with material+600*z; no bounded residual or matrix regularization",
        "sampling_observation": "Old even-ply openings and fixed every-four-ply sampling select one actual moving side; correction diagnoses supply a small number of Black positions",
        "color_caveat": "Features already identify own/opponent and rank-flip Black. Absolute color imbalance is not by itself proof of a color bug; real opposite-turn positions carry distinct tactical contexts",
        "turn_flip_caveat": "Same-board turn-flip probes change the right to move; their score sums are descriptive, not a required chess-rule identity or a source of synthetic/sign-flipped training labels",
        "baseline_caveat": "Material-only and classical-static diagnostics are not arena strength results; teacher labels are finite own-engine search estimates",
        "splits": {},
    }
    for split in training.SPLITS:
        selected = [row for row in rows if row["split"] == split]
        boards = [chess.Board(row["fen"]) for row in selected]
        x = np.stack([neural.encode_features(board) for board in boards])
        material = np.array([row["material_cp_stm"] for row in selected])
        teacher = np.array([row["teacher_cp_stm"] for row in selected])
        outcome = np.array([row["outcome_stm"] for row in selected])
        classical = np.array([classical_cp_stm(board) for board in boards])
        raw_z = neural.predict_z(x, model)
        predicted = material + 600 * raw_z
        mixed_target = .8 * np.tanh(teacher / 600) + .2 * outcome
        predicted_tanh = np.tanh(predicted / 600)
        teacher_tanh = np.tanh(teacher / 600)
        implied_cp = 600 * np.arctanh(np.clip(mixed_target, -1 + 1e-15, 1 - 1e-15))
        opposite = []
        for board in boards:
            board = board.copy(stack=False)
            board.turn = not board.turn
            board.ep_square = None
            opposite.append(neural.encode_features(board))
        turn_flip_sum = 600 * (raw_z + neural.predict_z(np.stack(opposite), model))
        any_unseen = x[:, unseen].sum(axis=1) > 0
        stats = {
            "samples": len(selected), "games": len({row["game_id"] for row in selected}),
            "turns": {"w": sum(board.turn == chess.WHITE for board in boards),
                      "b": sum(board.turn == chess.BLACK for board in boards)},
            "mixed_target_mse": float(np.mean((predicted_tanh - mixed_target) ** 2)),
            "material_mixed_target_mse": float(np.mean((np.tanh(material / 600) - mixed_target) ** 2)),
            "classical_static_mixed_target_mse": float(np.mean((np.tanh(classical / 600) - mixed_target) ** 2)),
            "teacher_tanh_mse": float(np.mean((predicted_tanh - teacher_tanh) ** 2)),
            "material_teacher_tanh_mse": float(np.mean((np.tanh(material / 600) - teacher_tanh) ** 2)),
            "classical_static_teacher_tanh_mse": float(np.mean((np.tanh(classical / 600) - teacher_tanh) ** 2)),
            "cp_mae_from_teacher": float(np.mean(abs(predicted - teacher))),
            "material_cp_mae_from_teacher": float(np.mean(abs(material - teacher))),
            "classical_static_cp_mae_from_teacher": float(np.mean(abs(classical - teacher))),
            "positions_using_never_trained_features": int(any_unseen.sum()),
            "never_trained_feature_activations": int(x[:, unseen].sum()),
            "teacher_and_final_outcome_opposite_sign": int((teacher * outcome < 0).sum()),
            "mixed_target_cp_shift_abs_quantiles_50_90_100": np.quantile(abs(implied_cp - teacher), [.5, .9, 1]).tolist(),
            "unbounded_residual_cp_quantiles_0_10_50_90_100": np.quantile(predicted - material, [0, .1, .5, .9, 1]).tolist(),
            "same_board_turn_flip_score_sum_abs_quantiles_50_90_100": np.quantile(abs(turn_flip_sum), [.5, .9, 1]).tolist(),
            "same_board_turn_flip_score_sum_abs_ge100": int((abs(turn_flip_sum) >= 100).sum()),
        }
        for name, mask in (("teacher_abs_cp_le100", abs(teacher) <= 100),
                           ("teacher_abs_cp_le300", abs(teacher) <= 300),
                           ("teacher_abs_cp_le600", abs(teacher) <= 600),
                           ("early_ply_le32", np.array([row["ply"] <= 32 for row in selected])),
                           ("pieces_le10", np.array([len(board.piece_map()) <= 10 for board in boards])),
                           ("has_untrained_feature", any_unseen),
                           ("no_untrained_feature", ~any_unseen)):
            stats[name] = {"samples": int(mask.sum()),
                           "mixed_target_mse": float(np.mean((predicted_tanh[mask] - mixed_target[mask]) ** 2)) if mask.any() else None,
                           "cp_mae_from_teacher": float(np.mean(abs(predicted[mask] - teacher[mask]))) if mask.any() else None,
                           "material_cp_mae_from_teacher": float(np.mean(abs(material[mask] - teacher[mask]))) if mask.any() else None,
                           "classical_static_cp_mae_from_teacher": float(np.mean(abs(classical[mask] - teacher[mask]))) if mask.any() else None}
        report["splits"][split] = stats
    report["historical_training_curve"] = [row for row in old_report["history"] if row["epoch"] in (0, 1, 8, 10, 20, 50, 100)]
    report["mixed_target_examples"] = [{"teacher_cp": cp, "outcome_stm": result,
                                         "implied_target_cp": 600 * float(np.arctanh(.8 * np.tanh(cp / 600) + .2 * result))}
                                        for cp, result in ((0, 1), (0, -1), (500, -1), (1000, -1))]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"status": report["status"], "model_sha256": expected,
                      "train_turns": report["splits"]["train"]["turns"],
                      "test_mse": report["splits"]["test"]["mixed_target_mse"]}))


if __name__ == "__main__":
    main()
