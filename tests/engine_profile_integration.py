"""Validate frozen engine profiles using private executable/model snapshots.

This is model loading, option ordering and deterministic arithmetic validation.
It neither changes the installed engine nor measures playing strength or speed.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time

import chess

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from engine_profile import compare_arguments, load_profile, snapshot_profile, verify_profile

PROFILES = ("classical", "residual-v2", "nnue-s32")
POSITIONS = (
    ("initial", chess.STARTING_FEN, ()),
    ("castling", "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1", ()),
    ("en-passant", "7k/8/8/3pP3/8/8/8/6K1 w - d6 0 1", ()),
    ("promotion-capture", "1r5k/P7/8/8/8/8/8/6K1 w - - 0 1", ()),
    ("full-history", chess.STARTING_FEN,
     ("g1f3", "g8f6", "g2g3", "g7g6", "f1g2", "f8g7", "e1g1", "e8g8")),
)
TIMEOUT = 20


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def profile_files(profile):
    bindings = [profile["binary"], *profile["models"].values(), *profile.get("artifacts", [])]
    return {item["path"]: digest(item["path"]) for item in bindings}


def native_source_provenance(profile):
    receipt = next(item for item in profile["artifacts"] if item["role"] == "native-build-receipt")
    record = json.loads(Path(receipt["path"]).read_text(encoding="utf-8-sig"))
    require(record["windows_binary_sha256"] == profile["binary"]["sha256"],
            "Native build receipt refers to a different executable")
    checked = []
    for relative, expected in record["native_source_sha256"].items():
        actual = (ROOT / relative).read_bytes()
        require(hashlib.sha256(actual).hexdigest() == expected, "Native source no longer matches build receipt")
        committed = subprocess.run(["git", "-C", str(ROOT), "show", profile["source"]["commit"] + ":" + relative],
                                   capture_output=True, check=True, timeout=TIMEOUT).stdout
        require(actual.replace(b"\r\n", b"\n") == committed.replace(b"\r\n", b"\n"),
                "Native source differs from declared source commit")
        checked.append(relative)
    return {"commit": profile["source"]["commit"], "native_source_files_verified": checked,
            "receipt_sha256": receipt["sha256"], "line_ending_policy": "Raw build hashes; Git text compared after CRLF normalization"}


class Uci:
    def __init__(self, binary, args, events):
        self.events = events
        self.process = subprocess.Popen([str(binary), *args], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, encoding="utf-8", bufsize=1)
        self.lines, self.errors = queue.Queue(), []
        self.readers = [threading.Thread(target=self.read_stdout, daemon=True),
                        threading.Thread(target=self.read_stderr, daemon=True)]
        for reader in self.readers:
            reader.start()

    def read_stdout(self):
        for line in self.process.stdout:
            line = line.rstrip("\r\n")
            self.events.append({"direction": "stdout", "line": line})
            self.lines.put(line)
        self.lines.put(None)

    def read_stderr(self):
        for line in self.process.stderr:
            self.errors.append(line)
            self.events.append({"direction": "stderr", "line": line.rstrip("\r\n")})

    def __enter__(self):
        self.send("uci")
        advertised = self.until("uciok")
        self.options = set()
        for line in advertised:
            match = re.match(r"option name (.+) type ", line)
            if match:
                self.options.add(match[1])
        return self

    def __exit__(self, *_):
        if self.process.poll() is None:
            self.send("quit")
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                # This PID belongs exclusively to this test's private snapshot.
                self.process.kill()
                self.process.wait(timeout=5)
        self.process.stdin.close()
        for reader in self.readers:
            reader.join(timeout=2)
        self.process.stdout.close()
        self.process.stderr.close()
        require(not self.errors, "Snapshot engine wrote unexpected stderr")

    def send(self, line):
        self.events.append({"direction": "stdin", "line": line})
        self.process.stdin.write(line + "\n")
        self.process.stdin.flush()

    def until(self, marker, prefix=False):
        deadline, output = time.monotonic() + TIMEOUT, []
        while True:
            try:
                line = self.lines.get(timeout=max(.001, deadline - time.monotonic()))
            except queue.Empty as error:
                raise AssertionError(f"Timed out waiting for {marker}") from error
            require(line is not None, "Unexpected snapshot engine EOF")
            output.append(line)
            if line.startswith(marker) if prefix else line == marker:
                return output
            require(time.monotonic() < deadline, "Snapshot engine response timeout")

    def command(self, line, expected_error=None):
        self.send(line)
        self.send("isready")
        output = self.until("readyok")
        messages = [value for value in output if value.startswith("info string ")]
        if expected_error:
            fragments = (expected_error,) if isinstance(expected_error, str) else expected_error
            require(any(any(fragment in value for fragment in fragments) for value in messages),
                    f"Expected native rejection for {line}")
        else:
            require(all(value.startswith("info string neural model checksum ") for value in messages),
                    f"Unexpected native option response: {messages}")
        require(output[-1] == "readyok", "Missing readiness response")
        return output

    def configure(self, profile):
        values = compare_arguments(profile)["options"]
        require(set(values) <= self.options, "Profile requests unadvertised UCI options")
        require("Threads" not in self.options, "Expected documented single-thread implementation")
        # Exercise the comparison adapter's actual ordering, with an explicit
        # disabled starting state so the same transport also supports mode reuse.
        names = list(values)
        for active, binding in (("UseNeuralEvaluation", "EvalFile"), ("UseNNUEEvaluation", "NeuralModelFile")):
            if values[active]:
                require(names.index(binding) < names.index(active), "Comparison adapter activates before loading its model")
        order = [("UseNeuralEvaluation", False), ("UseNNUEEvaluation", False)]
        order += list(values.items())
        replies = []
        for name, value in order:
            text = str(value).lower() if isinstance(value, bool) else str(value)
            replies.extend(self.command(f"setoption name {name} value {text}"))
        if profile["evaluation"] == "nnue":
            expected = profile["source"]["model_provenance"]["checksum"]
            require(f"info string neural model checksum {expected}" in replies,
                    "Native NNUE model checksum differs from frozen profile")
        return order

    def search(self, position, limit="nodes 1000"):
        name, fen, moves = position
        board = chess.Board(fen)
        require(board.is_valid(), "Invalid integration fixture root")
        for move in moves:
            board.push_uci(move)
        self.command("ucinewgame")
        self.command("setoption name Clear Hash")
        self.send("position fen " + fen + (" moves " + " ".join(moves) if moves else ""))
        self.send("go " + limit)
        output = self.until("bestmove ", prefix=True)
        require(not any("search failed" in line for line in output), "Native search failed")
        best = output[-1].split()[1]
        require(chess.Move.from_uci(best) in board.legal_moves, "Snapshot engine chose an illegal move")
        info = [line for line in output if line.startswith("info depth ")]
        require(info, "Missing actual search receipt")
        for line in info:
            match = re.search(r"score (cp|mate) (-?\d+) nodes (\d+)", line)
            require(match, "Malformed native score/node receipt")
            if limit.startswith("nodes "):
                require(int(match[3]) <= int(limit.split()[1]), "Native search exceeded node cap")
            probe = board.copy(stack=True)
            for move in line.split(" pv ", 1)[1].split() if " pv " in line else []:
                probe.push_uci(move)
        self.send("isready")
        require(self.until("readyok") == ["readyok"], "Unexpected post-search output")
        graph = [re.sub(r"\btime \d+", "time <elapsed>", line) for line in output]
        return {"position": name, "initial_fen": fen, "moves": list(moves), "limit": limit,
                "bestmove": best, "output": output, "graph": graph}


def native_model_checks(snapshot, report):
    binary = snapshot["binary"]["path"]
    if snapshot["evaluation"] == "residual":
        reply = subprocess.run([binary, "eval", "--eval-file", snapshot["models"]["EvalFile"]["path"]],
                               capture_output=True, text=True, timeout=TIMEOUT, check=True)
        require(re.fullmatch(r"score_cp: -?\d+\s*", reply.stdout), "Residual C++ loader failed")
        require(not reply.stderr, "Residual loader wrote unexpected stderr")
        report["native_residual_eval"] = reply.stdout.strip()
    if snapshot["evaluation"] == "nnue":
        fens = []
        for _, fen, moves in POSITIONS:
            board = chess.Board(fen)
            for move in moves:
                board.push_uci(move)
            fens.append(board.fen(en_passant="fen"))
        reply = subprocess.run([binary, "eval-batch", "--nnue-model", snapshot["models"]["NeuralModelFile"]["path"]],
                               input="\n".join(fens) + "\n", capture_output=True, text=True,
                               timeout=TIMEOUT, check=True)
        rows = [list(map(int, line.split())) for line in reply.stdout.splitlines()]
        require(len(rows) == len(fens) and all(len(row) == 3 and row[1] == row[2] for row in rows),
                "NNUE native full refresh/incremental mismatch")
        require(not reply.stderr, "NNUE loader wrote unexpected stderr")
        report["native_full_incremental_rows"] = rows


def run(profiles_dir, output=None):
    if output:
        output.mkdir(parents=True, exist_ok=False)
    report = {"schema_version": 1, "started_utc": datetime.now(timezone.utc).isoformat(),
              "scope": "Frozen model loading, UCI ordering and arithmetic/search smoke; no strength or speed claim",
              "shared_binary_modified": False, "profiles": [], "status": "RUNNING"}
    profiles = {name: load_profile(profiles_dir / (name + ".json")) for name in PROFILES}
    report["native_source"] = native_source_provenance(profiles["classical"])
    require(all(value["source"]["commit"] == report["native_source"]["commit"] for value in profiles.values()),
            "Profiles do not share one declared source baseline")
    before = {name: profile_files(value) for name, value in profiles.items()}
    with tempfile.TemporaryDirectory(prefix="regicide-m2-profile-validation-") as temporary:
        snapshots = {name: snapshot_profile(value, Path(temporary) / name) for name, value in profiles.items()}
        for name, profile in profiles.items():
            snapshot = snapshots[name]
            verify_profile(snapshot)
            require(Path(snapshot["binary"]["path"]).is_relative_to(temporary), "Test must only run a private snapshot")
            entry = {"id": profile["id"], "manifest_sha256": digest(profiles_dir / (name + ".json")),
                     "original_files": before[name], "snapshot_files": profile_files(snapshot), "events": []}
            report["profiles"].append(entry)
            native_model_checks(snapshot, entry)
            with Uci(snapshot["binary"]["path"], snapshot["args"], entry["events"]) as engine:
                entry["option_order"] = engine.configure(snapshot)
                engine.search(POSITIONS[0], "depth 1")  # Allocate TT before graph comparisons.
                entry["searches"] = [engine.search(position) for position in POSITIONS]
                baseline = engine.search(POSITIONS[-1], "depth 3")
                if name == "residual-v2":
                    wrong = snapshots["nnue-s32"]["models"]["NeuralModelFile"]["path"]
                    engine.command("setoption name EvalFile value " + wrong, "Unsupported neural model version")
                    require(engine.search(POSITIONS[-1], "depth 3")["graph"] == baseline["graph"],
                            "Wrong-family load changed the active residual evaluator")
                    engine.command("setoption name UseNNUEEvaluation value true", "disable UseNeuralEvaluation")
                    require(engine.search(POSITIONS[-1], "depth 3")["graph"] == baseline["graph"],
                            "Rejected simultaneous NNUE activation changed residual search")
                    entry["wrong_family_and_simultaneous_activation_rejected"] = True
                if name == "nnue-s32":
                    wrong = snapshots["residual-v2"]["models"]["EvalFile"]["path"]
                    engine.command("setoption name NeuralModelFile value " + wrong,
                                   ("invalid neural model magic", "neural model exceeds 256 KiB"))
                    require(engine.search(POSITIONS[-1], "depth 3")["graph"] == baseline["graph"],
                            "Wrong-family load changed the active NNUE evaluator")
                    engine.command("setoption name EvalFile value " + wrong)
                    engine.command("setoption name UseNeuralEvaluation value true", "disable UseNNUEEvaluation")
                    require(engine.search(POSITIONS[-1], "depth 3")["graph"] == baseline["graph"],
                            "Rejected residual activation changed NNUE search")
                    entry["wrong_family_and_simultaneous_activation_rejected"] = True
                    parity = []
                    for position in POSITIONS:
                        engine.command("setoption name UseIncrementalNeuralEvaluation value true")
                        incremental = engine.search(position, "depth 3")
                        engine.command("setoption name UseIncrementalNeuralEvaluation value false")
                        full = engine.search(position, "depth 3")
                        require(incremental["graph"] == full["graph"], "Full/incremental search graphs differ")
                        parity.append({"position": position[0], "graph": incremental["graph"]})
                    entry["full_incremental_search_parity"] = parity
            entry["status"] = "PASS"
            if output:
                (output / (name + "-uci.jsonl")).write_text(
                    "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in entry.pop("events")), encoding="utf-8")
            verify_profile(snapshot)
    require(before == {name: profile_files(value) for name, value in profiles.items()},
            "Original engine/model/source artifacts changed during validation")
    report["original_files_unchanged"] = True
    report["finished_utc"] = datetime.now(timezone.utc).isoformat()
    report["status"] = "PASS"
    if output:
        (output / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profiles-dir", type=Path, default=ROOT / "config/engine-profiles")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run(args.profiles_dir.resolve(), args.output.resolve() if args.output else None)
    print(json.dumps({"status": report["status"], "profiles": len(report["profiles"]),
                      "search_smokes": sum(len(item["searches"]) for item in report["profiles"]),
                      "nnue_search_parity_pairs": 5, "original_files_unchanged": True}, indent=2))


if __name__ == "__main__":
    main()
