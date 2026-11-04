"""Reproduce six public leaf observations with an explicitly supplied driver.

This wrapper was written after the recorded interactive runs; those runs were
not rerun merely to validate this wrapper. The same six CLI inputs are retained.
No private game, external engine, or live runtime is read or modified.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path,
                        help="New directory; existing paths are refused.")
    args = parser.parse_args()
    binary = args.binary.resolve(strict=True)
    cases = json.loads(Path(__file__).with_name("fixtures.json").read_text(encoding="utf-8"))
    args.output.mkdir(parents=True, exist_ok=False)
    before = sha256(binary)
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    records = []
    for case in cases:
        command = [str(binary), "--depth", str(case["depth"]), "--nodes", str(case["nodes"])]
        stdin = "\t".join([case["initial_fen"], case["history_uci"],
                            case["root_move"], case["target_uci"]]) + "\n"
        started = time.monotonic()
        try:
            process = subprocess.run(command, input=stdin, text=True, capture_output=True,
                                     timeout=30, **flags)
        except subprocess.TimeoutExpired as exc:
            # subprocess.run kills and waits for its own direct child on timeout.
            raise RuntimeError("trace timed out: " + case["name"]) from exc
        (args.output / (case["name"] + ".stdout.json")).write_text(process.stdout, encoding="utf-8")
        record = {"case": case["name"], "command": command, "stdin": stdin,
                  "returncode": process.returncode, "stderr": process.stderr,
                  "uncontrolled_wall_ms": round((time.monotonic() - started) * 1000, 3)}
        records.append(record)
        (args.output / "execution.json").write_text(json.dumps({"binary_sha256": before,
            "records": records}, indent=2), encoding="utf-8")
        if process.returncode:
            raise RuntimeError("trace failed: " + case["name"] + ": " + process.stderr)
        row = json.loads(process.stdout)
        if not row.get("position_unchanged") or not row.get("requested_iteration_completed"):
            raise RuntimeError("incomplete or mutated trace: " + case["name"])
        if row.get("causal_root_support_claim") is not False:
            raise RuntimeError("unexpected attribution contract")
        print(json.dumps({"case": case["name"], "matches": row["match_count"],
                          "search": row["search"]}), flush=True)
    if sha256(binary) != before:
        raise RuntimeError("driver changed during collection")


if __name__ == "__main__":
    main()
