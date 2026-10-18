import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

root = Path.cwd()
out = root / "build/neural-inference-opt"
binary = root / "build-neural-v4/regicide.exe"
godot = Path("<user>/Tools/Godot/Godot_v4.7.2-stable_win64.exe")
report = {
    "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
    "godot": godot.name,
    "mode": "Four existing headless scene suites against the real engine; no human/manual GUI game.",
    "checks": [],
}
commands = []
for label, script in [
    ("godot-integration", "tests/godot_integration_tests.py"),
    ("godot-handicap", "tests/handicap_godot_tests.py"),
    ("godot-archive", "tests/archive_godot_tests.py"),
    ("godot-hint", "tests/hint_godot_tests.py"),
]:
    commands.append((label, [sys.executable, script, "--godot", str(godot),
                             "--binary", str(binary), "--output", str(out / label)]))
node = shutil.which("node")
if node:
    commands.extend([
        ("web-syntax", [node, "--check", "web/human-play/app.js"]),
        ("web-hints", [node, "tests/web_hint_tests.js"]),
    ])
report["node_available"] = node is not None
for label, command in commands:
    log_path = out / (label + ".log")
    if log_path.exists():
        raise SystemExit("Refusing to overwrite " + log_path.name)
    print("START", label, flush=True)
    start = time.monotonic()
    with log_path.open("w", encoding="utf-8", newline="\n") as log:
        result = subprocess.run(command, cwd=root, stdout=log, stderr=subprocess.STDOUT,
                                env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
    report["checks"].append({
        "check": label, "returncode": result.returncode, "seconds": time.monotonic() - start,
        "log": log_path.name, "log_sha256": hashlib.sha256(log_path.read_bytes()).hexdigest(),
    })
    report["all_completed_passed"] = all(row["returncode"] == 0 for row in report["checks"])
    report["complete"] = len(report["checks"]) == len(commands)
    (out / "client-checks.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print("PASS" if result.returncode == 0 else "FAIL", label, flush=True)
    if result.returncode:
        raise SystemExit(result.returncode)
print("ALL CLIENT CHECKS PASSED", flush=True)
