#!/usr/bin/env python3
"""Recompute the native neural profile's summaries from its raw samples.

This audits internal consistency and the declared measurement design. It does
not establish that the host was idle, validate model provenance, or measure
playing strength; keep the resource log and source/model hashes with the report.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import math
from pathlib import Path
import statistics
from typing import Any


class AuditError(ValueError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AuditError(message)


def integer(value: Any, label: str, *, minimum: int = 0) -> int:
    require(type(value) is int and value >= minimum, f"invalid {label}")
    return value


def close(value: Any, expected: float, label: str) -> None:
    require(type(value) in (int, float) and math.isfinite(value), f"nonfinite {label}")
    require(math.isclose(value, expected, rel_tol=2e-12, abs_tol=1e-9), f"incorrect {label}")


def audit(data: dict[str, Any]) -> dict[str, Any]:
    require(data["schema"] == "regicide-neural-profile-v1", "unexpected schema")
    require(data["frozen_source_commit"] == "00dec89b08de4e5f573b18b440a93cd82f4b3311", "unexpected baseline")
    require(data["scope"] == "full_static_evaluation_including_V1_material_or_V2_classical_evaluation", "unexpected scope")
    positions = integer(data["positions"], "positions", minimum=1)
    require(data["variant_order"] == ["old_v063", "new"], "unexpected variants")
    require(data["timed_options"] == {"shield": True, "structure": True}, "unexpected timed options")
    require(data["orders_even_blocks"] == ["AB", "BA", "BA", "AB"], "unexpected even orders")
    require(data["orders_odd_blocks"] == ["BA", "AB", "AB", "BA"], "unexpected odd orders")
    require(data["pairs_per_block"] == 4, "unexpected pair count")
    models = data["models"]
    require(type(models) is list and len(models) > 0, "missing models")
    comparisons = 0
    for model_index, model in enumerate(models):
        require(model["model_index"] == model_index, "model index order changed")
        require(type(model["model_filename"]) is str and len(model["model_filename"]) > 0, "missing model filename")
        parity = model["equivalence"]
        require(parity["option_combinations_per_position"] == 4, "missing option combination")
        require(parity["comparisons"] == positions * 4, "wrong parity comparison count")
        require(parity["mismatch_count"] == 0 and parity["mismatches"] == [], "score mismatch")
        comparisons += parity["comparisons"]
    require(data["all_scores_and_checksums_match"] is True, "profile declared failure")
    if data["mode"] == "verify":
        require(data["blocks"] == 0 and data["model_order_per_block"] == [], "verify unexpectedly timed")
        require(all("samples" not in model for model in models), "verify has timed samples")
        return {"schema": "regicide-neural-profile-audit-v1", "mode": "verify", "models": len(models),
                "positions": positions, "score_comparisons": comparisons, "raw_samples": 0, "passed": True}
    require(data["mode"] == "profile", "unknown mode")
    require(data["blocks"] == 9, "unexpected block count")
    expected_model_orders = []
    for block in range(9):
        expected_model_orders.append([
            (block // 2 + (len(models) - 1 - rank if block % 2 else rank)) % len(models)
            for rank in range(len(models))
        ])
    require(data["model_order_per_block"] == expected_model_orders, "model order changed")
    observed_global: list[tuple[int, int, int, int, str]] = []
    model_summaries = []
    for model_index, model in enumerate(models):
        calls = integer(model["calls_per_sample"], "call count", minimum=1)
        require(model["warmup_calls_per_variant"] == 10000, "warmup count changed")
        warmup = model["warmup_checksums"]
        require(type(warmup) is list and len(warmup) == 2 and warmup[0] == warmup[1], "warmup checksum mismatch")
        require(all(type(value) is int for value in warmup), "warmup checksum is not an integer")
        calibration = model["calibration"]
        require(calibration["variant"] == "old_v063", "candidate calibrated work")
        calibration_calls = integer(calibration["calls"], "calibration calls")
        calibration_ns = integer(calibration["elapsed_ns"], "calibration time")
        integer(calibration["checksum"], "calibration checksum", minimum=-(2**63))
        target_ms = calibration["target_ms"]
        require(type(target_ms) in (int, float) and math.isfinite(target_ms) and 10 <= target_ms <= 1000, "invalid calibration target")
        if calibration_calls:
            require(calibration_calls == ((100000 + positions - 1) // positions) * positions, "calibration workload changed")
            require(calibration_ns > 0, "calibration clock failed")
            estimate = math.ceil(calibration_calls * target_ms * 1000000 / calibration_ns)
            bounded = min(max(estimate, positions), 999000000)
            require(calls == ((bounded + positions - 1) // positions) * positions, "calibrated call count changed")
        else:
            require(calibration_ns == 0 and calibration["checksum"] == 0, "partial manual calibration")
        samples = model["samples"]
        require(type(samples) is list and len(samples) == 72, "missing or extra raw sample")
        checksum = samples[0]["checksum"]
        integer(checksum, "sample checksum", minimum=-(2**63))
        by_block: dict[int, dict[str, list[float]]] = {}
        previous_sequence = -1
        for sample in samples:
            block = integer(sample["block"], "sample block")
            pair = integer(sample["pair"], "sample pair")
            sequence = integer(sample["sequence"], "sample sequence")
            require(block < 9 and pair < 4 and sequence > previous_sequence, "sample order changed")
            previous_sequence = sequence
            order = (data["orders_odd_blocks"] if block % 2 else data["orders_even_blocks"])[pair]
            require(sample["order"] == order, "pair order changed")
            variant = sample["variant"]
            require(variant in ("old_v063", "new"), "unknown sample variant")
            elapsed = integer(sample["elapsed_ns"], "elapsed time", minimum=1)
            ns_per_call = elapsed / calls
            close(sample["ns_per_call"], ns_per_call, "sample ns per call")
            require(sample["checksum"] == checksum, "sample checksum changed")
            by_block.setdefault(block, {"old_v063": [], "new": []})[variant].append(ns_per_call)
            observed_global.append((sequence, block, model_index, pair, variant))
        require(model["timed_checksums_match"] is True, "timed checksum failed")
        require(len(model["block_means_ns_per_call"]) == 9, "missing block means")
        old_values, new_values, ratios = [], [], []
        for block in range(9):
            require(set(by_block[block]) == {"old_v063", "new"}, "missing block variant")
            require(all(len(values) == 4 for values in by_block[block].values()), "unbalanced block")
            old_value = statistics.mean(by_block[block]["old_v063"])
            new_value = statistics.mean(by_block[block]["new"])
            ratio = new_value / old_value
            declared = model["block_means_ns_per_call"][block]
            require(declared["block"] == block, "block means reordered")
            close(declared["old_v063"], old_value, "old block mean")
            close(declared["new"], new_value, "new block mean")
            close(declared["new_over_old"], ratio, "block ratio")
            old_values.append(old_value)
            new_values.append(new_value)
            ratios.append(ratio)
        summaries = model["medians"]
        close(summaries["old_v063_ns"], statistics.median(old_values), "old median")
        close(summaries["new_ns"], statistics.median(new_values), "new median")
        close(summaries["paired_new_over_old"], statistics.median(ratios), "paired median ratio")
        close(model["block_ratio_min"], min(ratios), "minimum block ratio")
        close(model["block_ratio_max"], max(ratios), "maximum block ratio")
        model_summaries.append({"model_index": model_index, "model_filename": model["model_filename"],
                                "calls_per_sample": calls, "raw_samples": len(samples),
                                "paired_new_over_old": statistics.median(ratios)})
    expected_global = []
    for block, model_order in enumerate(expected_model_orders):
        for model_index in model_order:
            for pair, order in enumerate(data["orders_odd_blocks"] if block % 2 else data["orders_even_blocks"]):
                for variant in (("old_v063", "new") if order == "AB" else ("new", "old_v063")):
                    expected_global.append((len(expected_global), block, model_index, pair, variant))
    require(sorted(observed_global) == expected_global, "global order, duplicate, or omitted sample")
    require(Counter(value[0] for value in observed_global) == Counter(range(len(expected_global))), "duplicate sample sequence")
    return {"schema": "regicide-neural-profile-audit-v1", "mode": "profile", "positions": positions,
            "score_comparisons": comparisons, "raw_samples": len(observed_global), "models": model_summaries,
            "passed": True}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", type=Path)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    try:
        result = audit(json.loads(arguments.profile.read_text(encoding="utf-8")))
    except (AuditError, KeyError, TypeError, ValueError) as error:
        parser.exit(1, f"profile_neural_audit: {error}\n")
    text = json.dumps(result, indent=2, ensure_ascii=True, allow_nan=False) + "\n"
    if arguments.output is not None:
        with arguments.output.open("x", encoding="utf-8", newline="\n") as output:
            output.write(text)
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
