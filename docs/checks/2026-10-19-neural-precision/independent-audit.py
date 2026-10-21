"""Read-only independent replay/arithmetic/provenance audit of this fixed run.

Run with the repository's training Python.  This code imports neither precision
training nor experiment helpers, does not generate data or fit models, and writes
only its new exclusive audit result. python-chess supplies chess rules; model
parsing/checksum, board feature construction, RNG replay, integer prediction,
metrics and benchmark medians are independently implemented here.
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import random
import statistics
import subprocess

import chess
import numpy as np

RUN = Path(__file__).resolve().parent
ROOT = RUN.parents[2]
KEYS = ("input_weights", "hidden_bias", "output_weights")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def rows(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]


def resolve(entry):
    path = Path(entry["path"])
    return path if path.is_absolute() else ROOT / path


def identity(board):
    canonical = min(board.board_fen(), board.mirror().board_fen())
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def model(path):
    tokens = Path(path).read_text(encoding="ascii").split()
    assert tokens[0] == "REGICIDE_NNUE" and tokens[-2] == "checksum"
    numbers = [int(value) for value in tokens[1:-2]]
    version, inputs, hidden, clip, denominator = numbers[:5]
    assert version in (1, 2) and inputs == 768 and hidden == 16
    scale, offset = (1, 5) if version == 1 else (numbers[5], 6)
    assert clip == 127 * scale and 1 <= denominator <= 65536
    assert (version == 1 and scale == 1) or (2 <= scale <= 256 and scale & (scale - 1) == 0 and denominator % scale == 0)
    assert len(numbers) == offset + 16 + 16 + 768 * 16
    header = numbers[:offset]
    result = {"format_version": version, "input_scale": scale, "clip": clip,
              "output_scale": denominator}
    for key, size, shape, bound in (("hidden_bias", 16, (16,), 4096 if version == 1 else 1048576),
                                    ("output_weights", 16, (16,), 8192),
                                    ("input_weights", 768 * 16, (768, 16), 127 if version == 1 else 32767)):
        part = numbers[offset:offset + size]
        assert all(-bound <= value <= bound for value in part)
        result[key] = np.asarray(part, dtype=np.int64).reshape(shape)
        offset += size
    checksum = 14695981039346656037
    values = header + [int(value) for key in ("hidden_bias", "output_weights", "input_weights")
                       for value in result[key].flat]
    for value in values:
        for byte in int(value & 0xFFFFFFFF).to_bytes(4, "little"):
            checksum = ((checksum ^ byte) * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    assert checksum == int(tokens[-1])
    result["checksum"] = checksum
    return result


def pack(records):
    output = np.full((len(records), 2, 32), 768, dtype=np.int64)
    symbols = "PNBRQKpnbrqk"
    for n, record in enumerate(records):
        for i, (square, piece) in enumerate(sorted(chess.Board(record["fen"]).piece_map().items())):
            kind = symbols.index(piece.symbol())
            output[n, 0, i] = kind * 64 + square
            output[n, 1, i] = ((kind + 6) % 12) * 64 + (square ^ 56)
    return output


def predict(value, packed):
    table = np.concatenate((value["input_weights"], np.zeros((1, 16), dtype=np.int64)))
    sums = table[packed].sum(axis=2) + value["hidden_bias"]
    hidden = np.minimum(np.maximum(sums, 0), value["clip"])
    numerator = (hidden[:, 0] - hidden[:, 1]) @ value["output_weights"]
    # Python scalar integers make signed division and absence of overflow clear.
    return np.asarray([max(-28000, min(28000, (abs(int(number)) // value["output_scale"])
                                  * (1 if number >= 0 else -1))) for number in numerator])


def metrics(predictions, targets):
    residual = np.asarray(predictions, dtype=np.float64) - targets
    mse = float(np.square(residual).mean())
    return {"mae_cp": float(np.abs(residual).mean()), "mse_cp2": mse, "rmse_cp": mse**0.5}


def exact_metrics(actual, expected):
    for name in actual:
        assert actual[name] == expected[name], (name, actual, expected)


def changes(value, reference):
    return {key: int(np.count_nonzero(value[key] != reference[key])) for key in KEYS}


def audit_training():
    directory = RUN / "selection"
    freeze, decision, plan = read(directory / "freeze.json"), read(directory / "selection.json"), read(RUN / "protocol.json")
    executed = read(directory / "grid_plan.json")
    assert executed["candidate_grid"] == plan["candidate_grid"]
    for entry in freeze["files"].values():
        assert sha(resolve(entry)) == entry["sha256"]
    training = rows(resolve(freeze["files"]["training_data"]))
    packed, targets = pack(training), np.asarray([row["target_white_cp"] for row in training], dtype=np.float64)
    with np.load(resolve(freeze["files"]["initial_floats"]), allow_pickle=False) as source:
        initial = {key: source[key].copy() for key in KEYS}
    baseline = model(resolve(freeze["files"]["baseline_model"]))
    exact_metrics(metrics(predict(baseline, packed), targets), decision["baseline_train"])
    refs, ref_summary = {}, []
    for scale in (8, 16, 32):
        directory_ref = directory / f"reference-s{scale:03d}"
        reference = model(directory_ref / "initial_float_requantized.nnue")
        rescaled = model(directory_ref / "baseline_exact_rescaled.nnue")
        saved = read(directory_ref / "metrics.json")
        for key in KEYS:
            np.testing.assert_array_equal(reference[key], np.rint(initial[key] * (8 if key == "output_weights" else scale)).astype(np.int64))
            np.testing.assert_array_equal(rescaled[key], baseline[key] * (1 if key == "output_weights" else scale))
        np.testing.assert_array_equal(predict(rescaled, packed), predict(baseline, packed))
        exact_metrics(metrics(predict(reference, packed), targets), saved["initial_float_requantized_train"])
        assert sha(directory_ref / "initial_float_requantized.nnue") == saved["initial_reference_sha256"]
        assert sha(directory_ref / "baseline_exact_rescaled.nnue") == saved["rescaled_baseline_sha256"]
        refs[scale] = reference, rescaled
        ref_summary.append({"input_scale": scale, "untrained_requantized_train": saved["initial_float_requantized_train"]})
    candidates = []
    for index, config in enumerate(executed["candidate_grid"]):
        current = directory / config["candidate_id"]
        saved, integer = read(current / "metrics.json"), model(current / "model.nnue")
        with np.load(current / "float_weights.npz", allow_pickle=False) as source:
            shadow = {key: source[key].copy() for key in KEYS}
        assert saved["config"] == config and saved["grid_index"] == index
        assert len(saved["training_history"]) == config["epochs"] == 120
        assert saved["training_history"][-1]["optimizer_steps"] == 1440
        for key in KEYS:
            np.testing.assert_array_equal(integer[key], np.rint(shadow[key] * (8 if key == "output_weights" else config["input_scale"])).astype(np.int64))
        assert sha(current / "model.nnue") == saved["model_sha256"]
        assert sha(current / "float_weights.npz") == saved["float_weights_sha256"]
        assert integer["checksum"] == saved["model_checksum_fnv1a64"]
        measured = metrics(predict(integer, packed), targets)
        exact_metrics(measured, saved["train"])
        exact_metrics(measured, saved["training_history"][-1]["fake_quantized_train"])
        reference, rescaled = refs[config["input_scale"]]
        assert changes(integer, reference) == saved["changed_integer_parameters_vs_initial_reference"]
        assert changes(integer, rescaled) == saved["changed_integer_parameters_vs_baseline_rescaled"]
        assert changes(integer, baseline) == saved["changed_integer_parameters_vs_baseline_raw"]
        assert changes(shadow, initial) == saved["changed_float_parameters"]
        candidates.append({"grid_index": index, "candidate_id": config["candidate_id"], "train": measured,
                           "changed_integer_parameters_vs_initial_reference": changes(integer, reference)})
    ranked = sorted(candidates, key=lambda item: (item["train"]["mae_cp"], item["train"]["rmse_cp"], item["grid_index"]))
    assert [item["candidate_id"] for item in ranked] == [item["config"]["candidate_id"] for item in decision["ranking"]]
    assert ranked[0]["candidate_id"] == decision["selected_candidate_id"] == freeze["selected_candidate_id"]
    assert freeze["selected_config"] == decision["selected_config"]
    winner = directory / decision["selected_candidate_id"]
    assert (directory / "model.nnue").read_bytes() == (winner / "model.nnue").read_bytes()
    assert (directory / "float_weights.npz").read_bytes() == (winner / "float_weights.npz").read_bytes()
    selected = model(directory / "model.nnue")
    assert selected["checksum"] == freeze["model_checksum_fnv1a64"]
    reverted = dict(selected)
    reverted["input_weights"] = refs[selected["input_scale"]][0]["input_weights"]
    changed_rows = int(np.count_nonzero(predict(selected, packed) != predict(reverted, packed)))
    assert changed_rows == decision["input_learning_ablation_train_only"]["changed_prediction_rows"]
    exact_metrics(metrics(predict(reverted, packed), targets), decision["input_learning_ablation_train_only"]["reverted_inputs_train"])
    return {"frozen_dependencies": len(freeze["files"]), "training_positions": len(training),
            "candidates": candidates, "references": ref_summary,
            "selected_candidate_id": decision["selected_candidate_id"],
            "selected_integer_changes": changes(selected, refs[selected["input_scale"]][0]),
            "input_learning_ablation": decision["input_learning_ablation_train_only"]}


def audit_holdout():
    receipt_path = RUN / "experiment-freeze" / "freeze_receipt.json"
    receipt, meta = read(receipt_path), read(RUN / "holdout" / "holdout_manifest.json")
    report = read(RUN / "holdout-comparison.json")
    for entry in receipt["files"].values():
        assert sha(resolve(entry)) == entry["sha256"]
    assert sha(receipt_path) == meta["freeze_receipt_sha256"] == report["freeze_receipt_sha256"]
    assert sha(RUN / "holdout" / "holdout_manifest.json") == report["holdout_manifest_sha256"]
    assert sha(RUN / "holdout" / "raw_games.jsonl") == meta["raw_games_sha256"]
    assert sha(RUN / "holdout" / "holdout.jsonl") == meta["holdout_sha256"]
    assert datetime.fromisoformat(receipt["created_utc"]) < datetime.fromisoformat(meta["created_utc"])
    assert datetime.fromisoformat(meta["created_utc"]) <= datetime.fromisoformat(meta["finished_utc"]) < datetime.fromisoformat(report["evaluated_utc"])
    assert receipt["holdout_did_not_exist"] is True
    plan = receipt["holdout_plan"]
    assert plan == meta["holdout_plan"] == read(RUN / "protocol.json")["holdout_plan"]
    prior_ids, prior_groups, prior_counts = set(), set(), {}
    original_manifest = read(resolve(receipt["files"]["original_manifest"]))
    prior_groups.update(original_manifest["groups"])
    for name in ("original_train", "original_validation", "prior_holdout"):
        prior_rows = rows(resolve(receipt["files"][name]))
        prior_counts[name] = len(prior_rows)
        for row in prior_rows:
            key = identity(chess.Board(row["fen"]))
            assert key == row["feature_hash"]
            prior_ids.add(key)
            prior_groups.add(row["group_id"])
    prior_groups.update(item["group_id"] for item in rows(resolve(receipt["files"]["prior_raw_games"])))
    actual_raw, actual_rows = rows(RUN / "holdout" / "raw_games.jsonl"), rows(RUN / "holdout" / "holdout.jsonl")
    replayed_raw, replayed_rows, seen = [], [], set()
    candidate_count = excluded_count = duplicate_count = 0
    for game_index in range(plan["games"]):
        group = f"precision-heldout-{plan['seed']}-game-{game_index:06d}"
        assert group not in prior_groups
        board, moves = chess.Board(), []
        rng = random.Random(plan["seed"] + 1000003 * (game_index + 1))
        for ply in range(1, plan["max_plies"] + 1):
            legal = sorted(board.legal_moves, key=lambda move: move.uci())
            if not legal:
                break
            chosen = legal[rng.randrange(len(legal))]
            assert chosen in board.legal_moves
            board.push(chosen)
            moves.append(chosen.uci())
            if board.outcome(claim_draw=True) is not None:
                break
            if ply < plan["sample_start_ply"] or (ply - plan["sample_start_ply"]) % plan["sample_every"]:
                continue
            candidate_count += 1
            key = identity(board)
            if key in prior_ids:
                excluded_count += 1
                continue
            if key in seen:
                duplicate_count += 1
                continue
            seen.add(key)
            replayed_rows.append({"fen": board.fen(en_passant="fen"), "group_id": group,
                                  "ply": ply, "feature_hash": key})
        replayed_raw.append({"group_id": group, "moves_uci": moves})
    assert replayed_raw == actual_raw
    assert len(replayed_rows) == len(actual_rows) == meta["positions"] == report["positions"]
    assert len(actual_raw) == meta["groups"] == report["groups"] == 64
    for actual, expected in zip(actual_rows, replayed_rows, strict=True):
        assert {key: actual[key] for key in expected} == expected
        assert type(actual["target_white_cp"]) is int and type(actual["classical_stm_cp"]) is int
        assert actual["target_white_cp"] == actual["classical_stm_cp"] * (1 if chess.Board(actual["fen"]).turn else -1)
    assert candidate_count == meta["candidate_positions"] == 1894
    assert excluded_count == meta["excluded_prior_inputs"] == 0
    assert duplicate_count == meta["duplicate_inputs_removed"] == 1
    assert not (seen & prior_ids)
    packed, targets = pack(actual_rows), np.asarray([row["target_white_cp"] for row in actual_rows], dtype=np.float64)
    predictions, measured = {}, {}
    models = {}
    for name in ("baseline_model", "precision_model", "same_scale_initial", "trained_inputs_reverted"):
        path = resolve(receipt["files"][name])
        assert sha(path) == report["model_sha256"][name]
        models[name] = model(path)
        predictions[name] = predict(models[name], packed)
        measured[name] = metrics(predictions[name], targets)
        exact_metrics(measured[name], report["metrics"][name])
        np.testing.assert_array_equal(predictions[name], report["predictions"][name])
    for key in KEYS:
        np.testing.assert_array_equal(models["trained_inputs_reverted"][key],
                                      models["same_scale_initial" if key == "input_weights" else "precision_model"][key])
    changed_rows = int(np.count_nonzero(predictions["precision_model"] != predictions["trained_inputs_reverted"]))
    assert changed_rows == report["trained_input_changes_affect_prediction_rows"] == 1861
    for saved in report["per_game"]:
        mask = np.asarray([row["group_id"] == saved["group_id"] for row in actual_rows])
        assert int(mask.sum()) == saved["positions"]
        for name in measured:
            exact_metrics(metrics(predictions[name][mask], targets[mask]), saved[name])
    assert measured["baseline_model"]["mae_cp"] - measured["precision_model"]["mae_cp"] == report["mae_reduction_cp"]
    engine = resolve(receipt["files"]["engine"])
    engine_checks = {}
    fen_input = "\n".join(row["fen"] for row in actual_rows) + "\n"
    for name in ("baseline_model", "precision_model"):
        command = [str(engine), "eval-batch", "--neural-model", str(resolve(receipt["files"][name]))]
        process = subprocess.run(command, input=fen_input, text=True, capture_output=True, timeout=120, check=True)
        cpp = [tuple(int(value) for value in line.split("\t")) for line in process.stdout.splitlines()]
        assert len(cpp) == len(actual_rows) and all(len(row) == 3 for row in cpp)
        for index, (row, values) in enumerate(zip(actual_rows, cpp, strict=True)):
            sign = 1 if chess.Board(row["fen"]).turn else -1
            assert values[0] == row["classical_stm_cp"]
            assert values[1] == values[2] == int(predictions[name][index]) * sign
        engine_checks[name] = {"positions": len(actual_rows), "teacher_mismatches": 0,
                               "python_full_incremental_mismatches": 0, "command": command}
    return {"frozen_experiment_files": len(receipt["files"]), "receipt_precedes_generation": True,
            "raw_game_sequences_replayed": len(actual_raw), "positions_replayed": len(actual_rows),
            "candidate_positions": candidate_count, "duplicate_inputs_removed": duplicate_count,
            "prior_position_counts": prior_counts, "prior_unique_input_count": len(prior_ids),
            "prior_color_rank_mirror_input_overlap": 0, "prior_game_overlap": 0,
            "metrics": measured, "input_learning_changes_prediction_rows": changed_rows,
            "engine_checks": engine_checks}


def audit_benchmark():
    record = read(RUN / "evaluation-benchmark.json")
    assert record["error"] is None and record["exit_code"] == record["execution"]["returncode"] == 0
    assert record["inputs_unchanged"] is True
    assert record["source"]["snapshot_before"] == record["source"]["snapshot_after"]
    for path, digest in record["source"]["snapshot_before"]["files"].items():
        assert sha(ROOT / path) == digest
    for entry in record["inputs"].values():
        assert sha(entry["path"]) == entry["sha256_before"] == entry["sha256_after"]
    parsed = []
    for row in csv.DictReader(io.StringIO(record["execution"]["stdout"])):
        numeric = {key: (value if key == "workload" else int(value)) for key, value in row.items()}
        numeric["ns_per_operation"] = numeric["elapsed_ns"] / numeric["iterations"]
        parsed.append(numeric)
    assert parsed == record["records"] and len(parsed) == record["summary"]["samples"] == 96
    workloads = list(record["summary"]["models"]["baseline"]["workloads"])
    paired = {(row["model"], row["round"], row["workload"]): row for row in parsed}
    assert len(paired) == 96
    for row in parsed:
        assert row["transitions"] == 163 and row["iterations"] == 500000 and 0 <= row["round"] < 8
        assert row["model_order"] == (row["model"] if row["round"] % 2 == 0 else 1 - row["model"])
        assert row["order"] == (workloads.index(row["workload"]) if row["round"] % 2 == 0 else 5 - workloads.index(row["workload"]))
    summaries = {}
    for model_index, name in enumerate(("baseline", "qat")):
        saved = record["summary"]["models"][name]
        measured = {}
        for work in workloads:
            times = [paired[model_index, repeat, work]["ns_per_operation"] for repeat in range(8)]
            current = {"median_ns_per_operation": statistics.median(times),
                       "min_ns_per_operation": min(times), "max_ns_per_operation": max(times)}
            assert current == saved["workloads"][work]
            measured[work] = current["median_ns_per_operation"]
        for repeat in range(8):
            checksums = {paired[model_index, repeat, work]["checksum"] for work in workloads if work != "classical_after"}
            assert len(checksums) == 1
        for out, numerator, denominator in (
                ("median_paired_speedup_full_over_incremental", "neural_full_after", "neural_update_snapshot_restore"),
                ("median_paired_speedup_board_full_over_incremental", "board_play_full_undo", "board_play_incremental_undo")):
            value = statistics.median([paired[model_index, repeat, numerator]["elapsed_ns"]
                                       / paired[model_index, repeat, denominator]["elapsed_ns"] for repeat in range(8)])
            assert value == saved[out]
            measured[out] = value
        summaries[name] = measured
    ratios = {work: statistics.median([paired[1, repeat, work]["elapsed_ns"] / paired[0, repeat, work]["elapsed_ns"]
                                       for repeat in range(8)]) for work in workloads}
    assert ratios == record["summary"]["median_paired_qat_over_baseline_cost_ratio"]
    return {"samples_recomputed": len(parsed), "models_times_ns": summaries,
            "median_paired_precision_over_baseline_cost_ratio": ratios,
            "scope": "Checks recorded timings and arithmetic; no rerun, CPU isolation, load measurement or strength inference"}


def main():
    started = datetime.now(timezone.utc).isoformat()
    result = {"schema_version": 1, "audit_started_utc": started, "status": "PASS",
              "audit_source_sha256": sha(Path(__file__)), "training": audit_training(),
              "holdout": audit_holdout(), "benchmark": audit_benchmark(),
              "policy": "Existing source/model/data artifacts read only; no training, tuning or generation",
              "audit_finished_utc": datetime.now(timezone.utc).isoformat()}
    with (RUN / "independent-audit.json").open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(result, handle, indent=2, sort_keys=True)
        handle.write("\n")
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
