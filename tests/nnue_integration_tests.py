"""Exercise NNUE opt-in modes through the real CLI and UCI process.

Run with --binary path/to/regicide. Fixtures use exactly representable binary
fractions, so the original and quantized first layers must produce the same
search graph. These checks establish mode correctness, not playing strength.
"""

import argparse
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

import chess

from engine_integration import UciProcess, configure


BINARY = None
INPUTS, HIDDEN1, HIDDEN2 = 768, 32, 16


def write_fixture(path, version):
    """Serialize disk-order matrices without importing the training tools."""
    w1 = [0.0] * (INPUTS * HIDDEN1)
    b1 = [0.0] * HIDDEN1
    w2 = [0.0] * (HIDDEN1 * HIDDEN2)
    b2 = [0.0] * HIDDEN2
    w3 = [0.0] * HIDDEN2
    w1[31 * INPUTS + 10] = 0.5  # Own pawn c2, relative to the side to move.
    w1[31 * INPUTS + 6 * 64 + 50] = -0.25  # Opposing pawn c7.
    w1[64 + 21] = 0.125  # Own knight f3.
    b1[31], b1[0] = 0.125, 0.25
    w2[15 * HIDDEN1 + 31], w2[0] = 2.0, 0.5
    b2[15] = -0.125
    w3[15], w3[0] = 0.5, 0.25
    values = w1 + b1 + w2 + b2 + w3 + [-0.125]
    assert len(values) == 25153
    header = f"REGICIDE_NN_V{version} 768 32 16 600"
    if version == 2:
        header += " 100"
    path.write_text(header + "\n" + " ".join(map(str, values)) + "\n", encoding="ascii")


def corpus():
    boards = [chess.Board(), chess.Board("4k3/8/8/3pP3/8/8/8/4K3 w - d6 0 1"),
              chess.Board("4k3/P7/8/8/8/8/7p/4K3 w - - 0 1")]
    opening = chess.Board()
    for move in ("g1f3", "g8f6", "g2g3", "g7g6", "f1g2", "f8g7", "e1g1"):
        opening.push_uci(move)
    boards.extend((opening, boards[1].mirror()))
    assert all(board.is_valid() for board in boards)
    return boards


def fingerprint(reply):
    """Compare search decisions and work, excluding clock/allocation noise."""
    move, output = reply
    normalized = []
    for line in output:
        if not line.startswith("info "):
            continue
        line = re.sub(r" time \d+", "", line)
        if line.startswith("info string tt "):
            line = re.sub(r" (?:allocations|reuses|clears|bytes) \d+", "", line)
        normalized.append(line)
    return move, tuple(normalized)


def cli(arguments, returncode=0):
    completed = subprocess.run([str(BINARY), *map(str, arguments)], capture_output=True,
                               text=True, encoding="utf-8", timeout=30)
    if completed.returncode != returncode:
        raise AssertionError((completed.args, completed.returncode,
                              completed.stdout, completed.stderr))
    return completed


def static_score(path, board, *options):
    output = cli(["eval", "--eval-file", path, "--fen", board.fen(en_passant="fen"), *options])
    return int(output.stdout.strip().split(": ", 1)[1])


class NnueIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory(prefix="regicide nnue modes ")
        cls.paths = {}
        for version in (1, 2):
            path = Path(cls.folder.name) / f"dyadic V{version} model  with spaces.nn"
            write_fixture(path, version)
            cls.paths[version] = path

    @classmethod
    def tearDownClass(cls):
        cls.folder.cleanup()

    def test_uci_advertises_default_off_and_preferences_do_not_enable_neural(self):
        with UciProcess(BINARY) as engine:
            engine.send("uci")
            output = engine.until(lambda line: line == "uciok")
            self.assertIn("option name UseNNUE type check default false", output)
            self.assertIn("option name UseNeuralEvaluation type check default false", output)
            configure(engine, Hash=1)
            board = corpus()[3]
            baseline = fingerprint(engine.search(board, "go depth 3 nodes 20000"))
            # A mode preference without a model is valid, but remains classical.
            configure(engine, UseNNUE=True)
            self.assertEqual(baseline, fingerprint(engine.search(board, "go depth 3 nodes 20000")))
            configure(engine, UseNNUE=False, EvalFile=self.paths[1])
            self.assertEqual(baseline, fingerprint(engine.search(board, "go depth 3 nodes 20000")))
            configure(engine, UseNNUE=True)
            self.assertEqual(baseline, fingerprint(engine.search(board, "go depth 3 nodes 20000")))

    def test_enabled_modes_have_identical_search_graph_for_exact_fixtures(self):
        for version, path in self.paths.items():
            with UciProcess(BINARY) as engine:
                configure(engine, Hash=1, EvalFile=path, UseNeuralEvaluation=True)
                for board in corpus():
                    with self.subTest(version=version, fen=board.fen()):
                        configure(engine, UseNNUE=False)
                        original = fingerprint(engine.search(board, "go depth 3 nodes 20000"))
                        configure(engine, UseNNUE=True)
                        incremental = fingerprint(engine.search(board, "go depth 3 nodes 20000"))
                        self.assertEqual(original, incremental)

    def test_neural_disable_reenable_and_model_clear_preserve_classical_default(self):
        board = corpus()[3]
        with UciProcess(BINARY) as engine:
            configure(engine, Hash=1)
            baseline = fingerprint(engine.search(board, "go depth 3 nodes 20000"))
            configure(engine, EvalFile=self.paths[1], UseNeuralEvaluation=True, UseNNUE=True)
            enabled = fingerprint(engine.search(board, "go depth 3 nodes 20000"))
            self.assertNotEqual(baseline, enabled)
            configure(engine, UseNeuralEvaluation=False)
            self.assertEqual(baseline, fingerprint(engine.search(board, "go depth 3 nodes 20000")))
            configure(engine, UseNeuralEvaluation=True)
            self.assertEqual(enabled, fingerprint(engine.search(board, "go depth 3 nodes 20000")))
            configure(engine, EvalFile="<empty>")
            self.assertEqual(baseline, fingerprint(engine.search(board, "go depth 3 nodes 20000")))

    def test_invalid_uci_boolean_is_rejected_without_changing_mode(self):
        board = corpus()[1]
        with UciProcess(BINARY) as engine:
            configure(engine, Hash=1, EvalFile=self.paths[1], UseNeuralEvaluation=True, UseNNUE=True)
            expected = fingerprint(engine.search(board, "go depth 3 nodes 20000"))
            for invalid in ("yes", "TRUE", "false extra"):
                engine.send(f"setoption name UseNNUE value {invalid}")
                output = engine.ready()
                self.assertTrue(any(line.startswith("info string ") for line in output), output)
                self.assertEqual(expected, fingerprint(engine.search(board, "go depth 3 nodes 20000")))

    def test_cli_requires_model_and_static_modes_match_exact_fixtures(self):
        for command in (["eval", "--nnue"], ["search", "2", "--nnue"]):
            self.assertIn("--nnue requires --eval-file MODEL", cli(command, returncode=1).stderr)
        for version, path in self.paths.items():
            for board in corpus():
                for options in ((), ("--no-pawn-shield",), ("--no-pawn-structure",),
                                ("--no-pawn-shield", "--no-pawn-structure")):
                    with self.subTest(version=version, fen=board.fen(), options=options):
                        self.assertEqual(static_score(path, board, *options),
                                         static_score(path, board, "--nnue", *options))

    def test_cli_nnue_one_node_fallback_uses_requested_evaluator(self):
        board = corpus()[3]
        for version, path in self.paths.items():
            output = cli(["search", "--nodes", "1", "--eval-file", path, "--nnue",
                          "--fen", board.fen(en_passant="fen")]).stdout
            values = dict(line.split(": ", 1) for line in output.splitlines() if ": " in line)
            with self.subTest(version=version):
                self.assertEqual(int(values["depth"]), 0)
                self.assertEqual(int(values["score_cp"]), static_score(path, board, "--nnue"))
                self.assertIn(chess.Move.from_uci(values["bestmove"]), board.legal_moves)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True, type=Path)
    args, unittest_args = parser.parse_known_args()
    BINARY = args.binary.resolve(strict=True)
    unittest.main(argv=[sys.argv[0], *unittest_args])
