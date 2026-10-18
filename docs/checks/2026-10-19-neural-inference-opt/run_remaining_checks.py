import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

root = Path.cwd()
out = root / "build/neural-inference-opt"
binary = "build-neural-v4/regicide.exe"
original = out / "handicap.log"
commands = [
    ("handicap_rerun", "tests/handicap_play_tests.py"),
    ("archive", "tests/game_archive_tests.py"),
    ("bootstrap", "tests/godot_bootstrap_tests.py"),
    ("game_runner", "tests/game_runner_tests.py"),
    ("compare", "tests/compare_engines_tests.py"),
    ("benchmark", "tests/benchmark_search_tests.py"),
]
report = {
    "binary": binary,
    "binary_sha256": hashlib.sha256((root / binary).read_bytes()).hexdigest(),
    "original_failed_handicap_log": "handicap.log",
    "original_failed_handicap_log_sha256": hashlib.sha256(original.read_bytes()).hexdigest(),
    "failure_observed": "One loopback HTTP ConnectionAbortedError WinError 10053; engine-resumption check passed in that run.",
    "policy": "Rerun the failed suite once without product/test changes; then run only the five remaining suites sequentially.",
    "checks": [],
}
for name, script in commands:
    log_path = out / (name + ".log")
    if log_path.exists():
        raise SystemExit("Refusing to overwrite " + log_path.name)
    print("START", name, flush=True)
    start = time.monotonic()
    with log_path.open("w", encoding="utf-8", newline="\n") as log:
        completed = subprocess.run(
            [sys.executable, script, binary], cwd=root,
            stdout=log, stderr=subprocess.STDOUT,
            env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
        )
    report["checks"].append({
        "check": name, "script": script, "returncode": completed.returncode,
        "seconds": time.monotonic() - start, "log": log_path.name,
        "log_sha256": hashlib.sha256(log_path.read_bytes()).hexdigest(),
    })
    report["all_completed_passed"] = all(row["returncode"] == 0 for row in report["checks"])
    report["complete"] = len(report["checks"]) == len(commands)
    (out / "python-remaining-checks.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print("PASS" if completed.returncode == 0 else "FAIL", name, flush=True)
    if completed.returncode:
        raise SystemExit(completed.returncode)
print("ALL REMAINING CHECKS PASSED", flush=True)
