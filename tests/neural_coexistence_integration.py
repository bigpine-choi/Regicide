"""Exercise both preserved learning/model paths against one integrated engine.

This is a wiring and arithmetic check using tiny fixtures, not a strength test.
"""
import os

for _name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ[_name] = "1"

import argparse
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import chess
import chess.engine
import numpy as np
import neural_model as residual
import train_neural as residual_training
import neural_training as nnue
import neural_qat as qat
import neural_precision as precision
from neural_training_tests import fixture as residual_fixture
from neural_search_data.engine import EngineTeacher, LEGACY_EXTENSION_OPTIONS
from neural_training_integration import special_positions
import time


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    binary = args.binary.resolve()
    report = {"scope": "two model families, training exports and native arithmetic; no strength claim",
              "environment": {"python": sys.version.split()[0], "numpy": np.__version__,
                              "chess": chess.__version__}}
    with tempfile.TemporaryDirectory(prefix="regicide-m1-learning-") as temporary:
        root = Path(temporary)
        # The residual trainer retains its completed-game schema and V1/V2 files.
        fixture_dir = root / "residual-data"
        fixture_dir.mkdir()
        residual_fixture(fixture_dir)
        residual_paths = []
        for version in (1, 2):
            output = root / f"residual-v{version}"
            with contextlib.redirect_stdout(io.StringIO()):
                result = residual_training.main([
                    "--dataset", str(fixture_dir / "datasetmanifest.json"),
                    "--output", str(output), "--model-version", str(version),
                    "--epochs", "2", "--batch-size", "2", "--seed", "1021"])
            assert result == 0
            model_path = output / f"small-v{version}.nn"
            model = residual.load_model(model_path)
            for fen in [chess.STARTING_FEN, *special_positions()]:
                reply = subprocess.check_output([
                    str(binary), "eval", "--eval-file", str(model_path), "--fen", fen], text=True)
                assert int(reply.split(":", 1)[1]) == residual.evaluate_cp(fen, model)
            residual_paths.append(model_path)
        report["residual_versions_trained_and_loaded"] = [1, 2]

        # Actual tiny NNUE training plus both QAT export formats.
        data = root / "nnue-data"
        nnue.generate_dataset(binary, data, games=8, max_plies=24,
                              sample_every=2, seed=102121)
        trained = root / "nnue-trained"
        nnue.train_model(data, trained, epochs=2, batch_size=16, seed=102122)
        _, rows = nnue.read_dataset(data)
        indices, targets = nnue.pack_records(rows["train"])
        config = {"candidate_id": "m1-fixture", "epochs": 2, "batch_size": 16,
                  "input_bias_lr": .002, "output_lr": .005, "seed": 102123}
        floats, _ = qat.train_candidate(nnue.bootstrap_float(), indices, targets, config)
        qat_path = root / "qat-v1.nnue"
        nnue.save_model(nnue.quantize(floats), qat_path)
        floats, _ = precision.train_candidate(nnue.bootstrap_float(), indices, targets,
                                              {**config, "input_scale": 32})
        precision_path = root / "qat-v2.nnue"
        precision.save_model(precision.quantize(floats, 32), precision_path)
        fens = [row["fen"] for split in rows.values() for row in split] + special_positions()
        models = [(trained / "model.nnue", nnue), (qat_path, nnue),
                  (precision_path, precision)]
        for path, module in models:
            model = module.load_model(path)
            values = nnue.evaluate_engine(binary, fens, path)
            for fen, (_, full, incremental) in zip(fens, values, strict=True):
                board = chess.Board(fen)
                expected = module.quantized_white(board, model) * (1 if board.turn else -1)
                assert full == incremental == expected
        report["nnue_exports"] = ["float training v1", "QAT v1", "QAT S32 v2"]
        report["positions_per_nnue_export"] = len(fens)

        # A version number or file extension must never select the other parser.
        for command in ([str(binary), "eval", "--eval-file", str(precision_path)],
                        [str(binary), "eval-batch", "--nnue-model", str(residual_paths[1])]):
            rejected = subprocess.run(command, input=chess.STARTING_FEN + "\n",
                                      capture_output=True, text=True, timeout=10)
            assert rejected.returncode != 0, "wrong model family was accepted"
        report["cross_family_native_loads_rejected"] = 2

        # A single UCI process can switch the two independently loaded families.
        with chess.engine.SimpleEngine.popen_uci([str(binary), "uci"]) as engine:
            required = {"EvalFile", "NeuralModelFile", "UseNeuralEvaluation", "UseNNUEEvaluation"}
            assert required <= set(engine.options)
            engine.configure({"EvalFile": str(residual_paths[1]),
                              "UseNeuralEvaluation": True, "UseNNUEEvaluation": False})
            board = chess.Board()
            before = engine.analyse(board, chess.engine.Limit(depth=2), game=object())
            engine.configure({"NeuralModelFile": str(precision_path),
                              "UseNeuralEvaluation": False, "UseNNUEEvaluation": True})
            neural = engine.analyse(board, chess.engine.Limit(depth=2), game=object())
            engine.configure({"UseNNUEEvaluation": False, "UseNeuralEvaluation": True})
            after = engine.analyse(board, chess.engine.Limit(depth=2), game=object())
            assert before["score"] == after["score"] and before["pv"] == after["pv"]
            assert neural["pv"][0] in board.legal_moves
        with EngineTeacher(binary, root / "teacher-uci.jsonl", 1000,
                           time.monotonic() + 20) as teacher:
            teacher.query(chess.Board(), "m1-legacy-profile")
            assert all(teacher.options.get(name) is value
                       for name, value in LEGACY_EXTENSION_OPTIONS.items())
            report["teacher_options"] = teacher.options
        report["status"] = "PASS"
    text = json.dumps(report, indent=2) + "\n"
    if args.output:
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(text)
    print(text)


if __name__ == "__main__":
    main()
