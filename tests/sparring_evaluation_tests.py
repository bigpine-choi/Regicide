"""Exact score evidence and adaptive helper timing; synthetic UCI, not Elo."""

import copy
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

import chess
import chess.engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import sparring_evaluation as precision
from move_hints import MoveHints


def packet(depth=8, cp=20, move="e2e4", pov=chess.WHITE, **extra):
    return {"depth": depth, "score": chess.engine.PovScore(chess.engine.Cp(cp), pov),
            "pv": [chess.Move.from_uci(move)], **extra}


def steady():
    return {6: packet(6, 10), 7: packet(7, 20), 8: packet(8, 25)}


def fools_mate_board():
    board = chess.Board()
    for move in ("f2f3", "e7e5", "g2g4"):
        board.push_uci(move)
    return board


class PrecisionPolicyTests(unittest.TestCase):
    def test_public_time_and_depth_contract(self):
        self.assertEqual((precision.BASE_BUDGET_MS, precision.MAX_BUDGET_MS), (3000, 10000))
        self.assertEqual(precision.MIN_REFINED_DEPTH, 8)

    def test_exact_scores_are_white_pov_and_inputs_are_unchanged(self):
        board = chess.Board()
        before = board.fen(), list(board.move_stack)
        info = packet()
        original = copy.deepcopy(info)
        self.assertEqual(precision.validate_packet(board, info)["cp"], 20)
        self.assertEqual((board.fen(), board.move_stack), before)
        self.assertEqual(info, original)
        board.push_uci("e2e4")
        self.assertEqual(precision.validate_packet(board, packet(cp=70, move="e7e5", pov=chess.BLACK))["cp"], -70)

    def test_partial_bound_wrong_pov_rank_and_illegal_line_are_rejected(self):
        board = chess.Board()
        bad = [None, {}, {"depth": 8}, {"score": packet()["score"]},
               packet(depth=0), packet(depth=True), packet(pov=chess.BLACK),
               packet(multipv=2), packet(multipv=True), packet(lowerbound=True),
               packet(upperbound=True), packet(pv=[]), packet(pv=["e2e4"]),
               packet(move="e2e5"), packet(pv=[chess.Move.from_uci("e2e4"), chess.Move.from_uci("e2e3")]),
               packet(score=20)]
        for info in bad:
            with self.subTest(info=info):
                self.assertIsNone(precision.validate_packet(board, info))

    def test_mate_requires_exact_legal_terminal_line_distance_and_winner_both_colors(self):
        board = fools_mate_board()
        for current, move in ((board, "d8h4"), (board.mirror(), "d1h5")):
            with self.subTest(turn=current.turn):
                valid = packet(move=move, pov=current.turn,
                    score=chess.engine.PovScore(chess.engine.Mate(1), current.turn))
                sample = precision.validate_packet(current, valid)
                self.assertIsNone(sample["cp"])
                self.assertEqual(sample["winner"], "white" if current.turn else "black")
                self.assertEqual(sample["mate"], 1 if current.turn else -1)
                for score in (chess.engine.Cp(900), chess.engine.Mate(2), chess.engine.Mate(-1)):
                    self.assertIsNone(precision.validate_packet(current,
                        {**valid, "score": chess.engine.PovScore(score, current.turn)}))
        self.assertIsNone(precision.validate_packet(board,
            packet(move="b8c6", pov=chess.BLACK,
                score=chess.engine.PovScore(chess.engine.Mate(1), chess.BLACK))))

    def test_negative_mate_distance_uses_even_number_of_plies(self):
        board = chess.Board()
        board.push_uci("f2f3")
        board.push_uci("e7e5")
        info = packet(score=chess.engine.PovScore(chess.engine.Mate(-1), chess.WHITE),
            pv=[chess.Move.from_uci("g2g4"), chess.Move.from_uci("d8h4")])
        self.assertEqual(precision.validate_packet(board, info)["winner"], "black")
        info["score"] = chess.engine.PovScore(chess.engine.Mate(-2), chess.WHITE)
        self.assertIsNone(precision.validate_packet(board, info))

    def test_convergence_needs_latest_three_consecutive_deep_iterations(self):
        board = chess.Board()
        self.assertTrue(precision.converged(board, steady()))
        for rows in ({}, {7: packet(7), 8: packet(8)},
                     {5: packet(5), 6: packet(6), 7: packet(7)},
                     {5: packet(5), 7: packet(7), 8: packet(8)},
                     {**steady(), 9: packet(8)}, {"8": packet(8)}):
            with self.subTest(rows=rows):
                self.assertFalse(precision.converged(board, rows))

    def test_convergence_checks_tight_cp_window_and_root(self):
        board = chess.Board()
        rows = {6: packet(6, -10), 7: packet(7, 25), 8: packet(8, 10)}
        self.assertTrue(precision.converged(board, rows))
        rows[8] = packet(8, 26)
        self.assertFalse(precision.converged(board, rows))
        rows[8] = packet(8, 10, move="d2d4")
        self.assertFalse(precision.converged(board, rows))

    def test_invalid_latest_cannot_be_dropped_to_certify_old_score(self):
        board = chess.Board()
        for bad in (packet(9, lowerbound=True), packet(9, pv=[]), {"depth": 9},
                    packet(9, pov=chess.BLACK), packet(9, multipv=2)):
            self.assertFalse(precision.converged(board, {**steady(), 9: bad}))

    def test_mate_convergence_keeps_mate_and_checks_each_pv(self):
        board = fools_mate_board()
        rows = {depth: packet(depth, move="d8h4", pov=chess.BLACK,
                    score=chess.engine.PovScore(chess.engine.Mate(1), chess.BLACK)) for depth in (6, 7, 8)}
        self.assertTrue(precision.converged(board, rows))
        rows[8] = packet(8, move="b8c6", pov=chess.BLACK)
        self.assertFalse(precision.converged(board, rows))


class PrecisionPublishTests(unittest.TestCase):
    def setUp(self):
        self.service = MoveHints(Path("unused"), stream_evaluation=True)
        self.addCleanup(self.service.close)
        self.board = chess.Board()
        self.prepare()

    def prepare(self):
        with self.service._condition:
            self.service._enabled = False
        self.service.update_position(game_id="test", revision=len(self.board.move_stack), board=self.board,
            human_color=self.board.turn, movetime_ms=10000, engine_name="test", active=True, precision=True)
        with self.service._condition:
            self.service._enabled = True
        self.context, self.generation = self.service._context, self.service._generation
        self.cancel, self.expired = threading.Event(), threading.Event()

    def publish(self, info):
        self.service._publish_evaluation(self.generation, self.context, self.cancel, self.expired, info)

    def value(self):
        return self.service.snapshot_with_evaluation()[1]

    def test_shallow_exact_score_is_immediate_then_precision_is_certified(self):
        self.publish(packet(1, 44))
        self.assertEqual(self.value()["cp"], 44)
        self.assertFalse(self.value()["refinement"]["converged"])
        for info in steady().values():
            self.publish(info)
        self.assertTrue(self.value()["refinement"]["converged"])
        self.assertEqual(self.value()["depth"], 8)

    def test_bounds_and_invalid_newer_score_preserve_raw_last_exact_value(self):
        for info in steady().values():
            self.publish(info)
        for bad in (packet(9, 900, lowerbound=True), packet(9, 999, pv=[]), packet(9, 333, pov=chess.BLACK)):
            self.publish(bad)
            value = self.value()
            self.assertEqual((value["cp"], value["depth"], value["bound"]), (25, 8, "exact"))
            self.assertEqual(value["reliability"], "provisional")
            self.assertFalse(value["refinement"]["converged"])
            self.assertTrue(value["refinement"]["retained_previous_score"])
        self.assertEqual(self.value()["refinement"]["rejected_packets"], 3)

    def test_late_lower_depth_never_replaces_current_depth(self):
        for info in steady().values():
            self.publish(info)
        self.publish(packet(4, 900))
        value = self.value()
        self.assertEqual((value["cp"], value["depth"]), (25, 8))
        self.assertEqual(value["refinement"]["rejected_packets"], 1)

    def test_depth_only_packets_do_not_borrow_old_pv_or_interrupt_convergence(self):
        for info in steady().values():
            self.publish(info)
        self.publish({"depth": 12, "nodes": 9000})
        self.assertEqual(self.value()["depth"], 8)
        self.assertTrue(self.value()["refinement"]["converged"])

    def test_invalid_gap_prevents_convergence_until_three_fresh_valid_depths(self):
        for info in steady().values():
            self.publish(info)
        self.publish(packet(9, lowerbound=True))
        for depth in (10, 11):
            self.publish(packet(depth))
            self.assertFalse(self.value()["refinement"]["converged"])
        self.publish(packet(12))
        self.assertTrue(self.value()["refinement"]["converged"])

    def test_progress_boundary_is_three_seconds_and_stable_stop_is_preserved(self):
        for info in steady().values():
            self.publish(info)
        update = self.service._refinement_progress
        self.assertIsNone(update(self.generation, self.context, 2999))
        self.assertEqual(self.value()["refinement"]["phase"], "initial")
        self.assertEqual(update(self.generation, self.context, 3000), "stable")
        self.assertEqual(self.value()["refinement"]["phase"], "refining")
        self.assertEqual(update(self.generation, self.context, 3050, complete=True), "stable")
        self.assertEqual(self.value()["refinement"]["phase"], "complete")

    def test_volatile_position_keeps_refining_until_cap(self):
        for depth, cp in ((6, 20), (7, 200), (8, -100)):
            self.publish(packet(depth, cp))
        update = self.service._refinement_progress
        self.assertIsNone(update(self.generation, self.context, 3000))
        self.assertIsNone(update(self.generation, self.context, 9999))
        self.assertEqual(update(self.generation, self.context, 10000), "budget")

    def test_new_iteration_after_stop_request_does_not_inherit_stable_reason(self):
        for info in steady().values():
            self.publish(info)
        self.assertEqual(self.service._refinement_progress(self.generation, self.context, 3000), "stable")
        self.publish(packet(9, 500))
        self.assertEqual(self.value()["cp"], 500)
        self.assertFalse(self.value()["refinement"]["converged"])
        self.assertEqual(self.value()["refinement"]["stop_reason"], "stability_changed")

    def test_cancelled_or_new_generation_cannot_accept_old_score_or_timer(self):
        old = self.generation, self.context, self.cancel, self.expired
        self.cancel.set()
        self.publish(packet())
        self.assertIsNone(self.value()["cp"])
        self.board.push_uci("e2e4")
        self.prepare()
        self.service._publish_evaluation(*old, packet(20, 900))
        self.assertIsNone(self.service._refinement_progress(old[0], old[1], 9000))
        self.assertIsNone(self.value()["cp"])
        self.assertEqual(self.value()["refinement"]["elapsed_ms"], 0)

    def test_snapshots_detach_nested_refinement(self):
        self.value()["refinement"]["stop_reason"] = "tampered"
        self.assertIsNone(self.value()["refinement"]["stop_reason"])


class SyntheticStream:
    def __init__(self, owner, board, limit):
        self.owner, self.board, self.limit = owner, board, limit
        self.info = {}
        self.stop_event = threading.Event()
        self.stop_count = 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def __iter__(self):
        for info in self.owner.packets:
            self.info.update(info)
            yield info
        self.owner.waiting.set()
        if not self.owner.early_finish:
            self.stop_event.wait(2)

    def stop(self):
        self.stop_count += 1
        self.stop_event.set()

    def wait(self):
        return chess.engine.BestMove(next(iter(self.board.legal_moves)), None)


class SyntheticPeer:
    def __init__(self, packets, *, early_finish=False):
        self.packets = packets
        self.early_finish = early_finish
        self.calls = []
        self.waiting = threading.Event()

    def analysis(self, board, limit, **kwargs):
        self.stream = SyntheticStream(self, board, limit)
        self.calls.append((limit, kwargs))
        return self.stream

    def close(self):
        if hasattr(self, "stream"):
            self.stream.stop_event.set()


def wait_ready(service):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        value = service.snapshot_with_evaluation()[1]
        if value["status"] in ("ready", "unavailable"):
            return value
        time.sleep(.005)
    raise AssertionError("Synthetic helper did not finish")


class PrecisionWorkerTests(unittest.TestCase):
    def start(self, peer, *, budget=140, precise=True):
        service = MoveHints(Path("unused"), lambda _: peer, timeout_grace=.2, stream_evaluation=True)
        self.addCleanup(service.close)
        service.update_position(game_id="worker", revision=0, board=chess.Board(), human_color=chess.WHITE,
            movetime_ms=budget, engine_name="synthetic", active=True, precision=precise)
        service.set_enabled(True)
        return service

    def test_one_uninterrupted_search_stops_stable_even_without_next_packet(self):
        peer = SyntheticPeer(list(steady().values()))
        with patch.object(precision, "BASE_BUDGET_MS", 30):
            service = self.start(peer)
            value = wait_ready(service)
        self.assertEqual(len(peer.calls), 1)
        self.assertNotIn("root_moves", peer.calls[0][1])
        self.assertEqual(peer.calls[0][0].time, .14)
        self.assertGreaterEqual(peer.stream.stop_count, 1)
        self.assertEqual(value["refinement"]["stop_reason"], "stable")
        self.assertEqual(value["refinement"]["phase"], "complete")
        self.assertGreaterEqual(value["refinement"]["elapsed_ms"], 30)
        self.assertEqual(value["depth"], 8)

    def test_unsettled_search_uses_cap_and_never_averages_scores(self):
        peer = SyntheticPeer([packet(6, 10), packet(7, 300), packet(8, -200)])
        with patch.object(precision, "BASE_BUDGET_MS", 30):
            value = wait_ready(self.start(peer, budget=100))
        self.assertEqual(len(peer.calls), 1)
        self.assertEqual(value["cp"], -200)
        self.assertEqual(value["refinement"]["stop_reason"], "budget")
        self.assertGreaterEqual(value["refinement"]["elapsed_ms"], 100)

    def test_engine_can_finish_early_without_false_stability_claim(self):
        peer = SyntheticPeer([packet(2, 60)], early_finish=True)
        value = wait_ready(self.start(peer))
        self.assertEqual(value["refinement"]["stop_reason"], "engine_completed")
        self.assertFalse(value["refinement"]["converged"])
        self.assertEqual(value["cp"], 60)

    def test_disabled_helper_is_promptly_cancelled_and_old_score_cleared(self):
        peer = SyntheticPeer([packet(8, 60)])
        service = self.start(peer, budget=10000)
        self.assertTrue(peer.waiting.wait(1))
        service.set_enabled(False)
        deadline = time.monotonic() + 1
        while service._cancel is not None and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertIsNone(service._cancel)
        value = service.snapshot_with_evaluation()[1]
        self.assertIsNone(value["cp"])
        self.assertEqual(value["status"], "waiting")

    def test_legacy_helper_has_no_refinement_and_preserves_bound_display(self):
        peer = SyntheticPeer([packet(4, 80, lowerbound=True)], early_finish=True)
        value = wait_ready(self.start(peer, precise=False))
        self.assertNotIn("refinement", value)
        self.assertEqual((value["cp"], value["bound"]), (80, "lower"))

    def test_precision_never_treats_play_only_merged_info_as_exact_packet(self):
        class PlayOnly:
            def play(self, *_args, **_kwargs):
                raise AssertionError("Precision must request raw analysis events")
            def close(self):
                pass
        value = wait_ready(self.start(PlayOnly()))
        self.assertEqual(value["status"], "unavailable")
        self.assertIsNone(value["cp"])
        self.assertEqual(value["refinement"]["stop_reason"], "error")


if __name__ == "__main__":
    unittest.main()
