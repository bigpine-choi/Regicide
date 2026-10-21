"""Exercise a real, small training run and its exported integer model.

Usage: python tests/neural_training_integration.py --engine BUILD/regicide[.exe]
The C++ model test executable must be beside the engine, or supplied explicitly
with --model-tests. No generated dataset or model is left in the source tree.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import chess
import numpy as np


def run(command: list[str], *, input_text: str | None = None, timeout: int = 120) -> str:
    result = subprocess.run(command, input=input_text, text=True, capture_output=True, timeout=timeout)
    assert result.returncode == 0, (command, result.returncode, result.stdout, result.stderr)
    return result.stdout


def read_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def replace_partition(data: Path, name: str, rows: list[dict]) -> None:
    path = data / f"{name}.jsonl"
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
    manifest_path = data / "split_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["partitions"][name]["count"] = len(rows)
    manifest["partitions"][name]["sha256"] = sha256(path)
    manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")


def rejects_dataset(trainer: object, path: Path) -> None:
    try:
        trainer.read_dataset(path)
    except ValueError:
        return
    raise AssertionError(("dataset leakage was accepted", path))


def finite_metrics(value: object) -> None:
    if isinstance(value, dict):
        for item in value.values():
            finite_metrics(item)
    elif isinstance(value, list):
        for item in value:
            finite_metrics(item)
    elif isinstance(value, (int, float)):
        assert math.isfinite(value), ("nonfinite training metric", value)


def special_positions() -> list[str]:
    result: list[str] = []
    fixtures = [
        ("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1", "e1g1"),
        ("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1", "e1c1"),
        ("r3k2r/8/8/8/8/8/8/R3K2R b KQkq - 0 1", "e8g8"),
        ("r3k2r/8/8/8/8/8/8/R3K2R b KQkq - 0 1", "e8c8"),
        ("7k/8/8/3pP3/8/8/8/6K1 w - d6 0 1", "e5d6"),
        ("7k/8/8/8/3pP3/8/8/6K1 b - e3 0 1", "d4e3"),
        (chess.STARTING_FEN, "e2e4"),
    ]
    for promotion in "qrbn":
        fixtures.extend([
            ("7k/P7/8/8/8/8/8/6K1 w - - 0 1", "a7a8" + promotion),
            ("1r5k/P7/8/8/8/8/8/6K1 w - - 0 1", "a7b8" + promotion),
            ("7k/8/8/8/8/8/p7/6K1 b - - 0 1", "a2a1" + promotion),
            ("7k/8/8/8/8/8/p7/1R4K1 b - - 0 1", "a2b1" + promotion),
        ])
    for fen, text in fixtures:
        board = chess.Board(fen)
        assert board.is_valid() and chess.Move.from_uci(text) in board.legal_moves
        result.append(board.fen(en_passant="fen"))
        board.push_uci(text)
        result.append(board.fen(en_passant="fen"))
        board.pop()
        assert board.fen(en_passant="fen") == fen, ("fixture undo", fen)
    return result


class Uci:
    def __init__(self, engine: Path):
        self.process = subprocess.Popen([str(engine), "uci"], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        text=True, bufsize=1)
        self.lines: queue.Queue[str | None] = queue.Queue()

        def reader() -> None:
            assert self.process.stdout
            for line in self.process.stdout:
                self.lines.put(line.rstrip("\r\n"))
            self.lines.put(None)

        threading.Thread(target=reader, daemon=True).start()
        self.send("uci")
        self.handshake = self.until("uciok")

    def send(self, text: str) -> None:
        assert self.process.stdin
        self.process.stdin.write(text + "\n")
        self.process.stdin.flush()

    def until(self, prefix: str, timeout: int = 30) -> list[str]:
        rows: list[str] = []
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            assert remaining > 0, ("UCI timeout", prefix, rows)
            try:
                line = self.lines.get(timeout=remaining)
            except queue.Empty as error:
                raise AssertionError(("UCI timeout", prefix, rows)) from error
            assert line is not None, ("UCI closed unexpectedly", prefix, rows)
            rows.append(line)
            if line.startswith(prefix):
                return rows

    def ready(self) -> list[str]:
        self.send("isready")
        return self.until("readyok")

    def option(self, name: str, value: str) -> list[str]:
        self.send(f"setoption name {name} value {value}")
        return self.ready()

    def search(self, fen: str) -> dict:
        self.send("position fen " + fen)
        self.send("go depth 2")
        lines = self.until("bestmove ")
        infos = [line for line in lines if line.startswith("info depth 2 ")]
        assert infos, lines
        fields = infos[-1].split()
        score_start = fields.index("score") + 1
        return {"bestmove": lines[-1].split()[1], "score": fields[score_start:score_start + 2],
                "nodes": int(fields[fields.index("nodes") + 1]),
                "seldepth": int(fields[fields.index("seldepth") + 1]),
                "pv": fields[fields.index("pv") + 1:]}

    def close(self) -> None:
        if self.process.poll() is None:
            self.send("quit")
            self.process.wait(timeout=10)
        assert self.process.returncode == 0


def test_uci(engine: Path, model: Path, corrupt: Path, fens: list[str]) -> int:
    uci = Uci(engine)
    pairs = 0
    try:
        assert any("option name NeuralModelFile type string" in line for line in uci.handshake)
        uci.option("Hash", "1")
        uci.option("UseNNUEEvaluation", "true")
        uci.option("NeuralModelFile", str(model))
        baseline = {}
        for fen in fens:
            uci.option("UseIncrementalNeuralEvaluation", "true")
            inc = uci.search(fen)
            uci.option("UseIncrementalNeuralEvaluation", "false")
            assert uci.search(fen) == inc, ("loaded UCI full/incremental graph", fen)
            baseline[fen] = inc
            pairs += 1
        failed = uci.option("NeuralModelFile", str(corrupt))
        assert any("info string" in line and "neural model" in line and "mismatch" in line for line in failed), failed
        assert uci.search(fens[0]) == baseline[fens[0]], "failed model load replaced prior model"
        uci.send("ucinewgame")
        uci.ready()
        assert uci.search(fens[0]) == baseline[fens[0]], "newgame lost selected loaded model"
        # A replacement while the worker is active must stop and join that
        # worker before releasing its model. The following readiness and search
        # check exposes hangs, dangling pointers, or an incomplete replacement.
        uci.send("position startpos")
        uci.send("go infinite")
        uci.send("setoption name NeuralModelFile value <builtin>")
        switched = uci.ready()
        assert any(line.startswith("bestmove ") for line in switched), switched
        builtin = uci.search(fens[0])
        uci.option("UseIncrementalNeuralEvaluation", "true")
        assert uci.search(fens[0]) == builtin, "builtin restore full/incremental graph"
        uci.option("NeuralModelFile", str(model))
        assert uci.search(fens[0]) == baseline[fens[0]], "loaded model restore changed result"
        uci.option("NeuralModelFile", "")
        assert uci.search(fens[0]) == builtin, "empty model path did not restore builtin"
        return pairs + 1
    finally:
        uci.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", required=True, type=Path)
    parser.add_argument("--training-script", type=Path,
                        default=Path(__file__).resolve().parents[1] / "scripts" / "neural_training.py")
    parser.add_argument("--model-tests", type=Path)
    args = parser.parse_args()
    engine = args.engine.resolve()
    training_script = args.training_script.resolve()
    model_tests = args.model_tests or engine.with_name("regicide_neural_model_tests" + engine.suffix)
    assert model_tests.is_file(), ("build the C++ model test executable", model_tests)
    spec = importlib.util.spec_from_file_location("regicide_neural_training", training_script)
    assert spec and spec.loader
    trainer = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = trainer
    spec.loader.exec_module(trainer)
    with tempfile.TemporaryDirectory(prefix="regicide-training-integration-") as temporary:
        root = Path(temporary)
        data, trained, repeated = root / "data", root / "trained", root / "repeated"
        run([sys.executable, str(training_script), "generate", "--engine", str(engine),
             "--output", str(data), "--games", "8", "--max-plies", "24", "--sample-every", "2",
             "--seed", "20261019"])
        manifest = json.loads((data / "split_manifest.json").read_text(encoding="utf-8"))
        rows = {name: read_rows(data / ("train.jsonl" if name == "train" else "validation.jsonl"))
                for name in ("train", "validation")}
        assert all(rows.values()), "empty training or validation partition"
        groups = {name: {row["group_id"] for row in records} for name, records in rows.items()}
        features = {name: {trainer.feature_identity(chess.Board(row["fen"])) for row in records}
                    for name, records in rows.items()}
        assert not groups["train"] & groups["validation"], "same source game in both partitions"
        assert not features["train"] & features["validation"], "same mirrored input in both partitions"
        assert manifest["feature_overlap"] == 0
        for name, records in rows.items():
            assert manifest["partitions"][name]["count"] == len(records)
            filename = "train.jsonl" if name == "train" else "validation.jsonl"
            assert manifest["partitions"][name]["sha256"] == sha256(data / filename)
            for row in records:
                board = chess.Board(row["fen"])
                assert board.is_valid() and not board.is_game_over(claim_draw=False)
                assert manifest["groups"][row["group_id"]] == name
                assert row["feature_hash"] == trainer.feature_identity(board)
                assert trainer.feature_identity(board.mirror()) == row["feature_hash"]
                expected_white = row["classical_stm_cp"] * (1 if board.turn else -1)
                assert row["target_white_cp"] == expected_white, "label point of view changed"
        # Recompute hashes after adversarial edits: these checks must enforce
        # partition semantics, rather than rejecting only a changed file hash.
        crossed = root / "crossed-game"
        shutil.copytree(data, crossed)
        changed_rows = [dict(row) for row in rows["validation"]]
        changed_rows[0]["group_id"] = rows["train"][0]["group_id"]
        replace_partition(crossed, "validation", changed_rows)
        rejects_dataset(trainer, crossed)
        leaked = root / "leaked-input"
        shutil.copytree(data, leaked)
        changed_rows = [dict(row) for row in rows["validation"]]
        duplicate = dict(rows["train"][0])
        mirror = chess.Board(duplicate["fen"]).mirror()
        duplicate["fen"] = mirror.fen(en_passant="fen")
        duplicate["target_white_cp"] *= -1
        # Swapping colors and side-to-move preserves the relative label.
        duplicate["group_id"] = rows["validation"][0]["group_id"]
        changed_rows[0] = duplicate
        replace_partition(leaked, "validation", changed_rows)
        rejects_dataset(trainer, leaked)
        commands = [sys.executable, str(training_script), "train", "--data", str(data),
                    "--epochs", "8", "--batch-size", "16", "--seed", "20261020"]
        run([*commands, "--output", str(trained)])
        run([*commands, "--output", str(repeated)])
        model_path = trained / "model.nnue"
        assert model_path.read_bytes() == (repeated / "model.nnue").read_bytes(), "training is not seeded deterministic"
        # Alter only held-out targets while preserving dataset validity. An
        # export selected at a fixed epoch must remain byte-for-byte identical.
        changed_validation = root / "changed-validation"
        shutil.copytree(data, changed_validation)
        changed_rows = [dict(row) for row in rows["validation"]]
        for row in changed_rows:
            row["target_white_cp"] += 137
            row["classical_stm_cp"] = row["target_white_cp"] * (1 if chess.Board(row["fen"]).turn else -1)
        replace_partition(changed_validation, "validation", changed_rows)
        independent = root / "validation-independent"
        trainer.train_model(changed_validation, independent, epochs=8, batch_size=16, seed=20261020)
        assert model_path.read_bytes() == (independent / "model.nnue").read_bytes(), "held-out targets affected exported model"
        metrics = json.loads((trained / "metrics.json").read_text(encoding="utf-8"))
        finite_metrics(metrics)
        assert metrics["training_rows"] == len(rows["train"])
        assert metrics["validation_rows"] == len(rows["validation"])
        changed_parameters = sum(metrics["changed_integer_parameters"].values())
        assert changed_parameters > 0, "training did not alter exported integer weights"
        model = trainer.load_model(model_path)
        reloaded_path = root / "roundtrip.nnue"
        trainer.save_model(model, reloaded_path)
        reloaded = trainer.load_model(reloaded_path)
        for key in ("input_weights", "hidden_bias", "output_weights", "output_scale"):
            assert np.array_equal(model[key], reloaded[key]), ("Python model round trip", key)
        original = model_path.read_bytes()
        try:
            trainer.save_model(model, model_path)
        except (ValueError, FileExistsError):
            pass
        else:
            raise AssertionError("Python save overwrote an existing model")
        assert model_path.read_bytes() == original
        tokens = model_path.read_text(encoding="ascii").split()
        tokens[-1] = str(int(tokens[-1]) ^ 1)
        corrupt = root / "corrupt.nnue"
        corrupt.write_text(" ".join(tokens) + "\n", encoding="ascii")
        try:
            trainer.load_model(corrupt)
        except ValueError:
            pass
        else:
            raise AssertionError("Python loader accepted a corrupt checksum")
        rejected = subprocess.run([str(engine), "eval-batch", "--nnue-model", str(corrupt)],
                                  input=chess.STARTING_FEN + "\n", text=True, capture_output=True, timeout=10)
        assert rejected.returncode != 0, "C++ loader accepted a corrupt checksum"
        fens = [row["fen"] for records in rows.values() for row in records] + special_positions()
        triples = trainer.evaluate_engine(engine, fens, model_path)
        assert len(triples) == len(fens)
        for fen, scores in zip(fens, triples, strict=True):
            board = chess.Board(fen)
            expected = trainer.quantized_white(board, model) * (1 if board.turn else -1)
            assert scores[1] == scores[2] == expected, ("Python/full/incremental integer mismatch", fen, scores, expected)
        # This executable checks raw (unclipped) sums after actual moves, every
        # special move type, sibling search branches, and complete undo stacks.
        cpp_output = run([str(model_tests.resolve()), str(model_path)])
        assert "0 failures" in cpp_output, cpp_output
        uci_fens = [chess.STARTING_FEN,
                    "7k/8/8/3pP3/8/8/8/6K1 w - d6 0 1",
                    "1r5k/P7/8/8/8/8/8/6K1 w - - 0 1"]
        uci_pairs = test_uci(engine, model_path, corrupt, uci_fens)
        print(json.dumps({"train_rows": len(rows["train"]), "validation_rows": len(rows["validation"]),
                          "group_overlap": 0, "mirrored_feature_overlap": 0,
                          "python_full_incremental_positions": len(fens), "uci_parity_pairs": uci_pairs,
                          "trained_model_cpp_tests": cpp_output.splitlines(),
                          "changed_integer_parameters": changed_parameters,
                          "deterministic_export": True, "validation_target_independence": True,
                          "adversarial_split_rejections": 2}, indent=2))


if __name__ == "__main__":
    main()
