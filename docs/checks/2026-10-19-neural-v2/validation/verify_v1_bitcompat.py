"""Compare V1 arithmetic to frozen Git sources on one fixed synthetic task.

No native engine or arena is launched. This is an implementation regression
check, not a model/hyperparameter experiment or a strength result.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--reference", default="8298d65")
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name("v1-bitcompat.json"))
    args = parser.parse_args()
    root = args.root.resolve()
    sys.path.insert(0, str(root / "scripts"))
    import train_neural as current
    import neural_model as neural
    import numpy as np
    commit = subprocess.run(["git", "rev-parse", args.reference + "^{commit}"], cwd=root,
                            capture_output=True, text=True, check=True).stdout.strip()
    original_sha = {}
    for name, path in (("legacy_neural_model", "scripts/neural_model.py"),
                       ("legacy_train", "scripts/train_neural.py")):
        raw = subprocess.run(["git", "show", commit + ":" + path], cwd=root,
                             capture_output=True, check=True).stdout
        original_sha[path] = hashlib.sha256(raw).hexdigest()
        source = raw.decode("utf-8")
        if name == "legacy_train":
            source = source.replace("from neural_model import", "from legacy_neural_model import")
        module = types.ModuleType(name)
        module.__file__ = str(root / path)
        sys.modules[name] = module
        exec(compile(source, path, "exec"), module.__dict__)
    x = np.zeros((32, 768))
    x[::2, 0] = 1
    x[1::2, 1] = 1
    batch = x, np.zeros(32), np.tile([.5, -.5], 16)
    batches = {split: batch for split in current.SPLITS}
    settings = {"epochs": 80, "batch_size": 16, "seed": 23}
    old, old_report = sys.modules["legacy_train"].train(batches, **settings)
    new, new_report = current.train(batches, **settings)
    history_equal = old_report == new_report
    arrays_equal = {name: bool(np.array_equal(old.parameters()[name], value))
                    for name, value in new.parameters().items()}
    with tempfile.TemporaryDirectory() as temporary:
        temporary = Path(temporary)
        sys.modules["legacy_neural_model"].export_model(old, temporary / "old.nn")
        neural.export_model(new, temporary / "new.nn")
        old_sha, new_sha = digest(temporary / "old.nn"), digest(temporary / "new.nn")
    report = {"schema": "regicide.v1.bitcompat.v1", "created_utc": datetime.now(timezone.utc).isoformat(),
              "status": "passed" if history_equal and all(arrays_equal.values()) and old_sha == new_sha else "failed",
              "scope": "One fixed synthetic arithmetic regression, not playing strength or a training sweep",
              "reference_commit": commit, "reference_source_sha256": original_sha,
              "current_source_sha256": {path: digest(root / path) for path in original_sha},
              "helper_sha256": digest(__file__), "settings": settings,
              "batch": {"samples": 32, "features": "alternating input0/input1", "baseline_cp": 0,
                        "targets": "alternating +0.5/-0.5", "same_batch_for_all_splits": True},
              "all_epoch_losses_and_checkpoint_selection_bit_identical": history_equal,
              "parameter_arrays_bit_identical": arrays_equal,
              "parameter_count": sum(value.size for value in new.parameters().values()),
              "reference_model_sha256": old_sha, "current_model_sha256": new_sha,
              "selected_epoch": new_report["selected_epoch"]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"status": report["status"], "reference_commit": commit,
                      "model_sha256": new_sha, "parameters": report["parameter_count"]}))
    if report["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
