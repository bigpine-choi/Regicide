"""Temporary normal/adversarial tests of the frozen precision-QAT experiment.

Usage: python tests/neural_precision_experiment_tests.py --engine BUILD/regicide
Only test seeds are used. This does not read or generate the research holdout.
"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import random
import shutil
import sys
import tempfile

import chess
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import neural_training as base
import neural_precision as precision
import neural_precision_experiment as experiment


def read_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def write_rows(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def reject(action, expected: str, output: Path | None = None) -> None:
    try:
        action()
    except ValueError as error:
        assert expected in str(error).lower(), (expected, str(error))
    else:
        raise AssertionError("adversarial edit accepted: " + expected)
    if output is not None:
        assert not output.exists(), "failure left a completed-looking output"


def snapshot(directory: Path) -> dict[str, bytes]:
    return {file.relative_to(directory).as_posix(): file.read_bytes()
            for file in directory.rglob("*") if file.is_file()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", type=Path, required=True)
    args = parser.parse_args()
    engine = args.engine.resolve()
    assert engine.is_file(), "build the engine first"
    with tempfile.TemporaryDirectory(prefix="regicide-precision-experiment-") as temporary:
        root = Path(temporary)
        data, old_source, old_holdout = root / "data", root / "old-source", root / "old-heldout"
        base.generate_dataset(engine, data, games=8, max_plies=24, sample_every=2, seed=61141)
        _, partitions = base.read_dataset(data)
        # A small synthetic earlier experiment keeps CI independent of the
        # checked-in research models and their much larger historical datasets.
        base.generate_dataset(engine, old_source, games=4, max_plies=24, sample_every=2, seed=71141)
        _, old_partitions = base.read_dataset(old_source)
        old_holdout.mkdir()
        old_rows = []
        for records in old_partitions.values():
            for row in records:
                row["group_id"] = "earlier-test-" + row["group_id"]
                old_rows.append(row)
        raw_old = read_rows(old_source / "raw_games.jsonl")
        for game in raw_old:
            game["group_id"] = "earlier-test-" + game["group_id"]
        write_rows(old_holdout / "holdout.jsonl", old_rows)
        write_rows(old_holdout / "raw_games.jsonl", raw_old)
        base.write_json(old_holdout / "holdout_manifest.json", {
            "rows": len(old_rows), "holdout_sha256": base.sha256(old_holdout / "holdout.jsonl"),
            "raw_games_sha256": base.sha256(old_holdout / "raw_games.jsonl")})
        initial = base.bootstrap_float()
        initial_path, baseline_path = root / "initial.npz", root / "baseline.nnue"
        np.savez(initial_path, **initial)
        base.save_model(base.quantize(initial), baseline_path)
        grid = [{"candidate_id": "experiment-test", "input_scale": 8, "epochs": 4,
                 "input_bias_lr": .05, "output_lr": .02, "seed": 91143, "batch_size": 16}]
        holdout_plan = {"games": 4, "seed": 81143, "max_plies": 24,
                        "sample_every": 2, "sample_start_ply": 6}
        protocol = root / "protocol.json"
        base.write_json(protocol, {"candidate_grid": grid, "holdout_plan": holdout_plan})
        selection, frozen, holdout = root / "selection", root / "frozen", root / "holdout"
        selected = precision.select_model(data, baseline_path, initial_path, selection, plan_path=protocol)
        selected_bytes = snapshot(selection)

        # Matching train bytes are insufficient: the original validation
        # identity list must belong to the exact manifest used for training.
        wrong_data = root / "wrong-original"
        shutil.copytree(data, wrong_data)
        manifest_path = wrong_data / "split_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["adversarial_extra_field"] = True
        base.write_json(manifest_path, manifest)
        reject(lambda: experiment.freeze_experiment(selection, wrong_data, old_holdout,
            engine, protocol, root / "bad-manifest-freeze", holdout), "manifest",
            root / "bad-manifest-freeze")

        # Re-hashing an internally different executed grid must still fail
        # its semantic comparison with the independently bound protocol.
        grid_path, training_freeze_path = selection / "grid_plan.json", selection / "freeze.json"
        original_grid, original_training_freeze = grid_path.read_bytes(), training_freeze_path.read_bytes()
        altered_grid = json.loads(original_grid)
        altered_grid["candidate_grid"][0]["epochs"] += 1
        base.write_json(grid_path, altered_grid)
        altered_training_freeze = json.loads(original_training_freeze)
        altered_training_freeze["files"]["grid_plan"]["sha256"] = base.sha256(grid_path)
        base.write_json(training_freeze_path, altered_training_freeze)
        precision.verify_freeze(training_freeze_path)
        reject(lambda: experiment.freeze_experiment(selection, data, old_holdout,
            engine, protocol, root / "bad-grid-freeze", holdout), "grid", root / "bad-grid-freeze")
        grid_path.write_bytes(original_grid)
        training_freeze_path.write_bytes(original_training_freeze)
        assert snapshot(selection) == selected_bytes

        receipt = experiment.freeze_experiment(selection, data, old_holdout,
            engine, protocol, frozen, holdout)
        receipt_path = frozen / "freeze_receipt.json"
        original_receipt = receipt_path.read_bytes()
        assert not holdout.exists() and receipt["holdout_did_not_exist"]
        changed_receipt = json.loads(original_receipt)
        changed_receipt["holdout_plan"]["seed"] += 1
        base.write_json(receipt_path, changed_receipt)
        reject(lambda: experiment.generate_holdout(frozen), "plan", holdout)
        receipt_path.write_bytes(original_receipt)
        experiment.verify_experiment(frozen)
        frozen_bytes = snapshot(frozen)

        excluded, old_groups = experiment.prior_inputs(data, old_holdout)
        wanted_excluded = {row["feature_hash"] for records in partitions.values() for row in records}
        wanted_excluded.update(row["feature_hash"] for row in old_rows)
        assert excluded == wanted_excluded
        metadata = experiment.generate_holdout(frozen)
        rows, raw = read_rows(holdout / "holdout.jsonl"), read_rows(holdout / "raw_games.jsonl")
        assert all(row["feature_hash"] not in excluded and row["group_id"] not in old_groups for row in rows)
        assert len({row["feature_hash"] for row in rows}) == len(rows)
        replay = {}
        for index, game in enumerate(raw):
            rng, board = random.Random(81143 + 1000003 * (index + 1)), chess.Board()
            assert game["group_id"] == f"precision-heldout-81143-game-{index:06d}"
            for ply, text in enumerate(game["moves_uci"], 1):
                legal = sorted(board.legal_moves, key=lambda move: move.uci())
                assert rng.choice(legal).uci() == text, "game differs from independently replayed RNG"
                board.push_uci(text)
                replay[game["group_id"], ply] = board.fen(en_passant="fen")
        assert all(replay[row["group_id"], row["ply"]] == row["fen"] for row in rows)
        output = root / "comparison.json"
        report = experiment.evaluate_holdout(frozen, output)
        assert report["python_cpp_mismatches"] == report["teacher_label_mismatches"] == 0
        assert set(report["metrics"]) == {"baseline_model", "precision_model",
                                          "same_scale_initial", "trained_inputs_reverted"}
        target = np.array([row["target_white_cp"] for row in rows])
        for name, values in report["predictions"].items():
            difference = np.array(values) - target
            assert report["metrics"][name]["mae_cp"] == float(np.mean(np.abs(difference)))
            assert report["metrics"][name]["rmse_cp"] == float(np.sqrt(np.mean(difference**2)))
        assert report["positions"] == metadata["positions"] == len(rows)
        assert snapshot(frozen) == frozen_bytes and snapshot(selection) == selected_bytes

        # Updating the manifest hash cannot turn a forged move list into the
        # predeclared legal game generated by the frozen RNG.
        raw_path, meta_path, rows_path = (holdout / "raw_games.jsonl",
            holdout / "holdout_manifest.json", holdout / "holdout.jsonl")
        original_raw, original_meta, original_rows = (raw_path.read_bytes(),
            meta_path.read_bytes(), rows_path.read_bytes())
        forged_raw = copy.deepcopy(raw)
        forged_raw[0]["moves_uci"][0] = "e2e4" if forged_raw[0]["moves_uci"][0] != "e2e4" else "d2d4"
        write_rows(raw_path, forged_raw)
        changed_meta = json.loads(original_meta)
        changed_meta["raw_games_sha256"] = base.sha256(raw_path)
        base.write_json(meta_path, changed_meta)
        reject(lambda: experiment.evaluate_holdout(frozen, root / "forged-game.json"),
               "replay", root / "forged-game.json")
        raw_path.write_bytes(original_raw)
        meta_path.write_bytes(original_meta)

        # Forged labels retain the right side-to-move sign and file hashes;
        # re-evaluation by the unchanged classical teacher must catch them.
        forged_rows = copy.deepcopy(rows)
        for row in forged_rows:
            row["target_white_cp"] += 137
            row["classical_stm_cp"] = row["target_white_cp"] * (1 if chess.Board(row["fen"]).turn else -1)
        write_rows(rows_path, forged_rows)
        changed_meta = json.loads(original_meta)
        changed_meta["holdout_sha256"] = base.sha256(rows_path)
        base.write_json(meta_path, changed_meta)
        reject(lambda: experiment.evaluate_holdout(frozen, root / "forged-targets.json"),
               "teacher", root / "forged-targets.json")
        rows_path.write_bytes(original_rows)
        meta_path.write_bytes(original_meta)
        model_path = selection / "model.nnue"
        original_model = model_path.read_bytes()
        model_path.write_bytes(original_model + b"\n")
        reject(lambda: experiment.evaluate_holdout(frozen, root / "changed-model.json"),
               "changed", root / "changed-model.json")
        model_path.write_bytes(original_model)
        reject(lambda: experiment.generate_holdout(frozen), "overwrite")
        reject(lambda: experiment.evaluate_holdout(frozen, output), "overwrite")
        experiment.verify_experiment(frozen)
        assert snapshot(frozen) == frozen_bytes and snapshot(selection) == selected_bytes
        print(json.dumps({"training_rows": len(partitions["train"]),
            "old_validation_rows": len(partitions["validation"]),
            "synthetic_prior_holdout_rows": len(old_rows), "new_holdout_rows": len(rows),
            "selected_candidate_id": selected["selected_candidate_id"],
            "input_integer_changes_at_same_scale": receipt["input_integer_changes_from_same_scale_initial"],
            "models_compared": 4, "python_cpp_full_incremental_mismatches": 0,
            "teacher_label_mismatches": 0, "independent_legal_rng_replay": True,
            "all_prior_mirrored_inputs_excluded": True,
            "frozen_artifacts_unchanged": True,
            "adversarial_rejections": ["original_manifest_mismatch", "bound_protocol_executed_grid_mismatch",
                "receipt_generation_plan_mismatch", "rehashed_forged_raw_games",
                "rehashed_consistently_signed_wrong_teacher_targets", "frozen_model_changed",
                "holdout_overwrite", "report_overwrite"]}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
