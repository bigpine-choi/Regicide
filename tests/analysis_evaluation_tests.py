"""Streaming White-POV analysis scores from the recommendation search itself.

Usage: python tests/analysis_evaluation_tests.py [path/to/regicide]
Deterministic fake streams cover partial records and cancellation; an optional
real-engine search checks the integrated UCI stream without a second search.
"""

import argparse
import gc
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import chess
import chess.engine

import human_play_tests as baseline
from hint_state_tests import HintFactory, wait_for
from analysis_session import AnalysisSession
from move_hints import MoveHints

REAL_BINARY = None
BLACK_FEN = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1"


def packet(cp, *, pov=chess.WHITE, depth=3, **extra):
    return {"score": chess.engine.PovScore(chess.engine.Cp(cp), pov), "depth": depth, **extra}


class Stream:
    def __init__(self, owner, board):
        self.owner = owner
        self.move = chess.Move.from_uci(owner.move) if owner.move else next(iter(board.legal_moves))
        self.info = {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def __iter__(self):
        for value in self.owner.packets:
            if isinstance(value, BaseException):
                raise value
            self.info.update(value)
            yield dict(value)
        self.owner.waiting.set()
        if self.owner.blocked and not self.owner.release.wait(5):
            raise TimeoutError("test stream was not released")
        for value in self.owner.after_gate:
            self.info.update(value)
            yield dict(value)

    def wait(self):
        self.owner.wait_calls += 1
        if self.owner.wait_error:
            raise self.owner.wait_error
        return chess.engine.BestMove(self.move, None)


class StreamingPeer(baseline.FakeEngine):
    def __init__(self, packets=(), *, move=None, blocked=False, after_gate=(), wait_error=None):
        super().__init__()
        self.packets = list(packets)
        self.after_gate = list(after_gate)
        self.move = move
        self.blocked = blocked
        self.wait_error = wait_error
        self.waiting = threading.Event()
        self.release = threading.Event()
        self.close_event = threading.Event()
        self.close_releases = True
        self.analysis_calls = []
        self.wait_calls = 0

    def analysis(self, board, limit, *, game=None, info=None):
        self.analysis_calls.append((board.copy(stack=True), limit, game, info))
        return Stream(self, board)

    def play(self, *_args, **_kwargs):
        raise AssertionError("analysis recommendation and score must share one streamed search")

    def close(self):
        super().close()
        self.close_event.set()
        if self.close_releases:
            self.release.set()


class AnalysisEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="regicide-analysis-evaluation-")
        self.binary = Path(self.folder.name) / "fake-uci.exe"
        self.binary.write_bytes(b"fake peer; never executed")
        self.factory = HintFactory()
        self.analysis = AnalysisSession(self.binary, "Regicide test", self.factory)
        self.addCleanup(self.folder.cleanup)
        self.addCleanup(self.analysis.close)

    def start(self, peer, fen=chess.STARTING_FEN, ms=100):
        self.factory.peers.append(peer)
        return self.analysis.start(fen, ms, self.analysis.revision)

    def ready(self):
        return wait_for(lambda: self.analysis.snapshot()
                        if self.analysis.snapshot()["hint"]["status"] == "ready" else None)

    def unknown(self, evaluation):
        for field in ("cp", "mate", "depth", "bound", "white_share", "winner", "source_fen"):
            self.assertIsNone(evaluation[field], field)

    def identity(self, state):
        evaluation = state["evaluation"]
        for field in ("game_id", "revision", "fen", "turn"):
            self.assertEqual(evaluation[field], state[field], field)

    def test_editing_snapshot_is_unknown_and_copies_are_immutable(self):
        state = self.analysis.snapshot()
        self.identity(state)
        self.assertEqual(state["evaluation"]["status"], "waiting")
        self.unknown(state["evaluation"])
        state["evaluation"]["cp"] = 9999
        state["hint"]["move"] = "a1a8"
        self.unknown(self.analysis.snapshot()["evaluation"])
        self.assertIsNone(self.analysis.snapshot()["hint"]["move"])

    def test_publishes_before_search_completes_and_uses_one_search_only(self):
        peer = StreamingPeer([packet(125, depth=4)], move="e2e4", blocked=True)
        state = self.start(peer, ms=250)
        self.assertTrue(peer.waiting.wait(2))
        during = self.analysis.snapshot()
        self.identity(during)
        self.assertEqual(during["hint"]["status"], "searching")
        self.assertIsNone(during["hint"]["move"])
        score = during["evaluation"]
        self.assertEqual(score["status"], "searching")
        self.assertEqual(score["cp"], 125)
        self.assertEqual(score["depth"], 4)
        self.assertEqual(score["source"], "search")
        self.assertEqual(score["source_fen"], state["fen"])
        self.assertAlmostEqual(score["white_share"], .5 + .5 * 125 / 525)
        self.assertEqual(len(self.factory.instances), 1)
        self.assertEqual(len(peer.analysis_calls), 1)
        self.assertEqual(peer.analysis_calls[0][1].time, .25)
        self.assertEqual(during["moves"], [])
        peer.release.set()
        done = self.ready()
        self.assertEqual(done["hint"]["move"], "e2e4")
        self.assertEqual(done["evaluation"]["status"], "ready")
        self.assertEqual(done["evaluation"]["cp"], 125)
        self.assertEqual(done["fen"], state["fen"])

    def test_black_pov_scores_and_bounds_are_normalized_to_white(self):
        for info, expected_cp, expected_bound in [
            (packet(140, pov=chess.BLACK, lowerbound=True), -140, "upper"),
            (packet(-60, pov=chess.BLACK, upperbound=True), 60, "lower"),
            (packet(0, pov=chess.BLACK), 0, "exact"),
            (packet(75, pov=chess.WHITE, lowerbound=True), 75, "lower"),
        ]:
            with self.subTest(cp=expected_cp, bound=expected_bound):
                self.start(StreamingPeer([info]), BLACK_FEN)
                done = self.ready()
                self.identity(done)
                self.assertEqual(done["evaluation"]["cp"], expected_cp)
                self.assertEqual(done["evaluation"]["bound"], expected_bound)
                self.assertEqual(done["evaluation"]["turn"], "black")

    def test_mate_sign_and_mate_zero_keep_the_winner(self):
        for raw, pov, expected_mate, winner in [
            (chess.engine.Mate(3), chess.WHITE, 3, "white"),
            (chess.engine.Mate(2), chess.BLACK, -2, "black"),
            (chess.engine.Mate(0), chess.WHITE, 0, "black"),
            (chess.engine.Mate(0), chess.BLACK, 0, "white"),
            (chess.engine.MateGiven, chess.WHITE, 0, "white"),
        ]:
            with self.subTest(raw=raw, pov=pov):
                self.start(StreamingPeer([{"depth": 2, "score": chess.engine.PovScore(raw, pov)}]))
                evaluation = self.ready()["evaluation"]
                self.assertIsNone(evaluation["cp"])
                self.assertEqual(evaluation["mate"], expected_mate)
                self.assertEqual(evaluation["winner"], winner)
                self.assertEqual(evaluation["white_share"], 1 if winner == "white" else 0)

    def test_partial_packets_cannot_relabel_score_with_incomplete_depth(self):
        self.start(StreamingPeer([
            packet(30, depth=1), packet(-75, depth=4),
            {"string": "tt probes 20"}, {"nodes": 1000, "depth": 7},
            {"score": chess.engine.PovScore(chess.engine.Cp(999), chess.WHITE)},
            {"depth": 8, "score": chess.engine.PovScore(chess.engine.Cp(999), chess.WHITE),
             "lowerbound": True, "upperbound": True},
        ]))
        state = self.ready()
        evaluation = state["evaluation"]
        self.assertEqual(evaluation["status"], "ready")
        self.assertEqual(evaluation["cp"], -75)
        self.assertEqual(evaluation["depth"], 4)
        self.assertEqual(state["hint"]["depth"], 4)

    def test_no_score_and_failed_searches_clear_any_streamed_score(self):
        for peer in [StreamingPeer([{"depth": 3, "nodes": 123}]),
                     StreamingPeer([packet(200)], wait_error=RuntimeError("failed final result")),
                     StreamingPeer([packet(200)], move="e2e5")]:
            with self.subTest(peer=peer):
                self.start(peer)
                state = wait_for(lambda: self.analysis.snapshot()
                                 if self.analysis.snapshot()["hint"]["status"] != "searching" else None)
                self.assertEqual(state["evaluation"]["status"], "unavailable")
                self.unknown(state["evaluation"])

    def test_timeout_clears_streamed_score(self):
        peer = StreamingPeer([packet(200)], blocked=True)
        self.analysis._hints._timeout_grace = .01
        self.start(peer)
        self.assertTrue(peer.waiting.wait(2))
        state = wait_for(lambda: self.analysis.snapshot()
                         if self.analysis.snapshot()["hint"]["status"] == "unavailable" else None)
        self.assertEqual(state["evaluation"]["status"], "unavailable")
        self.unknown(state["evaluation"])

    def test_iterator_failure_consumes_completion_without_masking_failure(self):
        peer = StreamingPeer([packet(200), RuntimeError("iterator failed")])
        self.start(peer)
        state = wait_for(lambda: self.analysis.snapshot()
                         if self.analysis.snapshot()["hint"]["status"] == "unavailable" else None)
        self.assertEqual(peer.wait_calls, 1)
        self.assertEqual(state["evaluation"]["status"], "unavailable")
        self.unknown(state["evaluation"])

    def test_ignored_graceful_stop_retains_bounded_force_close(self):
        class IgnoredStopStream(Stream):
            def stop(inner):
                inner.owner.stop_calls += 1
        class IgnoredStopPeer(StreamingPeer):
            stop_calls = 0
            def analysis(inner, board, limit, *, game=None, info=None):
                inner.analysis_calls.append((board.copy(stack=True), limit, game, info))
                return IgnoredStopStream(inner, board)
        peer = IgnoredStopPeer([packet(200)], blocked=True)
        self.start(peer)
        self.assertTrue(peer.waiting.wait(2))
        started = time.monotonic()
        self.analysis.stop(self.analysis.revision)
        self.assertTrue(peer.close_event.wait(1.5))
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertGreaterEqual(peer.stop_calls, 1)
        self.unknown(self.analysis.snapshot()["evaluation"])

    def test_position_change_clears_old_score_and_cancels_late_packet(self):
        old = StreamingPeer([packet(200)], blocked=True, after_gate=[packet(9999, depth=9)])
        old.close_releases = False
        self.start(old)
        self.assertTrue(old.waiting.wait(2))
        self.assertEqual(self.analysis.snapshot()["evaluation"]["cp"], 200)
        latest = StreamingPeer([packet(60, pov=chess.BLACK)], blocked=True)
        self.factory.peers.append(latest)
        state = self.analysis.move("e2e4", self.analysis.revision)
        self.identity(state)
        self.assertEqual(state["evaluation"]["status"], "searching")
        self.unknown(state["evaluation"])
        self.assertTrue(old.close_event.wait(2))
        old.release.set()
        self.assertTrue(latest.waiting.wait(2))
        self.assertEqual(self.analysis.snapshot()["evaluation"]["cp"], -60)
        self.assertEqual(self.analysis.snapshot()["evaluation"]["fen"], state["fen"])
        latest.release.set()
        self.assertEqual(self.ready()["evaluation"]["cp"], -60)

    def test_stop_discards_late_scores_and_resets_every_score_field(self):
        peer = StreamingPeer([packet(200)], blocked=True, after_gate=[packet(9999)])
        self.start(peer)
        self.assertTrue(peer.waiting.wait(2))
        stopped = self.analysis.stop(self.analysis.revision)
        self.identity(stopped)
        self.assertEqual(stopped["evaluation"]["status"], "waiting")
        self.unknown(stopped["evaluation"])
        self.assertTrue(peer.close_event.wait(2))
        wait_for(lambda: self.analysis._hints._cancel is None)
        self.assertEqual(self.analysis.snapshot()["evaluation"], stopped["evaluation"])

    def test_terminal_scores_come_from_position_result_without_search(self):
        for fen, result, winner, share, mate in [
            ("7k/6Q1/5K2/8/8/8/8/8 b - - 0 1", "1-0", "white", 1, 0),
            ("8/8/8/8/8/5k2/6q1/7K w - - 0 1", "0-1", "black", 0, 0),
            ("7k/5Q2/5K2/8/8/8/8/8 b - - 0 1", "1/2-1/2", None, .5, None),
            ("4k3/8/8/8/8/8/8/4K3 w - - 0 1", "1/2-1/2", None, .5, None),
        ]:
            with self.subTest(result=result):
                state = self.analysis.start(fen, 100, self.analysis.revision)
                self.identity(state)
                evaluation = state["evaluation"]
                self.assertEqual(evaluation["status"], "terminal")
                self.assertEqual(evaluation["result"], result)
                self.assertEqual(evaluation["winner"], winner)
                self.assertEqual(evaluation["white_share"], share)
                self.assertEqual(evaluation["mate"], mate)
                self.assertEqual(evaluation["source"], "result")
                self.assertIsNone(evaluation["cp"])
                self.assertEqual(self.factory.instances, [])

    def test_play_only_fallback_uses_final_info_from_same_search(self):
        peer = baseline.FakeEngine()
        peer.replies.append(chess.engine.PlayResult(chess.Move.from_uci("e2e4"), None,
                                                    packet(110, depth=4)))
        self.start(peer)
        evaluation = self.ready()["evaluation"]
        self.assertEqual(evaluation["cp"], 110)
        self.assertEqual(evaluation["status"], "ready")
        self.assertEqual(len(peer.calls), 1)

    def test_normal_ranked_hints_use_exact_packets_without_publishing_evaluation(self):
        from ranked_hint_tests import RankedPeer
        peer = RankedPeer()
        hints = MoveHints(self.binary, lambda _: peer)
        self.addCleanup(hints.close)
        hints.update_position(game_id="normal", revision=1, board=chess.Board(), human_color=chess.WHITE,
                              movetime_ms=100, engine_name="fake", active=True)
        hints.set_enabled(True)
        wait_for(lambda: hints.snapshot()["status"] == "ready")
        self.assertEqual(len(peer.calls), 4)  # Three depth-1 ranks; incomplete depth-2 round.
        self.assertEqual(len(hints.snapshot()["candidates"]), 3)
        self.assertNotIn("evaluation", hints.snapshot())
        self.assertEqual(hints.snapshot_with_evaluation()[1], {})


class RealAnalysisEvaluationTests(unittest.TestCase):
    def test_actual_engine_returns_matching_white_pov_score_and_move(self):
        if REAL_BINARY is None:
            self.skipTest("pass a Regicide binary for real UCI stream")
        analysis = AnalysisSession(REAL_BINARY)
        self.addCleanup(analysis.close)
        for fen in (chess.STARTING_FEN, BLACK_FEN):
            with self.subTest(fen=fen):
                state = analysis.start(fen, 250, analysis.revision)
                done = wait_for(lambda: analysis.snapshot()
                                if analysis.snapshot()["hint"]["status"] == "ready" else None, timeout=8)
                self.assertIn(done["hint"]["move"], state["legal_moves"])
                evaluation = done["evaluation"]
                self.assertEqual(evaluation["status"], "ready")
                self.assertGreaterEqual(evaluation["depth"], 1)
                self.assertTrue(evaluation["cp"] is not None or evaluation["mate"] is not None)
                for field in ("game_id", "revision", "fen", "turn"):
                    self.assertEqual(evaluation[field], done[field])
                self.assertEqual(evaluation["source_fen"], done["fen"])
                self.assertEqual(done["moves"], [])

    def test_actual_stream_cancellation_has_no_unretrieved_future_stderr(self):
        if REAL_BINARY is None:
            self.skipTest("pass a Regicide binary for real cancellation stderr check")
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()),
                                 "--cancellation-probe", str(REAL_BINARY)],
                                capture_output=True, text=True, timeout=30, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("Future exception was never retrieved", result.stderr)
        self.assertNotIn("EngineTerminatedError", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["cancelled_searches"], 4)
        self.assertEqual(report["helper_processes"], 4)


def cancellation_probe(binary):
    peers = []
    def factory(path):
        peer = chess.engine.SimpleEngine.popen_uci([str(path), "uci"], timeout=3)
        peers.append(peer)
        return peer
    analysis = AnalysisSession(binary, engine_factory=factory)
    try:
        for fen in (chess.STARTING_FEN, BLACK_FEN) * 2:
            analysis.start(fen, 3000, analysis.revision)
            wait_for(lambda: analysis.snapshot()["evaluation"]["depth"] is not None, timeout=8)
            analysis.stop(analysis.revision)
            wait_for(lambda: analysis._hints._cancel is None, timeout=3)
            peers[-1].returncode.result(timeout=2)
    finally:
        analysis.close()
    gc.collect()
    time.sleep(.05)
    print(json.dumps({"cancelled_searches": 4, "helper_processes": len(peers)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path, nargs="?")
    parser.add_argument("--cancellation-probe", type=Path)
    args, rest = parser.parse_known_args()
    if args.cancellation_probe:
        cancellation_probe(args.cancellation_probe.resolve(strict=True))
        raise SystemExit(0)
    REAL_BINARY = args.binary.resolve() if args.binary else None
    unittest.main(argv=[str(Path(__file__)), *rest], verbosity=2)
