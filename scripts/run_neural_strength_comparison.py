"""Run one frozen neural/classical paired block with the existing CPU guard.

The same engine executable is copied to two distinct paths so the unchanged
guard can label each UCI transport by its declared role. The comparison harness
still selects every move, applies the time limit and writes the PGN. This file
only binds experiment inputs, checks option symmetry and preserves guard data.
"""

import argparse
import ctypes
import datetime
import hashlib
import json
from pathlib import Path
import re
import sys
from unittest.mock import patch

import compare_engines as comparison
import see_clean_guard as guarded


ROOT = Path(__file__).resolve().parents[1]
CRITICAL_SOURCES = (
    "scripts/run_neural_strength_comparison.py",
    "scripts/compare_engines.py",
    "scripts/see_clean_guard.py",
)


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def require(condition, message):
    if not condition:
        raise ValueError(message)


def resolve(path):
    candidate = Path(path)
    return candidate.resolve() if candidate.is_absolute() else (ROOT / candidate).resolve()


def write_json(path, document):
    with Path(path).open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(document, ensure_ascii=False, indent=2) + "\n")


def bound_files(freeze):
    files = {}
    for path, expected in freeze["files"].items():
        require(re.fullmatch(r"[0-9a-f]{64}", expected) is not None,
                f"Invalid source fingerprint: {path}")
        files[str(resolve(path))] = expected
    for field, path_key in (("baseline", "binary"), ("candidate", "binary"),
                            ("model", "path"), ("openings", "path"),
                            ("prior_validation", "path")):
        item = freeze[field]
        path = str(resolve(item[path_key]))
        expected = item["sha256"]
        require(re.fullmatch(r"[0-9a-f]{64}", expected) is not None,
                f"Invalid {field} fingerprint")
        require(path not in files or files[path] == expected, "Contradictory frozen fingerprints")
        files[path] = expected
    return files


def validate_freeze(freeze, movetime):
    require(freeze["schema_version"] == 1, "Unsupported experiment freeze version")
    require(re.fullmatch(r"[0-9a-f]{40}", freeze["engine_source_commit"]) is not None,
            "Use a full frozen engine source commit")
    require(freeze["time_controls_ms"] == [100, 1000] and movetime in freeze["time_controls_ms"],
            "The preregistered time conditions are 100 and 1000 milliseconds")
    require(freeze["max_plies"] == 500 and freeze["draw_policy"] == "claim",
            "Changed move-cap or draw policy")
    require(set(CRITICAL_SOURCES) <= set(freeze["files"]), "Freeze all three executed harness sources")
    require(resolve(freeze["baseline"]["binary"]) != resolve(freeze["candidate"]["binary"]),
            "Use two distinct binary copy paths for transport role registration")
    require(freeze["baseline"]["sha256"] == freeze["candidate"]["sha256"],
            "Both roles must run byte-identical engine binaries")
    require(freeze["common_effective_options"].get("Hash") == 16
            and freeze["common_effective_options"].get("Move Overhead") == 20
            and freeze["common_effective_options"].get("UseIncrementalNeuralEvaluation") is True,
            "Changed common memory, time-overhead or incremental evaluation settings")
    expected = {"Hash", "Move Overhead", "UsePositionalEvaluation", "UseQuiescence",
                "UseTranspositionTable", "UseFastMoves", "UseQuietMoveOrdering",
                "UseQuiescenceChecks", "PVS", "UseSEE", "UseIncrementalNeuralEvaluation"}
    require(set(freeze["common_effective_options"]) == expected,
            "Incomplete or unexpected common engine options")
    require(all(freeze["common_effective_options"][name] is True
                for name in expected - {"Hash", "Move Overhead"}), "A search option was disabled")
    bound_files(freeze)


def assert_frozen(freeze_path, freeze, expected_freeze_sha):
    require(digest(freeze_path) == expected_freeze_sha, "The experiment freeze file changed")
    current_head = comparison.git_command("rev-parse", "HEAD")
    require(current_head == freeze["engine_source_commit"], "Engine source HEAD differs from the freeze")
    observed = {}
    for path, expected in bound_files(freeze).items():
        actual = digest(path)
        require(actual == expected, f"Frozen input changed: {path}")
        observed[path] = actual
    validation = read_json(resolve(freeze["prior_validation"]["path"]))
    require(validation.get("all_passed") is True, "The pre-match validation gate did not pass")
    completed = datetime.datetime.fromisoformat(validation["completed_utc"].replace("Z", "+00:00"))
    require(completed.tzinfo is not None and completed <= datetime.datetime.now(datetime.timezone.utc),
            "The validation gate has no completed past UTC timestamp")
    return {"checked_utc": utc_now(), "freeze_sha256": expected_freeze_sha,
            "engine_source_commit": current_head, "files": observed}


class Awake:
    """Keep the system awake temporarily without changing its power policy."""

    def __init__(self):
        self.kernel = None
        self.record = {"requested_state": "ES_CONTINUOUS | ES_SYSTEM_REQUIRED",
                       "display_required": False, "persistent_power_policy_changed": False,
                       "prior_wake_state": None, "restore_requested_state": None,
                       "restore_succeeded": False}

    def start(self):
        require(sys.platform == "win32", "The guarded strength experiment requires Windows")
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.SetThreadExecutionState.argtypes = [ctypes.c_uint]
        self.kernel.SetThreadExecutionState.restype = ctypes.c_uint
        previous = self.kernel.SetThreadExecutionState(0x80000001)
        self.record["prior_wake_state"] = previous
        require(previous != 0, "Temporary system wake request failed")

    def close(self):
        previous = self.record["prior_wake_state"]
        if previous:
            restored = previous | 0x80000000
            self.record["restore_requested_state"] = restored
            self.record["restore_succeeded"] = self.kernel.SetThreadExecutionState(restored) != 0


def check_options(engines, freeze, candidate_options):
    defaults = {}
    for role, engine in engines.items():
        defaults[role] = {name: option.default for name, option in engine.options.items()
                          if option.type != "button"}
        for name, value in freeze["common_effective_options"].items():
            require(name in defaults[role] and type(defaults[role][name]) is type(value)
                    and defaults[role][name] == value, f"Unexpected {role} advertised default: {name}")
        require(defaults[role].get("UseNNUEEvaluation") is False
                and defaults[role].get("NeuralModelFile") == "<builtin>",
                f"Unexpected {role} evaluator default")
    require(set(defaults) == {"baseline", "candidate"} and defaults["baseline"] == defaults["candidate"],
            "Byte-identical binaries advertised different defaults")
    require(dict(engines["baseline"].id) == dict(engines["candidate"].id), "Engine identities differ")
    return {"advertised_defaults": defaults,
            "requested_options": {"baseline": {}, "candidate": candidate_options},
            "effective_options": {"baseline": defaults["baseline"],
                                  "candidate": {**defaults["candidate"], **candidate_options}}}


def run_block(freeze_path, movetime, output):
    freeze_path, output = Path(freeze_path).resolve(), Path(output).resolve()
    require(not output.exists(), f"Preserve the previous output directory: {output}")
    freeze = read_json(freeze_path)
    validate_freeze(freeze, movetime)
    frozen_sha = digest(freeze_path)
    output.mkdir(parents=True)
    stem = output / f"time{movetime}"
    state = {"schema_version": 1, "started_utc": utc_now(), "finished_utc": None,
             "freeze_path": str(freeze_path), "freeze_sha256": frozen_sha,
             "movetime_ms": movetime, "sequential_workloads": True,
             "frozen_before": None, "frozen_after": None, "engine_options_preflight": None,
             "valid_for_comparison": False, "exit_code": 2, "error": None}
    awake, guard, metadata, code = Awake(), None, None, 2
    candidate_options = {"UseNNUEEvaluation": True, "UseIncrementalNeuralEvaluation": True,
                         "NeuralModelFile": str(resolve(freeze["model"]["path"]))}
    try:
        state["frozen_before"] = assert_frozen(freeze_path, freeze, frozen_sha)
        args = comparison.arguments([
            str(resolve(freeze["baseline"]["binary"])), str(resolve(freeze["candidate"]["binary"])),
            "--movetime", str(movetime), "--openings", str(resolve(freeze["openings"]["path"])),
            "--max-plies", "500", "--draw-policy", "claim", "--pgn", str(stem.with_suffix(".pgn")),
            "--baseline-commit", freeze["engine_source_commit"],
            "--candidate-commit", freeze["engine_source_commit"],
            "--candidate-options", json.dumps(candidate_options)])
        require(len(args.selected_openings) == 8, "The frozen block must contain exactly eight starts")
        awake.start()
        guard = guarded.EngineCpuGuard(guarded.WindowsProcesses(),
                                       stem.with_suffix(".monitor.jsonl"), interval=0.5,
                                       abort_file=output / "abort-request.json")
        guard.start()
        guard.require_idle(10.0)
        original_play = comparison.play_one

        def checked_play(*arguments, **keywords):
            if state["engine_options_preflight"] is None:
                state["engine_options_preflight"] = check_options(arguments[1], freeze, candidate_options)
            return original_play(*arguments, **keywords)

        with patch.object(comparison, "play_one", side_effect=checked_play):
            metadata, code = guarded.execute_guarded_match(args, guard)
        if code == 0 and metadata["completed"]:
            guard.assert_transports()
        state["frozen_after"] = assert_frozen(freeze_path, freeze, frozen_sha)
    except BaseException as exc:
        state["error"] = f"{type(exc).__name__}: {exc}"
        print(state["error"], file=sys.stderr, flush=True)
        code = 2
    finally:
        if guard is not None:
            try:
                guard.close()
            except BaseException as exc:
                state["error"] = f"Guard cleanup failed: {type(exc).__name__}: {exc}"
                code = 2
            if guard.summary["contaminated"]:
                code = 2
        try:
            awake.close()
        except BaseException as exc:
            state["error"] = f"Wake restoration failed: {type(exc).__name__}: {exc}"
            code = 2
        state["wake_state"] = awake.record
        if not awake.record["restore_succeeded"]:
            if state["error"] is None:
                state["error"] = "The temporary wake state was not restored"
            code = 2
        complete = bool(metadata is not None and metadata["completed"] and not metadata["errors"])
        valid = bool(code == 0 and complete and state["frozen_after"] is not None
                     and state["engine_options_preflight"] is not None)
        if guard is not None:
            guard.summary.update(valid_for_comparison=valid,
                                 harness_commit=freeze["engine_source_commit"],
                                 candidate_commit=freeze["engine_source_commit"],
                                 baseline_commit=freeze["engine_source_commit"],
                                 movetime_ms=movetime, freeze_manifest_sha256=frozen_sha,
                                 monitor_log_sha256=digest(stem.with_suffix(".monitor.jsonl"))
                                 if stem.with_suffix(".monitor.jsonl").exists() else None,
                                 error=state["error"], exit_code=code)
            write_json(stem.with_suffix(".monitor.json"), guard.summary)
        if metadata is not None:
            metadata.update(external_engine_guard=guard.summary,
                            valid_for_comparison=valid,
                            harness_source_commit=freeze["engine_source_commit"],
                            frozen_before=state["frozen_before"], frozen_after=state["frozen_after"],
                            engine_options_preflight=state["engine_options_preflight"],
                            selected_model=freeze["model"], exit_code=code)
            if guard.summary["contaminated"]:
                metadata["errors"].append("Entire time block invalid: " + guard.summary["failure"]["reason"])
            if state["error"]:
                metadata["errors"].append(state["error"])
            comparison.save_outputs(args, metadata)
        state.update(finished_utc=utc_now(), valid_for_comparison=valid, exit_code=code)
        state["artifact_sha256"] = {path.name: digest(path) for path in output.iterdir()
                                    if path.is_file() and path.name != "driver-state.json"}
        write_json(output / "driver-state.json", state)
    return code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--freeze", type=Path, required=True)
    parser.add_argument("--movetime", type=int, choices=(100, 1000), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        return run_block(args.freeze, args.movetime, args.output)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"FAIL: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    sys.exit(main())
