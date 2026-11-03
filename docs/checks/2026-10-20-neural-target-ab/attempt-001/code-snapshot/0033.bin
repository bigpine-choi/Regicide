"""Explicit train-only calibration grid, recorded before any fresh holdout.
Uses the same tested QAT algorithm; changes only candidate settings.
"""
import argparse
import json
from pathlib import Path
import re
import neural_qat as qat


def select_from_plan(plan_path, data, baseline_model, initial_floats, output):
    plan_path = Path(plan_path).resolve()
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    grid = plan["candidate_grid"]
    if not isinstance(grid, list) or not 1 <= len(grid) <= 16: raise ValueError("invalid grid count")
    identifiers = set()
    for config in grid:
        qat._validate_config(config)
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", config["candidate_id"]) or config["candidate_id"] in identifiers:
            raise ValueError("candidate IDs must be unique safe directory names")
        identifiers.add(config["candidate_id"])
    original = qat.FIXED_GRID
    try:
        qat.FIXED_GRID = grid
        result = qat.select_model(data, baseline_model, initial_floats, output)
    finally:
        qat.FIXED_GRID = original
    # The freeze already binds the full executed candidate grid and selection.
    qat._write_exclusive_json(Path(output) / "calibration_driver.json", {
        "plan": {"path": str(plan_path), "sha256": qat.base.sha256(plan_path)},
        "driver": {"path": str(Path(__file__).resolve()), "sha256": qat.base.sha256(Path(__file__))},
        "freeze_sha256": qat.base.sha256(Path(output) / "freeze.json"),
        "scope": "training-only amended grid; no heldout access"})
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for key in ("plan", "data", "baseline-model", "initial-floats", "output"):
        p.add_argument("--" + key, type=Path, required=True)
    args = p.parse_args()
    print(json.dumps(select_from_plan(args.plan, args.data, args.baseline_model, args.initial_floats, args.output), indent=2))

if __name__ == "__main__": main()
