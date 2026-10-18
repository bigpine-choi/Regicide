"""Snapshot/rerun the preservation checks for the inference-only change.

This helper writes ignored audit evidence only. It never changes source,
learning state, model registries, or installed executables.
"""

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import subprocess

BASELINE = "00dec89b08de4e5f573b18b440a93cd82f4b3311"
BASELINE_BINARY = "605681320aff062ba2878eece74f453bf6ee451f1faa223e74a18caece328e5a"
EXISTING_PRIMARY_EOL_DRIFT = {
    "path": "tests/data/neural_diagnostic_positions_v1.json",
    "initial_sha256": "ec72e7023899dfdd01460d8840a4597231da7c773c26797ec1a82482ac4b064a",
    "canonical_sha256": "271b5e9288c07fc63d8d59e283bd2d83cc33ad62571d2a823d636e03909672c5",
    "initial_working_git_blob": "8b5b7f178f85ea31ecd62dda4a3f646cb4cd60a1",
    "baseline_git_blob": "7564abc2add5a14ea04a0fd36b09068dd2ddae60",
    "reason": "Preexisting installed checkout retained 19 CRLF endings after this fixture acquired -text; JSON content was identical before this task.",
}
ROOT = Path(__file__).resolve().parents[2]
PRIMARY = ROOT.parent / "Regicide"
OUT = Path(__file__).resolve().parent
BEFORE = OUT / "protection-before.json"
EXPECTED_MUTABLE = {
    "src/neural.cpp", "include/regicide/neural.hpp", "src/main.cpp", "src/uci.cpp",
    "tests/neural_tests.cpp", "tests/engine_integration.py", "scripts/build.ps1",
}


def git(root, *args, input=None):
    return subprocess.check_output(["git", "-C", str(root), *args], input=input)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_paths():
    listing = git(ROOT, "ls-tree", "-r", "--full-tree", BASELINE).decode().splitlines()
    result = {}
    for line in listing:
        metadata, name = line.split("\t", 1)
        blob = metadata.split()[2]
        protected_code = name.startswith(("src/", "include/", "scripts/", "tests/", "models/"))
        frozen_data = name.startswith("docs/checks/") and (
            name.endswith(".nn") or name.endswith("/positions.jsonl")
        )
        if (protected_code or frozen_data) and name not in EXPECTED_MUTABLE:
            result[name] = blob
    return dict(sorted(result.items()))


def working_blobs(root, names):
    values = git(root, "hash-object", "--stdin-paths", input=("\n".join(names) + "\n").encode())
    hashes = values.decode().splitlines()
    if len(hashes) != len(names):
        raise RuntimeError("Git did not return one filtered blob hash per protected file")
    return dict(zip(names, hashes))


def state_inventory(root):
    state = root / "build/learning-cycle"
    return {
        path.relative_to(state).as_posix(): sha(path)
        for path in sorted(state.rglob("*")) if path.is_file()
    }


def learning_facts(root):
    state = root / "build/learning-cycle"
    catalog = json.loads((state / "catalog.json").read_text(encoding="utf-8"))
    latest = json.loads((state / "latest.json").read_text(encoding="utf-8"))
    return {
        "attempts_started": catalog["attempts_started"],
        "registered_runs": catalog["runs"],
        "latest_attempt": latest["attempt_index"],
        "latest_run": latest["run"],
        "latest_status": latest["status"],
        "latest_adopted": latest["adopted"],
        "champion_exists": (state / "champion.json").exists(),
        "default_evaluation": "classical" if not (state / "champion.json").exists() else "champion",
    }


def inspect():
    baseline = source_paths()
    names = list(baseline)
    roots = {"research": ROOT, "primary": PRIMARY}
    sources = {}
    states = {}
    repositories = {}
    learning = {}
    for role, root in roots.items():
        blobs = working_blobs(root, names)
        sources[role] = {
            name: {
                "baseline_git_blob": baseline[name],
                "working_git_blob": blobs[name],
                "sha256": sha(root / name),
                "identical_to_baseline": blobs[name] == baseline[name],
            }
            for name in names
        }
        states[role] = state_inventory(root)
        learning[role] = learning_facts(root)
        repositories[role] = {
            "head": git(root, "rev-parse", "HEAD").decode().strip(),
            "branch": git(root, "branch", "--show-current").decode().strip(),
            "status": git(root, "status", "--porcelain").decode().splitlines(),
        }
    return {
        "schema": "regicide.neural-inference-preservation.v1",
        "checked_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "baseline_commit": BASELINE,
        "baseline_binary_sha256": BASELINE_BINARY,
        "intended_source_exclusions": sorted(EXPECTED_MUTABLE),
        "source_scope": "All baseline src/include/scripts/tests/models except intended implementation/version/build/test edits, plus all prior published neural weights and positions.jsonl datasets.",
        "protected_source_files": sources,
        "learning_state": states,
        "learning_facts": learning,
        "repositories": repositories,
        "installed_binary_sha256": sha(PRIMARY / "build/regicide.exe"),
        "frozen_baseline_binary_sha256": sha(ROOT / "build-neural-v3/regicide.exe"),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--capture", action="store_true")
    parser.add_argument("--installed-sha256", default=BASELINE_BINARY)
    parser.add_argument("--primary-head")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    current = inspect()
    checks = {
        "protected_sources_match_baseline": all(
            row["identical_to_baseline"]
            for role in current["protected_source_files"].values() for row in role.values()
        ),
        "both_learning_states_have_105_files": all(len(rows) == 105 for rows in current["learning_state"].values()),
        "research_primary_learning_bytes_identical": current["learning_state"]["research"] == current["learning_state"]["primary"],
        "attempt_4_retained_and_no_champion": all(
            facts["attempts_started"] == 4 and facts["latest_attempt"] == 4
            and facts["latest_status"] == "complete" and facts["latest_adopted"] is False
            and facts["champion_exists"] is False and len(facts["registered_runs"]) == 4
            for facts in current["learning_facts"].values()
        ),
        "frozen_baseline_binary_unchanged": current["frozen_baseline_binary_sha256"] == BASELINE_BINARY,
        "installed_binary_matches_expected": current["installed_binary_sha256"] == args.installed_sha256,
    }
    if args.primary_head:
        checks["primary_head_matches_expected"] = current["repositories"]["primary"]["head"] == args.primary_head
    if args.capture:
        if BEFORE.exists():
            raise SystemExit("Refusing to overwrite the initial preservation snapshot")
        checks["both_heads_start_at_baseline"] = all(
            row["head"] == BASELINE for row in current["repositories"].values()
        )
        output = BEFORE
    else:
        before = json.loads(BEFORE.read_text(encoding="utf-8"))
        checks["all_learning_bytes_unchanged_since_capture"] = current["learning_state"] == before["learning_state"]
        exceptions = []
        sources_unchanged = True
        for role, rows in current["protected_source_files"].items():
            old_rows = before["protected_source_files"][role]
            if rows.keys() != old_rows.keys():
                sources_unchanged = False
                continue
            for name, row in rows.items():
                if row == old_rows[name]:
                    continue
                drift = EXISTING_PRIMARY_EOL_DRIFT
                if (role == "primary" and name == drift["path"]
                    and old_rows[name]["sha256"] == drift["initial_sha256"]
                    and old_rows[name]["working_git_blob"] == drift["initial_working_git_blob"]
                    and row["sha256"] == drift["canonical_sha256"]
                    and row["working_git_blob"] == drift["baseline_git_blob"]
                    and row["identical_to_baseline"]):
                    exceptions.append(drift)
                else:
                    sources_unchanged = False
        current["declared_preexisting_installation_repairs"] = exceptions
        checks["protected_source_bytes_unchanged_except_declared_existing_eol_repair"] = sources_unchanged
        checks["learning_facts_unchanged_since_capture"] = current["learning_facts"] == before["learning_facts"]
        output = args.output or OUT / "protection-after.json"
    current["checks"] = checks
    current["passed"] = all(checks.values())
    output.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": output.name,
        "protected_files_per_checkout": len(current["protected_source_files"]["research"]),
        "state_files_per_checkout": {key: len(value) for key, value in current["learning_state"].items()},
        "checks": checks,
        "passed": current["passed"],
    }, indent=2))
    if not current["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
