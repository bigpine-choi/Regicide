"""Compare the fixed public-data reproduction; no setting selection or edits."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DIR = ROOT / "build/neural-cost-quality"
RUN = ROOT / "build/learning-cycle/runs/000004-01d14f9c73af"
PUBLIC = ROOT / "docs/checks/2026-10-19-neural-cost-quality/run/quality"
EXPECTED = "5b363fa05487d7707d943c26c8a8343c86122315ecd61f88003692ff449291b3"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    original_path = RUN / "model/report.json"
    reproduced_path = DIR / "public-reproduction/report.json"
    original = json.loads(original_path.read_text(encoding="utf-8"))
    reproduced = json.loads(reproduced_path.read_text(encoding="utf-8"))
    model = DIR / "public-reproduction" / reproduced["model_path"]
    original_model = RUN / "model" / original["model_path"]
    semantic_fields = ("architecture", "baseline", "batch_size", "correction", "correction_cap_cp",
        "epochs", "history", "implementation_sha256", "initial_model_sha256", "initialization", "l2",
        "learning_rate", "loss", "loss_components", "metrics", "model_format", "model_version",
        "optimizer", "parameter_count", "rng", "scale_cp", "seed", "selected_epoch", "target",
        "turn_distribution")
    comparisons = {key: reproduced[key] == original[key] for key in semantic_fields}
    public_inputs = {"manifest": sha(PUBLIC / "manifest.json"),
                     "positions": sha(PUBLIC / "positions.jsonl"),
                     "games": sha(PUBLIC / "games.jsonl")}
    checks = {"model_sha256_matches_expected": sha(model) == EXPECTED,
              "model_bytes_equal_original": model.read_bytes() == original_model.read_bytes(),
              "selected_epoch_is_81": reproduced["selected_epoch"] == 81,
              "dataset_counts_equal_original": reproduced["dataset"]["counts"] == original["dataset"]["counts"],
              "public_inputs_unchanged_during_training": reproduced["dataset"]["raw_sha256"] == public_inputs,
              "all_semantic_training_fields_equal": all(comparisons.values())}
    result = {"schema": "regicide.neural.public-reproduction.v1", "status": "pass" if all(checks.values()) else "fail",
        "use": "post-experiment reproducibility verification only; no training/model settings changed",
        "source_commit": "426830231d5291991a589cf73b058c88b653d1fe",
        "public_dataset": "docs/checks/2026-10-19-neural-cost-quality/run/quality/manifest.json",
        "public_input_sha256": public_inputs, "original_training_report_sha256": sha(original_path),
        "reproduction_training_report_sha256": sha(reproduced_path), "model_sha256": sha(model),
        "expected_model_sha256": EXPECTED, "selected_epoch": reproduced["selected_epoch"],
        "configuration": {key: reproduced[key] for key in ("model_version", "seed", "epochs", "batch_size", "learning_rate", "l2")},
        "dataset_counts": reproduced["dataset"]["counts"], "metrics": reproduced["metrics"],
        "semantic_training_fields_equal": comparisons, "checks": checks,
        "provenance_note": "Public privacy redaction changes some JSON file hashes. Training reads and validates the public game histories/positions directly; the same deterministic training produces byte-identical model and numerical history/metrics."}
    with (DIR / "public-reproduction-report.json").open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    print(json.dumps({"status": result["status"], "model_sha256": result["model_sha256"],
                      "selected_epoch": result["selected_epoch"], "checks": checks}))
    if result["status"] != "pass":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
