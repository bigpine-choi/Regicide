"""Deterministic M1 compatibility checks, never a playing-strength experiment.

Run --binary build-m1/regicide.exe; optionally provide --installed-reference
and --research-reference compiled from the pristine source revisions below.
Raw UCI checks run even when reference executables are omitted. Only elapsed
time is excluded from graph comparisons; no node, PV or work counter is hidden.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time


ROOT = Path(__file__).resolve().parents[1]
INSTALLED_REF = "3c3d4b4"
RESEARCH_REF = "0f7783b"
MODEL_PATHS = {
    "residual_v1": ROOT / "models/regicide-small-v1.nn",
    "residual_v2": ROOT / "docs/checks/2026-10-19-neural-cost-quality/run/model/small-v2.nn",
    "integer_v1": ROOT / "docs/checks/2026-10-19-neural-training/first-run/training/model.nnue",
    "integer_v2": ROOT / "docs/checks/2026-10-19-neural-precision/selection/model.nnue",
}
POSITIONS = (
    ("initial", "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", ()),
    ("castling_and_pins", "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1", ()),
    ("en_passant", "7k/8/8/3pP3/8/8/8/6K1 w - d6 0 1", ()),
    ("promotion_capture", "1r5k/P7/8/8/8/8/8/6K1 w - - 0 1", ()),
    ("black_pawn_ending", "6k1/8/5p2/3Pp3/8/4K3/8/8 b - - 7 31", ()),
    ("quiet_knights", "4k3/8/2n2n2/3p4/3P4/2N2N2/8/4K3 w - - 0 1", ()),
    ("castled_history", "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
     ("g1f3", "g8f6", "g2g3", "g7g6", "f1g2", "f8g7", "e1g1", "e8g8")),
    ("repetition_history", "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
     ("g1f3", "g8f6", "f3g1", "f6g8", "g1f3", "g8f6", "f3g1", "f6g8")),
)
TIMEOUT = 90


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def relative(path):
    try:
        return Path(path).resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return Path(path).name


def ensure(condition, message):
    if not condition:
        raise AssertionError(message)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def cli_search(binary, position, flags, depth):
    name, fen, moves = position
    command = [str(binary), "search", str(depth), "--hash", "1", "--fen", fen, *map(str, flags)]
    if moves:
        command.extend(["--play", *moves])
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=TIMEOUT)
    ensure(result.returncode == 0, f"CLI failed ({name}): {result.stderr}\n{result.stdout}")
    fields = dict(line.split(":", 1) for line in result.stdout.splitlines() if ":" in line)
    fields = {key.strip(): value.strip() for key, value in fields.items() if key != "time_ms"}
    ensure(fields.get("depth") == str(depth) and fields.get("stopped") == "0",
           f"Incomplete deterministic search ({name}): {fields}")
    ensure("pv" in fields and "nodes" in fields and "tt_probes" in fields, "Missing CLI graph fields")
    return fields


def reference_sources(binary, revision):
    source = Path(binary).resolve().parent.parent
    commit = subprocess.check_output(["git", "rev-parse", revision], cwd=ROOT, text=True).strip()
    paths = sorted([*source.glob("src/*.cpp"), *source.glob("include/regicide/*.hpp")])
    ensure(len(paths) >= 12, f"Missing pristine reference source beside {relative(binary)}")
    checked = []
    for path in paths:
        name = path.relative_to(source).as_posix()
        expected = subprocess.check_output(["git", "show", f"{commit}:{name}"], cwd=ROOT)
        ensure(path.read_bytes().replace(b"\r\n", b"\n") == expected.replace(b"\r\n", b"\n"),
               f"Reference source differs from declared revision: {name}")
        checked.append({"path": name, "sha256": sha256(path)})
    return {"revision": commit, "source_files_verified_against_git": checked}


class Uci:
    """Real line-oriented pipe transport; no GUI or chess-engine wrapper."""

    def __init__(self, binary):
        self.process = subprocess.Popen([str(binary), "uci"], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, encoding="utf-8", bufsize=1)
        self.lines = queue.Queue()
        self.stderr = []
        self.threads = [threading.Thread(target=self.read_stdout, daemon=True),
                        threading.Thread(target=self.read_stderr, daemon=True)]
        for thread in self.threads:
            thread.start()

    def read_stdout(self):
        for line in self.process.stdout:
            self.lines.put(line.rstrip("\r\n"))
        self.lines.put(None)

    def read_stderr(self):
        self.stderr.extend(self.process.stderr)

    def __enter__(self):
        self.send("uci")
        self.advertised = self.until("uciok")
        self.command("setoption name Hash value 1")
        return self

    def __exit__(self, *_):
        if self.process.poll() is None:
            self.send("quit")
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
        self.process.stdin.close()
        for thread in self.threads:
            thread.join(timeout=2)
        self.process.stdout.close()
        self.process.stderr.close()

    def send(self, line):
        self.process.stdin.write(line + "\n")
        self.process.stdin.flush()

    def until(self, marker, *, prefix=False):
        end = time.monotonic() + TIMEOUT
        lines = []
        while True:
            try:
                line = self.lines.get(timeout=max(0.001, end - time.monotonic()))
            except queue.Empty as error:
                raise AssertionError(f"UCI timeout waiting for {marker}: {lines[-10:]}") from error
            ensure(line is not None, f"Unexpected UCI EOF: {self.stderr}")
            lines.append(line)
            if (line.startswith(marker) if prefix else line == marker):
                return lines
            ensure(time.monotonic() < end, f"UCI timeout waiting for {marker}")

    def command(self, command):
        self.send(command)
        self.send("isready")
        return self.until("readyok")

    def search(self, position, depth):
        _, fen, moves = position
        self.send("position fen " + fen + (" moves " + " ".join(moves) if moves else ""))
        self.send(f"go depth {depth}")
        output = self.until("bestmove ", prefix=True)
        self.send("isready")
        ensure(self.until("readyok") == ["readyok"], "Unexpected output after completed search")
        graph = [re.sub(r"\btime \d+", "time <elapsed>", line) for line in output]
        ensure(any(line.startswith(f"info depth {depth} ") for line in graph), "UCI depth incomplete")
        return graph


def cli_parity(args, report):
    arms = []
    if args.installed_reference:
        report["references"]["installed"] = {"binary": relative(args.installed_reference),
            "sha256": sha256(args.installed_reference),
            **reference_sources(args.installed_reference, INSTALLED_REF)}
        arms.extend(("installed", name, flags, flags) for name, flags in (
            ("classical", []), ("residual_v1", ["--eval-file", MODEL_PATHS["residual_v1"]]),
            ("residual_v2", ["--eval-file", MODEL_PATHS["residual_v2"]])))
    if args.research_reference:
        report["references"]["research"] = {"binary": relative(args.research_reference),
            "sha256": sha256(args.research_reference),
            **reference_sources(args.research_reference, RESEARCH_REF)}
        legacy = ["--no-lmr", "--no-pawn-shield", "--no-pawn-structure"]
        arms.extend(("research", name, before, [*after, *legacy]) for name, before, after in (
            ("legacy_classical", [], []), ("integer_builtin", ["--neural"], ["--nnue"]),
            ("integer_v1", ["--neural", "--neural-model", MODEL_PATHS["integer_v1"]],
             ["--nnue", "--nnue-model", MODEL_PATHS["integer_v1"]]),
            ("integer_v2", ["--neural", "--neural-model", MODEL_PATHS["integer_v2"]],
             ["--nnue", "--nnue-model", MODEL_PATHS["integer_v2"]])))
    for family, arm, before, after in arms:
        reference = args.installed_reference if family == "installed" else args.research_reference
        for position in POSITIONS:
            expected = cli_search(reference, position, before, args.depth)
            actual = cli_search(args.binary, position, after, args.depth)
            common = sorted(set(expected) & set(actual))
            ensure(set(expected) <= set(actual), f"Integrated search omitted fields: {set(expected)-set(actual)}")
            differences = {key: {"reference": expected[key], "integrated": actual[key]}
                           for key in common if expected[key] != actual[key]}
            ensure(not differences, f"{family}/{arm}/{position[0]} parity mismatch: {differences}")
            if family == "research":
                ensure(actual.get("lmr_reductions") == actual.get("lmr_researches") == "0",
                       "Research comparison accidentally enabled LMR")
            report["cli_pairs"].append({"reference_family": family, "arm": arm,
                "position": position[0], "compared_fields": common, "result": expected,
                "candidate_additional_fields": {key: actual[key] for key in actual if key not in expected},
                "pass": True})


def raw_uci_checks(args, report, temp):
    paths = {}
    for name, source in MODEL_PATHS.items():
        target = temp / (name + "  consecutive  spaces" + source.suffix)
        shutil.copyfile(source, target)
        paths[name] = target
    bad_residual = temp / "broken residual.nn"
    bad_residual.write_text("REGICIDE_NN_V2 768 32 16 600 100\n0\n", encoding="ascii")
    bad_integer = temp / "broken integer.nnue"
    text = paths["integer_v2"].read_text(encoding="ascii")
    bad_integer.write_text(re.sub(r"checksum \d+", "checksum 0", text), encoding="ascii")
    position = POSITIONS[5]
    with Uci(args.binary) as engine:
        for name in ("UseNeuralEvaluation", "UseNNUEEvaluation", "NeuralModelFile",
                     "EvalFile", "UseIncrementalNeuralEvaluation"):
            ensure(any(line.startswith("option name " + name + " type ") for line in engine.advertised),
                   f"Missing advertised option {name}")
        # All comparisons below use warmed, reused TT storage so only elapsed
        # time differs, including comparisons after rejected commands.
        engine.search(position, args.depth)
        classical = engine.search(position, args.depth)

        def command(line, *, error=None):
            output = engine.command(line)
            if error:
                expected_errors = (error,) if isinstance(error, str) else error
                ensure(any(fragment in item for fragment in expected_errors for item in output),
                       f"Missing expected rejection: {line}: {output}")
            return output

        def unchanged(name, expected, messages):
            actual = engine.search(position, args.depth)
            ensure(actual == expected, f"UCI {name} changed active model/search graph")
            report["uci_cases"].append({"name": name, "pass": True,
                "messages": [re.sub(re.escape(str(temp)), "<temporary-models>", item) for item in messages],
                "graph_sha256": fingerprint(actual), "bestmove": actual[-1]})

        command(f"setoption name EvalFile value {paths['residual_v2']}")
        command("setoption name UseNeuralEvaluation value true")
        residual = engine.search(position, args.depth)
        unchanged("residual_path_preserves_consecutive_spaces", residual, [])
        output = command(f"setoption name NeuralModelFile value {paths['integer_v2']}")
        expected_checksum = re.search(r"checksum (\d+)", text).group(1)
        ensure(any(item.endswith("checksum " + expected_checksum) for item in output), "Loaded integer checksum differs")
        unchanged("inactive_integer_load_keeps_residual_active", residual, output)
        unchanged("reject_simultaneous_nnue_activation", residual,
                  command("setoption name UseNNUEEvaluation value true", error="disable UseNeuralEvaluation"))
        unchanged("residual_wrong_family_rollback", residual,
                  command(f"setoption name EvalFile value {paths['integer_v2']}", error="Unsupported neural model version"))
        unchanged("residual_corrupt_file_rollback", residual,
                  command(f"setoption name EvalFile value {bad_residual}", error="Missing neural model parameter"))
        command("setoption name UseNeuralEvaluation value false")
        unchanged("classical_after_residual", classical, [])
        command("setoption name UseNNUEEvaluation value true")
        integer = engine.search(position, args.depth)
        unchanged("integer_path_preserves_consecutive_spaces", integer, [])
        unchanged("reject_simultaneous_residual_activation", integer,
                  command("setoption name UseNeuralEvaluation value true", error="disable UseNNUEEvaluation"))
        unchanged("integer_wrong_family_rollback", integer,
                  command(f"setoption name NeuralModelFile value {paths['residual_v1']}",
                          error=("invalid neural model magic", "neural model exceeds 256 KiB")))
        unchanged("integer_corrupt_checksum_rollback", integer,
                  command(f"setoption name NeuralModelFile value {bad_integer}", error="checksum mismatch"))
        command("setoption name UseIncrementalNeuralEvaluation value false")
        unchanged("integer_full_refresh_exact_graph", integer, [])
        command("setoption name UseIncrementalNeuralEvaluation value true")
        unchanged("integer_incremental_restore_exact_graph", integer, [])
        output = command("setoption name NeuralModelFile value <builtin>")
        builtin_checksums = [item.rsplit(" ", 1)[1] for item in output
                             if item.startswith("info string neural model checksum ")]
        ensure(len(builtin_checksums) == 1, "Explicit builtin reset did not report one checksum")
        builtin_checksum = builtin_checksums[0]
        builtin = engine.search(position, args.depth)
        ensure(builtin != integer, "Reset fixture cannot distinguish learned weights from builtin")
        unchanged("explicit_integer_builtin_reset", builtin, output)
        for name, line in (("empty_integer_path_restores_builtin", "setoption name NeuralModelFile value"),
                           ("missing_integer_value_restores_builtin", "setoption name NeuralModelFile")):
            command(f"setoption name NeuralModelFile value {paths['integer_v2']}")
            unchanged(name + "_loaded_before_reset", integer, [])
            output = command(line)
            ensure(any(item.endswith("checksum " + builtin_checksum) for item in output),
                   "Empty/missing integer model reset reported wrong builtin checksum")
            unchanged(name, builtin, output)
        command(f"setoption name NeuralModelFile value {paths['integer_v2']}")
        unchanged("learned_integer_restored_after_builtin_resets", integer, [])
        command("setoption name UseNNUEEvaluation value false")
        command("setoption name UseNeuralEvaluation value true")
        unchanged("stored_residual_weights_survive_integer_cycle", residual, [])
        command("setoption name UseNeuralEvaluation value false")
        unchanged("classical_after_both_model_families", classical, [])
        command("setoption name UseNNUEEvaluation value true")
        command("setoption name EvalFile value <empty>")
        unchanged("clear_inactive_residual_preserves_nnue", integer, [])
        unchanged("empty_residual_cannot_activate_or_mutate_nnue", integer,
                  command("setoption name UseNeuralEvaluation value true", error="load a valid EvalFile"))
        command("setoption name UseNNUEEvaluation value false")
        unchanged("classical_after_clear_and_rejections", classical, [])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--installed-reference", type=Path)
    parser.add_argument("--research-reference", type=Path)
    parser.add_argument("--depth", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    ensure(1 <= args.depth <= 5, "Use fixed validation depths 1..5")
    args.binary = args.binary.resolve()
    report = {"schema": "regicide-m1-engine-parity-v1", "started_at": datetime.now(timezone.utc).isoformat(),
        "scope": "Deterministic compatibility and state rollback only; no playing-strength or timing claim.",
        "test_script": {"path": relative(__file__), "sha256": sha256(__file__)},
        "candidate": {"binary": relative(args.binary), "sha256": sha256(args.binary),
            "source_files": {name: sha256(ROOT / name) for name in
                ("src/main.cpp", "src/uci.cpp", "src/search.cpp", "src/evaluation.cpp", "src/neural.cpp", "src/nnue.cpp",
                 "include/regicide/search.hpp", "include/regicide/nnue.hpp")}},
        "models": {name: {"path": relative(path), "sha256": sha256(path),
            "header": path.read_text(encoding="ascii").splitlines()[0]} for name, path in MODEL_PATHS.items()},
        "depth": args.depth, "hash_mib": 1,
        "excluded_fields": ["elapsed time only (CLI time_ms, UCI time)"],
        "research_candidate_disabled_options": ["UseLMR", "UsePawnShield", "UsePawnStructure"],
        "positions": [{"name": name, "fen": fen, "history": list(moves)} for name, fen, moves in POSITIONS],
        "references": {}, "cli_pairs": [], "uci_cases": []}
    try:
        cli_parity(args, report)
        with tempfile.TemporaryDirectory(prefix="regicide m1 parity ") as directory:
            raw_uci_checks(args, report, Path(directory))
        ensure(sha256(args.binary) == report["candidate"]["sha256"], "Candidate binary changed during validation")
        for name, digest in report["candidate"]["source_files"].items():
            ensure(sha256(ROOT / name) == digest, f"Candidate source changed during validation: {name}")
        for name, path in MODEL_PATHS.items():
            ensure(sha256(path) == report["models"][name]["sha256"], f"Model changed during validation: {name}")
        report["input_bytes_unchanged_at_completion"] = True
        report["pass"] = True
    except Exception as error:
        report["pass"] = False
        report["error"] = str(error)
    report["completed_at"] = datetime.now(timezone.utc).isoformat()
    report["counts"] = {"cli_reference_pairs": len(report["cli_pairs"]),
                        "raw_uci_cases": len(report["uci_cases"]), "position_fixtures": len(POSITIONS)}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"pass": report["pass"], **report["counts"],
                      **({"error": report["error"]} if "error" in report else {})}, ensure_ascii=False))
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
