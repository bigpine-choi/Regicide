"""Cross-language and opt-in neural evaluator integration checks.

Run with --binary path/to/regicide. Scores and rule outcomes are assertions;
timeouts only detect a hung process and do not measure playing strength.
"""

import argparse
import hashlib
import io
import json
from pathlib import Path
import random
import re
import subprocess
import sys
import tempfile
import time
import unittest

import chess
import chess.engine
import chess.pgn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
from neural_model import evaluate_cp, export_model, initialize_model, load_model, material_cp_stm
from play_human import HumanGame
from engine_integration import UciProcess, configure

BINARY = None


def zero_model():
    result = initialize_model(1)
    for value in result.parameters().values():
        value.fill(0)
    return result


def bias_model(bias):
    result = zero_model()
    result.b3[0] = bias
    return result


def corpus():
    """Legal opening, middle game, endgame and promotion feature coverage."""
    boards = [chess.Board(fen) for fen in (
        chess.STARTING_FEN,
        "4k3/8/8/3pP3/8/8/8/4K3 w - d6 0 1",
        "4k3/P7/8/8/8/8/7p/4K3 w - - 0 1",
        "6k1/8/8/8/8/2B5/4N3/6K1 w - - 0 1",
        "8/5pk1/6p1/3P4/8/4K3/8/8 w - - 0 1",
        "8/6k1/8/2r5/8/4K3/1R6/8 b - - 87 54",
        "7k/6Q1/5K2/8/8/8/8/8 b - - 0 1",
        "7k/5Q2/6K1/8/8/8/8/8 b - - 0 1",
    )]
    promoted = boards[2].copy()
    promoted.push_uci("a7a8q")
    boards.append(promoted)
    castled = chess.Board()
    for move in ("g1f3", "g8f6", "g2g3", "g7g6", "f1g2", "f8g7", "e1g1", "e8g8"):
        castled.push_uci(move)
    boards.append(castled)
    rng = random.Random(20261018)
    walk = chess.Board()
    for ply in range(72):
        moves = sorted(walk.legal_moves, key=lambda move: move.uci())
        if not moves or walk.is_game_over():
            break
        walk.push(moves[rng.randrange(len(moves))])
        if ply % 6 == 5:
            boards.append(walk.copy(stack=True))
    # mirror() swaps colors and ranks, so both black-turn and opposing channels
    # are checked against a separate Python implementation of the features.
    result = boards + [board.mirror() for board in boards]
    assert len(result) >= 20 and all(board.is_valid() for board in result)
    return result


def cli(command, *, expected_returncode=0):
    completed = subprocess.run([str(BINARY), *map(str, command)],
                               capture_output=True, text=True, encoding="utf-8", timeout=30)
    if completed.returncode != expected_returncode:
        raise AssertionError((completed.args, completed.returncode, completed.stdout, completed.stderr))
    return completed


def fields(text):
    return dict(line.split(": ", 1) for line in text.splitlines() if ": " in line)


def score(output):
    matches = [re.search(r"\bscore (cp|mate) (-?\d+)\b", line)
               for line in output if line.startswith("info depth ")]
    matches = [match for match in matches if match]
    if not matches:
        raise AssertionError(("missing search score", output))
    return matches[-1].group(1), int(matches[-1].group(2))


def graph_fingerprint(move, output):
    """Keep every progress/statistic line except time and TT allocation work."""
    normalized = []
    for line in output:
        if not line.startswith("info "):
            continue
        line = re.sub(r" time \d+", "", line)
        if line.startswith("info string tt "):
            line = re.sub(r" (?:allocations|reuses|clears|bytes) \d+", "", line)
        normalized.append(line)
    return move, tuple(normalized)


def configured_search(path, enabled, board, *, positional=True):
    with UciProcess(BINARY) as engine:
        configure(engine, Hash=1)
        if path is not None:
            configure(engine, EvalFile=path)
        if enabled:
            configure(engine, UseNeuralEvaluation=True)
        if not positional:
            configure(engine, UsePositionalEvaluation=False)
        return engine.search(board, "go depth 3 nodes 20000")


class NeuralIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory(prefix="regicide neural tests ")
        cls.root = Path(cls.folder.name)
        cls.models = {
            "zero": zero_model(),
            "seed": initialize_model(20261018),
            "positive": bias_model(2),
            "negative": bias_model(-2),
            "extreme_positive": bias_model(100),
            "extreme_negative": bias_model(-100),
        }
        hand = zero_model()
        # Two explicit channels and late hidden rows exercise orientation and
        # output-major matrix order independently of the random seed fixture.
        hand.w1[31, 10] = .5  # own pawn c2
        hand.w1[31, 6 * 64 + 50] = -.25  # opponent pawn c7
        hand.b1[31] = .125
        hand.w2[15, 31] = 2
        hand.b2[15] = -.125
        hand.w3[15] = .5
        hand.b3[0] = -.125
        cls.models["hand"] = hand
        cls.paths = {}
        for name, model in cls.models.items():
            path = cls.root / f"{name} model  with spaces.nn"
            export_model(model, path)
            cls.paths[name] = path
        cls.bad = cls.root / "invalid model.nn"
        cls.bad.write_text("REGICIDE_NN_V1 768 32 16 600\nnot a model\n", encoding="ascii")

    @classmethod
    def tearDownClass(cls):
        cls.folder.cleanup()

    def test_python_cpp_static_parity_across_features_and_game_stages(self):
        boards = corpus()
        for name in ("zero", "seed", "hand"):
            restored = load_model(self.paths[name])
            for index, board in enumerate(boards):
                with self.subTest(model=name, position=index, fen=board.fen()):
                    expected = evaluate_cp(board, restored)
                    actual = int(fields(cli(["eval", "--eval-file", self.paths[name],
                                             "--fen", board.fen(en_passant="fen")]).stdout)["score_cp"])
                    self.assertLessEqual(abs(actual - expected), 1)
        print(f"Python/C++ parity: {len(boards)} positions x 3 models; <= 1 cp", flush=True)

    def test_cli_full_history_and_missing_model_errors(self):
        moves = ["e2e4", "c7c5", "g1f3", "d7d6", "d2d4", "c5d4", "f3d4"]
        board = chess.Board()
        for move in moves:
            board.push_uci(move)
        actual = fields(cli(["eval", "--eval-file", self.paths["seed"], "--play", *moves]).stdout)
        self.assertLessEqual(abs(int(actual["score_cp"]) - evaluate_cp(board, self.models["seed"])), 1)
        for command in (["eval"], ["eval", "--eval-file", self.bad],
                        ["search", "2", "--eval-file", self.root / "missing.nn"]):
            self.assertIn("error:", cli(command, expected_returncode=1).stderr)

    def test_uci_default_and_loaded_but_disabled_preserve_classical_graph(self):
        boards = [chess.Board(), corpus()[5], corpus()[9]]
        for board in boards:
            expected = configured_search(None, False, board)
            loaded_off = configured_search(self.paths["positive"], False, board)
            self.assertEqual(graph_fingerprint(*loaded_off), graph_fingerprint(*expected))
        with UciProcess(BINARY) as engine:
            engine.send("uci")
            output = engine.until(lambda line: line == "uciok")
            self.assertIn("option name UseNeuralEvaluation type check default false", output)
            engine.send("setoption name UseNeuralEvaluation value true")
            self.assertTrue(any("load a valid EvalFile" in line for line in engine.ready()))

    def test_uci_node_limited_fallback_uses_selected_evaluator(self):
        board = chess.Board("4k3/8/8/8/3p4/8/3P4/4K3 w - - 0 1")
        with UciProcess(BINARY) as engine:
            configure(engine, EvalFile=self.paths["positive"], UseNeuralEvaluation=True)
            _, output = engine.search(board, "go nodes 1")
            self.assertEqual(score(output), ("cp", evaluate_cp(board, self.models["positive"])))
            configure(engine, UsePositionalEvaluation=False)
            _, output = engine.search(board, "go nodes 1")
            self.assertEqual(score(output), ("cp", material_cp_stm(board)))

    def test_uci_failed_load_preserves_model_and_clear_disables_it(self):
        board = chess.Board()
        classical = configured_search(None, False, board)
        with UciProcess(BINARY) as engine:
            configure(engine, Hash=1, EvalFile=self.paths["positive"], UseNeuralEvaluation=True)
            original = engine.search(board, "go depth 3 nodes 20000")
            for invalid in (self.bad, self.root / "missing.nn"):
                engine.send(f"setoption name EvalFile value {invalid}")
                self.assertTrue(any(line.startswith("info string ") for line in engine.ready()))
                retained = engine.search(board, "go depth 3 nodes 20000")
                self.assertEqual(graph_fingerprint(*retained), graph_fingerprint(*original))
            configure(engine, EvalFile="<empty>")
            cleared = engine.search(board, "go depth 3 nodes 20000")
            self.assertEqual(graph_fingerprint(*cleared), graph_fingerprint(*classical))
            engine.send("setoption name UseNeuralEvaluation value true")
            self.assertTrue(any("load a valid EvalFile" in line for line in engine.ready()))

    def test_reused_tt_isolated_across_models_options_and_new_game(self):
        board = corpus()[9]
        variants = [("positive", True, True), ("seed", True, True),
                    ("negative", True, True), ("negative", False, True),
                    ("positive", True, False), ("hand", True, True)]
        with UciProcess(BINARY) as engine:
            configure(engine, Hash=1)
            for index, (name, enabled, positional) in enumerate(variants):
                with self.subTest(model=name, enabled=enabled, positional=positional):
                    configure(engine, EvalFile=self.paths[name], UseNeuralEvaluation=enabled,
                              UsePositionalEvaluation=positional)
                    expected = configured_search(self.paths[name], enabled, board, positional=positional)
                    actual = engine.search(board, "go depth 3 nodes 20000")
                    self.assertEqual(graph_fingerprint(*actual), graph_fingerprint(*expected))
                    if index % 2:
                        engine.send("ucinewgame")
                    else:
                        engine.send("setoption name Clear Hash")
                    engine.ready()
                    repeated = engine.search(board, "go depth 3 nodes 20000")
                    self.assertEqual(graph_fingerprint(*repeated), graph_fingerprint(*expected))

    def test_python_chess_client_can_stop_neural_search(self):
        board = chess.Board()
        with chess.engine.SimpleEngine.popen_uci([str(BINARY), "uci"], timeout=10) as engine:
            engine.configure({"EvalFile": str(self.paths["seed"]), "UseNeuralEvaluation": True})
            with engine.analysis(board, chess.engine.Limit(nodes=20000), info=chess.engine.INFO_ALL) as analysis:
                for info in analysis:
                    if info.get("depth", 0) >= 1:
                        break
                analysis.stop()
                result = analysis.wait()
                self.assertIn(result.move, board.legal_moves)
            reply = engine.play(board, chess.engine.Limit(nodes=1), info=chess.engine.INFO_ALL)
            self.assertIn(reply.move, board.legal_moves)
            self.assertEqual(reply.info["score"].pov(board.turn).score(),
                             evaluate_cp(board, self.models["seed"]))

    def test_extreme_scores_cannot_override_mate_stalemate_or_dead_material(self):
        cases = [
            ("7k/6Q1/5K2/8/8/8/8/8 b - - 0 1", "score_mate", 0),
            ("7k/6Q1/5K2/8/8/8/8/8 b - - 150 1", "score_mate", 0),
            ("7k/5Q2/6K1/8/8/8/8/8 b - - 0 1", "score_cp", 0),
            ("4k3/8/8/8/8/8/8/2B1K3 w - - 0 1", "score_cp", 0),
            ("7k/8/8/8/8/8/8/R3K3 w - - 150 1", "score_cp", 0),
        ]
        for name in ("extreme_positive", "extreme_negative"):
            for fen, score_name, expected in cases:
                with self.subTest(model=name, fen=fen):
                    result = fields(cli(["search", "2", "--nodes", "4096", "--eval-file", self.paths[name],
                                         "--fen", fen]).stdout)
                    self.assertEqual(result["bestmove"], "0000")
                    self.assertEqual(int(result[score_name]), expected)
                    self.assertEqual(int(result["nodes"]), 0)
            mate = chess.Board("7k/5K2/6Q1/8/8/8/8/8 w - - 0 1")
            result = fields(cli(["search", "2", "--nodes", "4096", "--eval-file", self.paths[name],
                                 "--fen", mate.fen()]).stdout)
            self.assertEqual(int(result["score_mate"]), 1)
            mate.push_uci(result["bestmove"])
            self.assertTrue(mate.is_checkmate())

    def test_draw_claims_and_full_history_precede_negative_neural_score(self):
        path = self.paths["extreme_negative"]
        fifty = fields(cli(["search", "2", "--nodes", "1", "--eval-file", path,
                            "--fen", "7k/8/8/8/8/8/8/R3K3 w - - 100 1"]).stdout)
        self.assertEqual(int(fifty["score_cp"]), 0)
        cycle = ["g1f3", "g8f6", "f3g1", "f6g8"]
        for repeats, automatic in ((2, False), (4, True)):
            result = fields(cli(["search", "2", "--nodes", "1", "--eval-file", path,
                                 "--play", *(cycle * repeats)]).stdout)
            self.assertEqual(int(result["score_cp"]), 0)
            self.assertEqual(result["bestmove"] == "0000", automatic)
            self.assertEqual(int(result["nodes"]) == 0, automatic)

    def test_human_backend_main_and_hints_share_model_and_saved_identity(self):
        created, configured = [], []

        class RecordingEngine:
            def __init__(self, peer):
                self.peer = peer

            def configure(self, options):
                configured.append(dict(options))
                return self.peer.configure(options)

            def __getattr__(self, name):
                return getattr(self.peer, name)

        def factory(binary):
            peer = chess.engine.SimpleEngine.popen_uci([str(binary), "uci"], timeout=10)
            created.append(peer)
            return RecordingEngine(peer)

        save_dir = self.root / "actual neural human game"
        game = HumanGame(BINARY, save_dir, factory, factory, neural_model=self.paths["seed"])
        try:
            state = game.new_game("white", 100, game.state()["revision"])
            identity = state["engine"]
            self.assertEqual(identity["evaluation"], "neural_experimental")
            self.assertEqual(identity["model_sha256"], hashlib.sha256(self.paths["seed"].read_bytes()).hexdigest())
            self.assertEqual(identity["model_file"], self.paths["seed"].name)
            original_fen = state["fen"]
            game.set_hint_enabled(True)
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline and game.hint()["status"] == "searching":
                time.sleep(.01)
            hint = game.hint()
            self.assertEqual(hint["status"], "ready", hint)
            self.assertIn(chess.Move.from_uci(hint["move"]), chess.Board(original_fen).legal_moves)
            self.assertEqual(game.state()["fen"], original_fen)
            self.assertEqual(len(configured), 2)
            self.assertEqual(len({options["EvalFile"] for options in configured}), 1)
            self.assertTrue(all(options["UseNeuralEvaluation"] is True for options in configured))
            snapshot = Path(configured[0]["EvalFile"])
            self.assertEqual(hashlib.sha256(snapshot.read_bytes()).hexdigest(), identity["model_sha256"])
            game.set_hint_enabled(False)
            state = game.human_move("e2e4", game.state()["revision"])
            state = game.engine_move(state["revision"])
            self.assertIsNone(state["error"])
            self.assertEqual(len(state["moves"]), 2)
            record = json.loads((save_dir / f"{state['game_id']}.json").read_text(encoding="utf-8"))
            self.assertEqual(record["state"]["engine"]["model_sha256"], identity["model_sha256"])
        finally:
            game.close()
            for peer in created:
                peer.close()
                peer.returncode.result(timeout=5)

    def test_human_session_freezes_model_bytes_for_later_hints_and_pgn(self):
        original = self.root / "mutable model  with spaces.nn"
        initial_payload = self.paths["positive"].read_bytes()
        original.write_bytes(initial_payload)
        initial_sha = hashlib.sha256(initial_payload).hexdigest()
        expected_score = evaluate_cp(chess.Board(), self.models["positive"])
        loaded, created = [], []

        class RecordingEngine:
            def __init__(self, peer):
                self.peer = peer

            def configure(self, options):
                payload_sha = hashlib.sha256(Path(options["EvalFile"]).read_bytes()).hexdigest()
                self.peer.configure(options)
                probe = self.peer.play(chess.Board(), chess.engine.Limit(nodes=1),
                                       info=chess.engine.INFO_ALL)
                loaded.append((payload_sha, probe.info["score"].pov(chess.WHITE).score()))

            def __getattr__(self, name):
                return getattr(self.peer, name)

        def factory(binary):
            peer = chess.engine.SimpleEngine.popen_uci([str(binary), "uci"], timeout=10)
            created.append(peer)
            return RecordingEngine(peer)

        game = HumanGame(BINARY, self.root / "frozen model game", factory, factory, neural_model=original)
        try:
            game.new_game("white", 100, game.state()["revision"])
            self.assertEqual(loaded, [(initial_sha, expected_score)])
            # The hint peer is created lazily after this replacement. Reading
            # the original path again would silently load the opposite score.
            original.write_bytes(self.paths["negative"].read_bytes())
            self.assertNotEqual(hashlib.sha256(original.read_bytes()).hexdigest(), initial_sha)
            main_probe = game._engine.play(chess.Board(), chess.engine.Limit(nodes=1),
                                           info=chess.engine.INFO_ALL)
            self.assertEqual(main_probe.info["score"].pov(chess.WHITE).score(), expected_score)
            game.set_hint_enabled(True)
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline and game.hint()["status"] == "searching":
                time.sleep(.01)
            self.assertEqual(game.hint()["status"], "ready", game.hint())
            self.assertEqual(loaded, [(initial_sha, expected_score), (initial_sha, expected_score)])
            self.assertEqual(game.state()["engine"]["model_sha256"], initial_sha)
            exported = chess.pgn.read_game(io.StringIO(game.pgn()))
            self.assertIsNotNone(exported)
            self.assertFalse(exported.errors)
            self.assertEqual(exported.headers["ModelSHA256"], initial_sha)
            self.assertEqual(exported.headers["Evaluation"], "neural_experimental")
            self.assertEqual(exported.headers["ModelFile"], original.name)
            state = game.state()
            saved = json.loads((game.save_dir / f"{state['game_id']}.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["state"]["engine"]["model_sha256"], initial_sha)
        finally:
            game.close()
            for peer in created:
                peer.close()
                peer.returncode.result(timeout=5)

    def test_human_default_stays_classical_and_invalid_model_fails_before_start(self):
        default = HumanGame(BINARY, self.root / "classical human game")
        try:
            self.assertNotIn("evaluation", default.state()["engine"])
            self.assertIsNone(default.neural_model)
        finally:
            default.close()

        def must_not_start(_):
            raise AssertionError("invalid model must fail before starting a UCI peer")

        with self.assertRaises(ValueError):
            HumanGame(BINARY, self.root / "invalid human game", must_not_start, neural_model=self.bad)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True, type=Path)
    args, unittest_args = parser.parse_known_args()
    BINARY = args.binary.resolve(strict=True)
    unittest.main(argv=[sys.argv[0], *unittest_args])
