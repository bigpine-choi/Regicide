"""Development-only FEN matches fail closed and preserve real chess endings."""

import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import chess
import chess.engine
import chess.pgn
import endgame_matches as MATCH
from collect_learning_data import OPTIONS
from learning_arena import GuardedEngine


FEN = "k7/8/1QK5/8/8/8/8/8 w - - 0 1"


class Guard:
    instances = []
    dirty = False

    def __init__(self, provider, output, interval):
        self.summary = {"contaminated": False, "scope": "mocked guard for orchestration test"}
        self.path, self.registered, self.closed = output, [], False
        self.__class__.instances.append(self)

    def start(self):
        self.path.write_text('{"mocked":true}\n')

    def register_transport(self, pid, role, argv):
        self.registered.append((pid, role, argv))

    def assert_transports(self):
        if len(self.registered) != 2 or len({row[0] for row in self.registered}) != 2:
            raise RuntimeError("expected two transports")

    def sample(self, phase):
        if self.dirty and phase == "after-game":
            self.summary["contaminated"] = True

    def assert_clean(self):
        if self.summary["contaminated"]:
            raise RuntimeError("synthetic external CPU contamination")

    def close(self):
        self.closed = True


class Engine:
    count = 0
    move = "b6b7"

    def __init__(self):
        type(self).count += 1
        self.transport = SimpleNamespace(get_pid=lambda: 100 + type(self).count)
        # Capture this instance's PID rather than the later shared counter.
        pid = 100 + type(self).count
        self.transport = SimpleNamespace(get_pid=lambda: pid)
        self.options = {name: object() for name in (*OPTIONS, "EvalFile", "UseNeuralEvaluation")}
        self.id = {"name": "mock development engine"}
        self.configurations = []
        self.search_limits = []
        self.closed = self.quit_called = False

    def configure(self, options):
        self.configurations.append(dict(options))

    def ping(self):
        return None

    def play(self, board, limit, **kwargs):
        self.search_limits.append(limit)
        move = chess.Move.from_uci(self.move)
        if move not in board.legal_moves:
            move = next(iter(board.legal_moves))
        return chess.engine.PlayResult(move, None, info={"nodes": 10, "depth": 2,
            "time": .001, "pv": [move], "score": chess.engine.PovScore(chess.engine.Cp(20), board.turn)})

    def quit(self):
        self.quit_called = True

    def close(self):
        self.closed = True


class DevelopmentMatchTests(unittest.TestCase):
    def setUp(self):
        Guard.instances.clear()
        Guard.dirty = False
        Engine.count = 0
        Engine.move = "b6b7"

    def setup_files(self, temporary, *, positions=None):
        root = Path(temporary)
        binary, model, openings = root / "regicide.exe", root / "model.nn", root / "openings.json"
        binary.write_bytes(b"fixture binary")
        model.write_bytes(b"fixture model")
        openings.write_text(json.dumps({"schema": MATCH.OPENINGS_SCHEMA, "positions": positions or [
            {"name": "mate-in-one", "fen": FEN, "moves": []}]}))
        return binary, model, openings, root / "match"

    def run_mock(self, files, *, validation=None, **kwargs):
        engines = []
        def factory(*args, **keys):
            engine = Engine()
            engines.append(engine)
            return engine
        with patch.object(MATCH, "WindowsProcesses", return_value=object()), \
                patch.object(MATCH, "LinuxProcesses", return_value=object()), \
                patch.object(MATCH, "EngineCpuGuard", Guard), \
                patch.object(MATCH, "validate_native_model", side_effect=validation or (lambda *_: {"score_cp": 0})), \
                patch.object(chess.engine.SimpleEngine, "popen_uci", side_effect=factory), \
                contextlib.redirect_stdout(io.StringIO()):
            return MATCH.run_matches(*files, **kwargs), engines

    def test_two_real_rule_games_color_swap_and_no_adoption_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            files = self.setup_files(temporary)
            report, engines = self.run_mock(files)
            self.assertEqual(report["status"], "complete")
            self.assertEqual(report["summary"]["candidate_wins"], 1)
            self.assertEqual(report["summary"]["candidate_losses"], 1)
            self.assertEqual(report["summary"]["pair_scores"], [.5])
            self.assertNotIn("decision", report)
            self.assertNotIn("attempt_index", report)
            self.assertNotIn("promotion_passed", report)
            self.assertEqual(len(engines), 2)
            self.assertTrue(all(engine.closed and engine.quit_called for engine in engines))
            self.assertTrue(Guard.instances[0].closed)
            self.assertEqual(len(Guard.instances[0].registered), 2)
            self.assertEqual(report["engines"]["baseline"]["options"]["UseNeuralEvaluation"], False)
            self.assertEqual(report["engines"]["candidate"]["options"]["UseNeuralEvaluation"], True)
            for key in OPTIONS:
                self.assertEqual(report["engines"]["candidate"]["options"][key],
                                 report["engines"]["baseline"]["options"][key])
            self.assertEqual(engines[1].configurations[0], {"EvalFile": str((files[-1] / "candidate.nn").resolve())})
            self.assertEqual(MATCH.verify_records(report), report["summary"])

    def test_pgn_retains_custom_fen_and_full_legal_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            files = self.setup_files(temporary)
            report, _ = self.run_mock(files)
            stream = io.StringIO((files[-1] / "games.pgn").read_text())
            for record in report["games"]:
                game = chess.pgn.read_game(stream)
                self.assertFalse(game.errors)
                self.assertEqual(game.board().fen(en_passant="fen"), FEN)
                self.assertEqual(game.headers["SetUp"], "1")
                self.assertEqual([move.uci() for move in game.mainline_moves()], record["uci_moves"])
                board = game.end().board()
                self.assertTrue(board.is_checkmate())
                self.assertEqual(game.headers["Result"], board.result(claim_draw=True))
            self.assertIsNone(chess.pgn.read_game(stream))

    def test_invalid_native_model_starts_no_guard_or_engine(self):
        with tempfile.TemporaryDirectory() as temporary:
            files = self.setup_files(temporary)
            def fail(*_):
                raise ValueError("native parser rejected model")
            report, engines = self.run_mock(files, validation=fail)
            self.assertEqual(report["status"], "failed")
            self.assertIsNone(report["summary"])
            self.assertFalse(Guard.instances)
            self.assertFalse(engines)
            self.assertIn("native parser rejected", report["errors"][0])

    def test_ply_cap_is_unfinished_and_cannot_publish_partial_wdl(self):
        with tempfile.TemporaryDirectory() as temporary:
            files = self.setup_files(temporary)
            Engine.move = "b6b8"
            report, _ = self.run_mock(files, max_plies=1)
            self.assertEqual(report["status"], "failed")
            self.assertIsNone(report["summary"])
            self.assertEqual(report["games"][0]["result"], "*")
            self.assertEqual(report["games"][0]["termination"], "ply_cap")
            self.assertFalse(report["games"][0]["completed"])
            self.assertIsNone(report["games"][0]["candidate_points"])

    def test_dirty_guard_invalidates_even_completed_chess_game(self):
        with tempfile.TemporaryDirectory() as temporary:
            files = self.setup_files(temporary)
            Guard.dirty = True
            report, _ = self.run_mock(files)
            self.assertEqual(report["status"], "failed")
            self.assertIsNone(report["summary"])
            self.assertTrue(report["games"][0]["completed"])
            self.assertTrue(report["resource_guard"]["contaminated"])
            self.assertFalse(report["resource_guard"]["valid_for_comparison"])

    def test_guarded_round_trip_rejects_response_overshoot(self):
        engine = Engine()
        guard = SimpleNamespace(assert_clean=lambda: None)
        wrapped = GuardedEngine(engine, guard, 100)
        with patch("learning_arena.time.monotonic", side_effect=[0, .200001]):
            with self.assertRaisesRegex(TimeoutError, "response exceeded"):
                wrapped.play(chess.Board(FEN), chess.engine.Limit(time=.1))

    def test_illegal_or_terminal_input_history_and_pair_limit_rejected(self):
        bad = [
            [{"name": "invalid", "fen": "8/8/8/8/8/8/8/8 w - - 0 1", "moves": []}],
            [{"name": "illegal", "fen": FEN, "moves": ["e2e4"]}],
            [{"name": "terminal", "fen": FEN, "moves": ["b6b7"]}],
            [{"name": "duplicate", "fen": FEN, "moves": []}] * 2,
            [{"name": str(index), "fen": FEN, "moves": []} for index in range(17)]]
        for positions in bad:
            with self.subTest(positions=positions), tempfile.TemporaryDirectory() as temporary:
                files = self.setup_files(temporary, positions=positions)
                with self.assertRaises(ValueError):
                    MATCH.load_openings(files[2])

    def test_declared_history_is_replayed_from_custom_fen(self):
        with tempfile.TemporaryDirectory() as temporary:
            fen = "k7/8/2K5/1Q6/8/8/8/8 w - - 0 1"
            files = self.setup_files(temporary, positions=[{"name": "prefix", "fen": fen,
                                                          "moves": ["b5c5", "a8b8"]}])
            opening = MATCH.load_openings(files[2])["openings"][0]
            board = chess.Board(fen)
            board.push_uci("b5c5")
            board.push_uci("a8b8")
            self.assertEqual(opening["fen_after_opening"], board.fen(en_passant="fen"))
            self.assertEqual(opening["fen"], fen)
            self.assertEqual(opening["moves"], ["b5c5", "a8b8"])

    def test_independent_replay_rejects_tampered_role_timing_rules_and_points(self):
        with tempfile.TemporaryDirectory() as temporary:
            report, _ = self.run_mock(self.setup_files(temporary))
            for field, value in (("result", "0-1"), ("candidate_points", 0),
                                 ("termination", "stalemate"), ("final_fen", FEN),
                                 ("initial_fen", chess.STARTING_FEN)):
                item = copy.deepcopy(report)
                item["games"][0][field] = value
                with self.subTest(field=field), self.assertRaises(ValueError):
                    MATCH.verify_records(item)
            for field, value in (("elapsed_seconds", .201), ("elapsed_seconds", float("nan")),
                                 ("role", "baseline"), ("fen_before", chess.STARTING_FEN),
                                 ("move", "b6b8")):
                item = copy.deepcopy(report)
                item["games"][0]["search_moves"][0][field] = value
                with self.subTest(field=field), self.assertRaises(ValueError):
                    MATCH.verify_records(item)

    def test_frozen_input_change_invalidates_entire_match(self):
        with tempfile.TemporaryDirectory() as temporary:
            files = self.setup_files(temporary)
            original = MATCH.comparison.play_one
            def mutate(*args):
                result = original(*args)
                files[1].write_bytes(b"changed model")
                return result
            with patch.object(MATCH.comparison, "play_one", side_effect=mutate):
                report, _ = self.run_mock(files)
            self.assertEqual(report["status"], "failed")
            self.assertIsNone(report["summary"])
            self.assertIn("frozen input changed", report["errors"][0])

    def test_existing_output_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            files = self.setup_files(temporary)
            files[-1].mkdir()
            marker = files[-1] / "user-file"
            marker.write_text("keep")
            with self.assertRaises(FileExistsError):
                MATCH.run_matches(*files)
            self.assertEqual(marker.read_text(), "keep")

    def test_fixed_nodes_uses_same_bounded_work_with_no_guard_or_time_budget(self):
        with tempfile.TemporaryDirectory() as temporary:
            files = self.setup_files(temporary)
            # A browser workload cannot contaminate a mode which makes no
            # timing comparison; no clean-host claim is manufactured instead.
            Guard.dirty = True
            report, engines = self.run_mock(files, nodes=1000)
            self.assertEqual(report["status"], "complete")
            self.assertTrue(report["completed"] and report["valid_for_comparison"])
            self.assertEqual(report["resource_mode"], "fixed_nodes")
            self.assertEqual(report["nodes_per_move"], 1000)
            self.assertIsNone(report["movetime_ms"])
            self.assertIsNone(report["move_response_allowance_seconds"])
            self.assertIsNone(report["resource_samples_sha256"])
            self.assertEqual(report["resource_guard"]["required"], False)
            self.assertEqual(report["resource_guard"]["performed"], False)
            self.assertFalse(Guard.instances)
            self.assertEqual(len(engines), 2)
            self.assertTrue(all(engine.search_limits for engine in engines))
            for engine in engines:
                for limit in engine.search_limits:
                    self.assertEqual(limit.nodes, 1000)
                    self.assertIsNone(limit.time)
                    self.assertIsNone(limit.depth)
            self.assertIn("no equal-time strength inference", report["summary"]["scope"])
            self.assertEqual(MATCH.verify_records(report), report["summary"])
            pgn = chess.pgn.read_game(io.StringIO((files[-1] / "games.pgn").read_text()))
            self.assertEqual(pgn.headers["RequestedNodes"], "1000")
            self.assertNotIn("RequestedMoveTimeMS", pgn.headers)

    def test_fixed_node_verifier_rejects_missing_or_overshot_node_counters(self):
        with tempfile.TemporaryDirectory() as temporary:
            report, _ = self.run_mock(self.setup_files(temporary), nodes=1000)
            for invalid in (None, 0, -1, True, 1.5, 1001):
                item = copy.deepcopy(report)
                item["games"][0]["search_moves"][0]["nodes"] = invalid
                with self.subTest(nodes=invalid), self.assertRaisesRegex(ValueError, "node count"):
                    MATCH.verify_records(item)
            item = copy.deepcopy(report)
            item["games"][0]["search_moves"][0]["elapsed_seconds"] = 3.0
            self.assertEqual(MATCH.verify_records(item), report["summary"])

    def test_fixed_node_wrapper_rejects_changed_limits_and_engine_overshoot(self):
        wrapper = MATCH.FixedNodeEngine(Engine(), 1000)
        for limit in (chess.engine.Limit(nodes=999), chess.engine.Limit(nodes=1000, time=.1),
                      chess.engine.Limit(nodes=1000, depth=3)):
            with self.subTest(limit=limit), self.assertRaisesRegex(ValueError, "limits changed"):
                wrapper.play(chess.Board(FEN), limit)
        bad = SimpleNamespace(play=lambda *_args, **_kwargs: SimpleNamespace(info={"nodes": 1001}))
        wrapper = MATCH.FixedNodeEngine(bad, 1000)
        with self.assertRaisesRegex(ValueError, "bounded node counter"):
            wrapper.play(chess.Board(FEN), chess.engine.Limit(nodes=1000))

    def test_fixed_node_ply_cap_still_cannot_become_draw_or_valid_comparison(self):
        with tempfile.TemporaryDirectory() as temporary:
            Engine.move = "b6b8"
            report, _ = self.run_mock(self.setup_files(temporary), nodes=1000, max_plies=1)
            self.assertEqual(report["status"], "failed")
            self.assertFalse(report["valid_for_comparison"])
            self.assertIsNone(report["summary"])
            self.assertEqual(report["games"][0]["result"], "*")
            self.assertEqual(report["games"][0]["termination"], "ply_cap")

    def test_fixed_node_invalid_budget_rejected_before_outputs(self):
        for invalid in (0, -1, True, 1.5):
            with self.subTest(nodes=invalid), tempfile.TemporaryDirectory() as temporary:
                files = self.setup_files(temporary)
                with self.assertRaisesRegex(ValueError, "node budget"):
                    MATCH.run_matches(*files, nodes=invalid)
                self.assertFalse(files[-1].exists())


if __name__ == "__main__":
    unittest.main()
