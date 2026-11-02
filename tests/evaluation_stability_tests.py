"""Search consistency metadata and human-turn streaming generation isolation.

Usage: python tests/evaluation_stability_tests.py
These synthetic UCI packets test evidence boundaries, not engine accuracy.
"""

import copy
from pathlib import Path
import sys
import threading
import time
import unittest

import chess
import chess.engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from evaluation_stability import assess
from move_hints import MoveHints


def packet(depth, cp=20, *, move="e2e4", pov=chess.WHITE, **extra):
    return {"depth": depth, "score": chess.engine.PovScore(chess.engine.Cp(cp), pov),
            "pv": [chess.Move.from_uci(move)], **extra}


def steady(**kwargs):
    return {depth: packet(depth, **kwargs) for depth in (4, 5, 6)}


class EvaluationStabilityTests(unittest.TestCase):
    def setUp(self):
        self.board = chess.Board()

    def test_two_or_three_consecutive_exact_iterations_are_required(self):
        for packets, count in [({}, 0), ({3: packet(3)}, 0), ({4: packet(4)}, 1),
                               ({3: packet(3), 4: packet(4)}, 1),
                               ({4: packet(4), 6: packet(6)}, 1),
                               ({4: packet(4), 6: packet(6), 7: packet(7)}, 2)]:
            with self.subTest(depths=list(packets)):
                value = assess(self.board, packets)
                self.assertEqual(value["reliability"], "provisional")
                self.assertEqual(value["stable_depths"], count)
        for packets, count in [({4: packet(4), 5: packet(5)}, 2),
                               ({3: packet(3), 4: packet(4), 5: packet(5)}, 2), (steady(), 3)]:
            with self.subTest(depths=list(packets)):
                value = assess(self.board, packets)
                self.assertEqual(value["reliability"], "stable")
                self.assertEqual(value["stable_depths"], count)

    def test_only_shallow_warmup_can_be_excluded_not_bad_eligible_samples(self):
        self.assertEqual(assess(self.board, {3: packet(3, 900, pv=[], lowerbound=True),
                                           4: packet(4), 5: packet(5)})["reliability"], "stable")
        for bad in ({"depth": 5}, packet(5, pv=[])):
            value = assess(self.board, {4: packet(4), 5: bad, 6: packet(6)})
            self.assertEqual(value["reliability"], "provisional")
        value = assess(self.board, {4: packet(4), 5: packet(5, lowerbound=True), 6: packet(6)})
        self.assertEqual(value["reliability"], "provisional")

    def test_third_disagreeing_sample_cannot_fall_back_to_agreeing_pair(self):
        packets = {4: packet(4), 5: packet(5)}
        self.assertEqual(assess(self.board, packets)["reliability"], "stable")
        packets[6] = packet(6, 101)
        self.assertEqual(assess(self.board, packets)["reliability"], "volatile")
        packets = {4: packet(4, 101), 5: packet(5), 6: packet(6)}
        self.assertEqual(assess(self.board, packets)["reliability"], "volatile")
        self.assertEqual(assess(self.board, {4: packet(4), 5: packet(5, 101)})["reliability"], "volatile")

    def test_cp_range_is_white_perspective_and_threshold_is_inclusive(self):
        packets = {4: packet(4, -20), 5: packet(5, 60), 6: packet(6, 10)}
        result = assess(self.board, packets)
        self.assertEqual(result, {"reliability": "stable", "recent_cp_range_white": [-20, 60],
                                  "score_swing_cp": 80, "stable_depths": 3})
        packets[6] = packet(6, -21)
        self.assertEqual(assess(self.board, packets)["reliability"], "volatile")
        self.board.push_uci("e2e4")
        black = {4: packet(4, 60, pov=chess.BLACK, move="e7e5"),
                 5: packet(5, 90, pov=chess.BLACK, move="e7e5"),
                 6: packet(6, 70, pov=chess.BLACK, move="e7e5")}
        self.assertEqual(assess(self.board, black)["recent_cp_range_white"], [-90, -60])
        self.assertEqual(assess(self.board, black)["reliability"], "stable")

    def test_root_move_changes_are_volatile_even_at_equal_scores(self):
        packets = steady()
        packets[5] = packet(5, move="d2d4")
        self.assertEqual(assess(self.board, packets)["reliability"], "volatile")

    def test_newest_incomplete_or_corrupted_packet_cannot_reuse_old_stability(self):
        bad_packets = [{"depth": 7}, packet(7, pv=[]), packet(7, pv=[chess.Move.from_uci("e2e5")]),
                       packet(7, pv=[chess.Move.from_uci("e2e4"), chess.Move.from_uci("e2e3")]),
                       packet(7, pov=chess.BLACK), packet(7, multipv=2),
                       packet(6), packet(7, score=90), packet(7, cp=float("nan"))]
        for bad in bad_packets:
            with self.subTest(packet=bad):
                self.assertEqual(assess(self.board, {**steady(), 7: bad})["reliability"], "provisional")

    def test_bound_is_never_an_exact_range(self):
        for flag in ({"lowerbound": True}, {"upperbound": True},
                     {"lowerbound": True, "upperbound": True}):
            packets = {**steady(), 7: packet(7, **flag)}
            result = assess(self.board, packets)
            self.assertEqual(result["reliability"], "bounded")
            self.assertIsNone(result["recent_cp_range_white"])
            self.assertIsNone(result["score_swing_cp"])
        packets[7] = packet(7)
        self.assertEqual(assess(self.board, packets)["reliability"], "stable")

    def test_old_bound_does_not_relabel_current_exact_score(self):
        for flag in ({"lowerbound": True}, {"upperbound": True}):
            with self.subTest(flag=flag):
                packets = {4: packet(4, **flag), 5: packet(5), 6: packet(6)}
                value = assess(self.board, packets)
                self.assertEqual(value["reliability"], "provisional")
                self.assertEqual(value["stable_depths"], 2)
                self.assertIsNone(value["recent_cp_range_white"])
                packets[7] = packet(7)
                self.assertEqual(assess(self.board, packets)["reliability"], "stable")
                packets[7] = packet(7, **flag)
                self.assertEqual(assess(self.board, packets)["reliability"], "bounded")

    def test_duplicate_depth_and_wrong_key_cannot_inflate_evidence(self):
        self.assertEqual(assess(self.board, {4: packet(4), 5: packet(5)})["stable_depths"], 2)
        for malformed in [{"4": packet(4)}, {True: packet(1)}, {0: packet(0)}, {6: packet(7)}]:
            self.assertEqual(assess(self.board, malformed)["reliability"], "provisional")

    def test_old_outliers_expire_and_inputs_are_not_mutated(self):
        packets = {3: packet(3, 2000, move="d2d4"), **steady()}
        snapshot, fen = copy.deepcopy(packets), self.board.fen()
        result = assess(self.board, dict(reversed(list(packets.items()))))
        self.assertEqual(result["reliability"], "stable")
        self.assertEqual(packets, snapshot)
        self.assertEqual(self.board.fen(), fen)
        self.assertEqual(self.board.move_stack, [])

    def test_mates_require_legal_matching_terminal_line_and_never_fake_cp(self):
        for move in ("f2f3", "e7e5", "g2g4"):
            self.board.push_uci(move)
        packets = {depth: packet(depth, move="d8h4", pov=chess.BLACK,
                    score=chess.engine.PovScore(chess.engine.Mate(1), chess.BLACK))
                   for depth in (4, 5, 6)}
        result = assess(self.board, packets)
        self.assertEqual(result["reliability"], "stable")
        self.assertIsNone(result["recent_cp_range_white"])
        self.assertIsNone(result["score_swing_cp"])
        for bad in [packet(6, move="d8h4", pov=chess.BLACK),
                    packet(6, move="d8h4", pov=chess.BLACK,
                           score=chess.engine.PovScore(chess.engine.Mate(-1), chess.BLACK)),
                    packet(6, move="b8c6", pov=chess.BLACK,
                           score=chess.engine.PovScore(chess.engine.Mate(1), chess.BLACK))]:
            packets[6] = bad
            self.assertEqual(assess(self.board, packets)["reliability"], "provisional")

    def test_transition_between_cp_and_verified_mate_is_volatile(self):
        for move in ("f2f3", "e7e5", "g2g4"):
            self.board.push_uci(move)
        packets = {4: packet(4, move="b8c6", pov=chess.BLACK),
                   5: packet(5, move="d8h4", pov=chess.BLACK,
                             score=chess.engine.PovScore(chess.engine.Mate(1), chess.BLACK)),
                   6: packet(6, move="d8h4", pov=chess.BLACK,
                             score=chess.engine.PovScore(chess.engine.Mate(1), chess.BLACK))}
        result = assess(self.board, packets)
        self.assertEqual(result["reliability"], "volatile")
        self.assertIsNone(result["score_swing_cp"])


class EvaluationStabilityStreamTests(unittest.TestCase):
    def setUp(self):
        self.service = MoveHints(Path("unused-test-engine"), stream_evaluation=True)
        self.addCleanup(self.service.close)
        self.board = chess.Board()
        self.prepare()

    def prepare(self):
        # Exercise the real position reset without starting a worker; publish
        # events below explicitly control old/new generation races.
        with self.service._condition:
            self.service._enabled = False
        self.service.update_position(game_id="game", revision=len(self.board.move_stack),
                                     board=self.board, human_color=self.board.turn,
                                     movetime_ms=1000, engine_name="test", active=True)
        with self.service._condition:
            self.service._enabled = True
        self.context, self.generation = self.service._context, self.service._generation
        self.cancel, self.timeout = threading.Event(), threading.Event()

    def publish(self, info):
        self.service._publish_evaluation(self.generation, self.context, self.cancel, self.timeout, info)

    def value(self):
        return self.service.snapshot_with_evaluation()[1]

    def test_completed_packets_only_and_range_is_detached(self):
        for info in steady().values():
            self.publish(info)
        self.assertEqual(self.value()["reliability"], "stable")
        self.publish({"depth": 9, "nodes": 300})
        self.publish({"score": chess.engine.PovScore(chess.engine.Cp(900), chess.WHITE)})
        self.assertEqual(self.value()["depth"], 6)
        self.assertEqual(self.value()["reliability"], "stable")
        self.value()["recent_cp_range_white"][0] = 999
        self.assertEqual(self.value()["recent_cp_range_white"], [20, 20])

    def test_new_generation_clears_stability_and_rejects_late_old_packet(self):
        for info in steady().values():
            self.publish(info)
        old = (self.generation, self.context, self.cancel, self.timeout)
        self.board.push_uci("e2e4")
        self.prepare()
        self.assertIsNone(self.value()["cp"])
        self.assertEqual(self.value()["stable_depths"], 0)
        self.service._publish_evaluation(*old, packet(7, 800))
        self.assertIsNone(self.value()["cp"])
        self.publish(packet(4, 35, move="e7e5", pov=chess.BLACK))
        self.assertEqual(self.value()["cp"], -35)
        self.assertEqual(self.value()["reliability"], "provisional")
        self.assertEqual(self.value()["stable_depths"], 1)

    def test_bound_or_missing_pv_cannot_inherit_a_stable_label(self):
        for info in steady().values():
            self.publish(info)
        self.publish(packet(6, lowerbound=True))
        self.assertEqual(self.value()["reliability"], "bounded")
        self.publish(packet(6))
        self.assertEqual(self.value()["reliability"], "stable")
        bad = packet(7)
        del bad["pv"]
        self.publish(bad)
        self.assertEqual(self.value()["reliability"], "provisional")
        self.assertEqual(self.value()["depth"], 7)

    def test_stream_warmup_then_two_stable_depths_then_disagreement(self):
        self.publish(packet(3, 900, move="d2d4"))
        self.publish(packet(4))
        self.assertEqual(self.value()["reliability"], "provisional")
        self.publish(packet(5))
        self.assertEqual(self.value()["reliability"], "stable")
        self.assertEqual(self.value()["stable_depths"], 2)
        self.publish(packet(6, move="d2d4"))
        self.assertEqual(self.value()["reliability"], "volatile")

    def test_cancelled_or_expired_packets_cannot_enter_evidence(self):
        for event in (self.cancel, self.timeout):
            event.set()
            self.publish(packet(6, 500))
            self.assertEqual(self.service._evaluation_packets, {})
            self.assertIsNone(self.value()["cp"])
            event.clear()

    def test_actual_worker_keeps_one_search_and_publishes_stability(self):
        class Stream:
            def __init__(self):
                self.info = {}
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                pass
            def __iter__(self):
                for info in steady().values():
                    self.info.update(info)
                    yield info
            def wait(self):
                return chess.engine.BestMove(chess.Move.from_uci("e2e4"), None)

        class Peer:
            calls = 0
            def analysis(self, *_args, **_kwargs):
                self.calls += 1
                return Stream()
            def close(self):
                pass

        peer = Peer()
        service = MoveHints(Path("unused-test-engine"), lambda _: peer, stream_evaluation=True)
        self.addCleanup(service.close)
        service.update_position(game_id="worker", revision=0, board=self.board,
                                human_color=chess.WHITE, movetime_ms=1000, engine_name="test", active=True)
        service.set_enabled(True)
        deadline = time.monotonic() + 2
        while service.snapshot()["status"] != "ready" and time.monotonic() < deadline:
            time.sleep(.005)
        hint, evaluation = service.snapshot_with_evaluation()
        self.assertEqual(hint["status"], "ready")
        self.assertEqual(evaluation["reliability"], "stable")
        self.assertEqual(evaluation["depth"], 6)
        self.assertEqual(peer.calls, 1)


if __name__ == "__main__":
    unittest.main()
