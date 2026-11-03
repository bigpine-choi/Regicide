"""Unrestricted reference audits; they never propose an engine move."""

from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from sparring_referee_tests import FakeFactory, long_board, packet
import chess
import sparring_referee as referee


class RefereeQualityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.binary = Path(temporary.name) / "stockfish-fixture.exe"
        self.binary.write_bytes(b"fake UCI quality peer")

    def peer(self, plan=None, *, available=True, factory=None):
        factory = factory or FakeFactory(plan)
        service = referee.SparringReferee(self.binary if available else None, factory)
        self.addCleanup(service.close)
        return service, factory

    @staticmethod
    def assess(service, board=None, move="e2e4", **options):
        return service.assess_move_quality(game_id="quality", revision=1,
                                           board=board or chess.Board(),
                                           selected_move=move, **options)

    @staticmethod
    def score_plan(scores):
        def plan(board, limit, options, index):
            move = next(iter(board.legal_moves))
            return {"packets": [packet(board, move, scores[index])], "best": move}
        return plan

    def test_unrestricted_root_and_reply_keep_native_selection(self):
        service, factory = self.peer(self.score_plan([75, -170]))
        value = self.assess(service)
        self.assertEqual((value["status"], value["selected_uci"]), ("ready", "e2e4"))
        self.assertEqual((value["root"]["cp_own"], value["child"]["cp_own"]), (75, -170))
        self.assertEqual(value["root"]["fen"], chess.Board().fen(en_passant="fen"))
        self.assertEqual(value["history_uci"], [])
        self.assertEqual(value["source"], referee.SOURCE)
        self.assertEqual(value["policy"], referee.POLICY)
        self.assertEqual(len(value["binary_sha256"]), 64)
        self.assertEqual(len(factory.searches), 2)
        self.assertNotEqual(value["root"]["uci"], value["selected_uci"])
        child = chess.Board()
        child.push_uci("e2e4")
        self.assertEqual(value["child"]["fen"], child.fen(en_passant="fen"))
        self.assertEqual(factory.searches[1]["board"].move_stack, child.move_stack)
        for search in factory.searches:
            self.assertNotIn("root_moves", search["options"])
            self.assertNotIn("multipv", search["options"])
        self.assertLess(factory.searches[0]["limit"].time, .65 * .36)
        self.assertGreater(factory.searches[1]["limit"].time, .65 * .60)
        self.assertEqual(factory.active, 0)
        self.assertTrue(factory.engines[0].closed.is_set())

    def test_black_reference_keeps_original_side_perspective(self):
        board = chess.Board()
        board.push_uci("e2e4")
        service, _ = self.peer(self.score_plan([-80, 250]))
        value = self.assess(service, board, "e7e5")
        self.assertEqual(value["status"], "ready")
        self.assertEqual(value["history_uci"], ["e2e4"])
        self.assertEqual((value["root"]["cp_own"], value["child"]["cp_own"]), (80, -250))
        self.assertEqual((value["root"]["cp_white"], value["child"]["cp_white"]), (-80, 250))

    def test_single_selected_move_is_audited_after_move_fifty(self):
        board = long_board(98)
        service, factory = self.peer()
        value = self.assess(service, board, next(iter(board.legal_moves)))
        self.assertEqual(value["status"], "ready")
        self.assertEqual(len(factory.searches), 2)

    def test_budget_cap_and_insufficient_budget_are_bounded(self):
        service, factory = self.peer()
        value = self.assess(service, budget_ms=5000)
        self.assertEqual((value["status"], value["budget_ms"]), ("ready", 2150))
        self.assertLessEqual(value["analysis_budget_ms"], 650)
        self.assertLess(factory.searches[0]["limit"].time, .65 * .36)
        service, factory = self.peer()
        value = self.assess(service, budget_ms=20)
        self.assertEqual((value["status"], value["reason"]), ("unverified", "insufficient_budget"))
        self.assertEqual(factory.searches, [])
        self.assertEqual(factory.active, 0)

    def test_only_completed_exact_depth_ten_packets_qualify(self):
        for stage in (0, 1):
            for corrupt in ("shallow", "bound", "partial"):
                with self.subTest(stage=stage, corrupt=corrupt):
                    def plan(board, limit, options, index):
                        moves = list(board.legal_moves)
                        row = packet(board, moves[0])
                        best = moves[0]
                        if stage == index:
                            if corrupt == "shallow":
                                row["depth"] = 9
                            elif corrupt == "bound":
                                row["lowerbound"] = True
                            elif corrupt == "partial":
                                row.pop("pv")
                        return {"packets": [row], "best": best}
                    service, factory = self.peer(plan)
                    value = self.assess(service)
                    self.assertEqual((value["status"], value["reason"]),
                                     ("unverified", "root_incomplete" if stage == 0 else "child_incomplete"))
                    self.assertIsNone(value["root"] if stage == 0 else value["child"])
                    self.assertEqual(factory.active, 0)

    def test_last_completed_position_value_survives_different_final_bestmove(self):
        def plan(board, limit, options, index):
            moves = list(board.legal_moves)
            bound = packet(board, moves[1], 500, depth=13)
            bound["lowerbound"] = True
            return {"packets": [packet(board, moves[0], 10, depth=10),
                                packet(board, moves[0], 35, depth=12), bound], "best": moves[1]}
        service, factory = self.peer(plan)
        value = self.assess(service)
        self.assertEqual((value["status"], value["selected_uci"]), ("ready", "e2e4"))
        for name in ("root", "child"):
            evidence = value[name]
            self.assertEqual((evidence["depth"], evidence["cp_own"]), (12, 35))
            self.assertEqual(evidence["scope"], "completed_position_value")
            self.assertEqual(evidence["evaluated_completed_uci"], evidence["uci"])
            self.assertNotEqual(evidence["evaluated_completed_uci"], evidence["returned_uci"])
            self.assertEqual(value[name + "_search"]["returned_uci"], evidence["returned_uci"])
            self.assertEqual(value[name + "_search"]["matching_depths"], [])
        self.assertEqual(factory.active, 0)

    def test_quality_exact_retry_recovers_prior_bound_but_later_bound_invalidates(self):
        for bound_first in (False, True):
            with self.subTest(bound_first=bound_first):
                def plan(board, limit, options, index):
                    move = next(iter(board.legal_moves))
                    exact = packet(board, move, 30, depth=10)
                    bound = packet(board, move, 300, depth=10)
                    bound["upperbound"] = True
                    return {"packets": [bound, exact] if bound_first else [exact, bound], "best": move}
                service, _ = self.peer(plan)
                value = self.assess(service)
                self.assertEqual(value["status"], "ready" if bound_first else "unverified")
                if bound_first:
                    self.assertEqual(value["child"]["cp_own"], 30)
                else:
                    self.assertEqual(value["reason"], "root_incomplete")

    def test_illegal_final_bestmove_cannot_certify_position_value(self):
        def plan(board, limit, options, index):
            move = next(iter(board.legal_moves))
            return {"packets": [packet(board, move)], "best": chess.Move.null()}
        service, _ = self.peer(plan)
        value = self.assess(service)
        self.assertEqual((value["status"], value["reason"]), ("unverified", "referee_search_failed"))

    def test_newer_bound_does_not_replace_completed_exact_packet(self):
        def plan(board, limit, options, index):
            move = next(iter(board.legal_moves))
            bound = packet(board, move, 900, depth=11)
            bound["upperbound"] = True
            return {"packets": [packet(board, move, 25, depth=10), bound], "best": move}
        service, _ = self.peer(plan)
        value = self.assess(service)
        self.assertEqual(value["status"], "ready")
        self.assertEqual((value["root"]["depth"], value["child"]["depth"]), (10, 10))
        self.assertEqual((value["root"]["cp_own"], value["child"]["cp_own"]), (25, 25))

    def test_missing_peer_and_initial_cancellation_never_spawn(self):
        service, factory = self.peer(available=False)
        value = self.assess(service)
        self.assertEqual((value["status"], value["reason"]), ("unverified", "referee_unavailable"))
        self.assertEqual(factory.engines, [])
        service, factory = self.peer()
        value = self.assess(service, cancelled=lambda: True)
        self.assertEqual((value["status"], value["reason"]), ("unverified", "cancelled"))
        self.assertEqual(factory.engines, [])

    def test_cancel_while_waiting_for_shared_screen_lock(self):
        service, factory = self.peer()
        cancelled = threading.Event()
        held, release = threading.Event(), threading.Event()
        def hold_lock():
            with service._screen_lock:
                held.set()
                release.wait(1)
        thread = threading.Thread(target=hold_lock)
        thread.start()
        self.addCleanup(thread.join, .5)
        self.addCleanup(release.set)
        self.assertTrue(held.wait(.3))
        timer = threading.Timer(.03, cancelled.set)
        timer.start()
        self.addCleanup(timer.cancel)
        start = time.monotonic()
        value = self.assess(service, cancelled=cancelled.is_set)
        self.assertEqual(value["reason"], "cancelled")
        self.assertLess(time.monotonic() - start, .35)
        self.assertEqual(factory.engines, [])

    def test_timeout_cancels_only_the_owned_peer(self):
        gate = threading.Event()
        def plan(board, limit, options, index):
            move = next(iter(board.legal_moves))
            return {"packets": [packet(board, move)], "best": move, "gate": gate}
        service, factory = self.peer(plan)
        unrelated = threading.Event()
        start = time.monotonic()
        value = self.assess(service, budget_ms=120)
        self.assertEqual((value["status"], value["reason"]), ("unverified", "timeout"))
        self.assertLess(time.monotonic() - start, .5)
        self.assertTrue(factory.engines[0].closed.is_set())
        self.assertFalse(unrelated.is_set())
        self.assertEqual(factory.active, 0)

    def test_cancellation_in_either_search_never_returns_ready(self):
        for stage in (0, 1):
            with self.subTest(stage=stage):
                cancelled = threading.Event()
                def plan(board, limit, options, index):
                    move = next(iter(board.legal_moves))
                    if index == stage:
                        cancelled.set()
                    return {"packets": [packet(board, move)], "best": move}
                service, factory = self.peer(plan)
                value = self.assess(service, cancelled=cancelled.is_set)
                self.assertEqual((value["status"], value["reason"]), ("unverified", "cancelled"))
                self.assertEqual(factory.active, 0)

    def test_position_change_during_search_invalidates_audit(self):
        gate = threading.Event()
        def plan(board, limit, options, index):
            move = next(iter(board.legal_moves))
            return {"packets": [packet(board, move)], "best": move, "gate": gate}
        service, factory = self.peer(plan)
        completed = []
        thread = threading.Thread(target=lambda: completed.append(self.assess(service)))
        thread.start()
        self.assertTrue(factory.entered.wait(.3))
        newer = chess.Board()
        newer.push_uci("d2d4")
        service.update_position(game_id="quality", revision=2, board=newer, active=False)
        thread.join(timeout=.5)
        self.assertFalse(thread.is_alive())
        self.assertEqual((completed[0]["status"], completed[0]["reason"]),
                         ("unverified", "position_changed"))
        self.assertEqual(factory.active, 0)

    def test_same_fen_but_other_history_is_stale(self):
        board = chess.Board()
        for move in ("g1f3", "g8f6", "f3g1", "f6g8"):
            board.push_uci(move)
        no_history = chess.Board(board.fen(en_passant="fen"))
        service, factory = self.peer()
        service.update_position(game_id="quality", revision=1, board=board, active=False)
        value = self.assess(service, no_history)
        self.assertEqual((value["status"], value["reason"]), ("unverified", "stale_position"))
        self.assertEqual(factory.engines, [])

    def test_live_reference_is_stopped_before_quality_peer_starts(self):
        gate = threading.Event()
        def plan(board, limit, options, index):
            move = next(iter(board.legal_moves))
            return {"packets": [packet(board, move)], "best": move,
                    "gate": gate if index == 0 else None}
        service, factory = self.peer(plan)
        service.update_position(game_id="quality", revision=1, board=chess.Board())
        self.assertTrue(factory.entered.wait(.3))
        value = self.assess(service)
        service.cancel()
        self.assertEqual(value["status"], "ready")
        self.assertTrue(factory.engines[0].closed.is_set())
        self.assertEqual(factory.maximum, 1)

    def test_mate_scores_keep_original_side_and_explicit_winner(self):
        board = chess.Board()
        board.push_uci("e2e4")
        def plan(position, limit, options, index):
            move = next(iter(position.legal_moves))
            return {"packets": [packet(position, move, mate_white=-5 if index == 0 else 4)], "best": move}
        service, _ = self.peer(plan)
        value = self.assess(service, board, "e7e5")
        self.assertEqual(value["status"], "ready")
        self.assertEqual((value["root"]["mate_own"], value["child"]["mate_own"]), (5, -4))
        self.assertEqual((value["root"]["winner"], value["child"]["winner"]), ("black", "white"))
        self.assertIsNone(value["root"]["cp_own"])
        self.assertIsNone(value["child"]["cp_own"])

    def test_rule_proven_checkmate_child_needs_no_reply_search(self):
        board = chess.Board("7k/5Q2/6K1/8/8/8/8/8 w - - 0 1")
        chosen = chess.Move.from_uci("f7g7")
        def plan(position, limit, options, index):
            return {"packets": [packet(position, chosen, mate_white=1)], "best": chosen}
        service, factory = self.peer(plan)
        value = self.assess(service, board, chosen)
        self.assertEqual(value["status"], "ready")
        self.assertEqual(len(factory.searches), 1)
        child = value["child"]
        self.assertEqual((child["terminal"], child["termination"], child["winner"], child["outcome"]),
                         (True, "checkmate", "white", "1-0"))
        self.assertEqual(child["mate_own"], 0)
        self.assertIsNone(child["cp_own"])

    def test_rule_proven_stalemate_child_has_exact_zero(self):
        board = chess.Board("7k/5Q2/6K1/8/8/8/8/8 w - - 0 1")
        chosen = chess.Move.from_uci("f7e6")
        def plan(position, limit, options, index):
            return {"packets": [packet(position, chosen, cp_white=0)], "best": chosen}
        service, factory = self.peer(plan)
        value = self.assess(service, board, chosen)
        self.assertEqual(value["status"], "ready")
        self.assertEqual(len(factory.searches), 1)
        child = value["child"]
        self.assertEqual((child["terminal"], child["termination"], child["outcome"]),
                         (True, "stalemate", "1/2-1/2"))
        self.assertEqual((child["cp_white"], child["cp_own"]), (0, 0))
        self.assertIsNone(child["mate_own"])
        self.assertIsNone(child["winner"])

    def test_terminal_root_skips_without_starting_peer(self):
        board = chess.Board("7k/6Q1/6K1/8/8/8/8/8 b - - 0 1")
        service, factory = self.peer()
        value = self.assess(service, board, chess.Move.null())
        self.assertEqual((value["status"], value["reason"]), ("skipped", "terminal_position"))
        self.assertEqual(factory.engines, [])

    def test_session_reuses_cold_peer_and_closes_at_exit(self):
        class ColdFactory(FakeFactory):
            def __call__(self, binary):
                time.sleep(.75)
                return super().__call__(binary)
        service, factory = self.peer(factory=ColdFactory())
        with service.quality_session():
            first = self.assess(service, budget_ms=2150)
            self.assertEqual(first["status"], "ready")
            self.assertGreater(first["startup_ms"], 700)
            self.assertLessEqual(first["analysis_budget_ms"], 650)
            self.assertFalse(first["engine_reused"])
            second = self.assess(service, move="d2d4", budget_ms=650)
            self.assertEqual(second["status"], "ready")
            self.assertTrue(second["engine_reused"])
            self.assertLess(second["startup_ms"], 100)
            self.assertEqual(len(factory.engines), 1)
            self.assertEqual(factory.active, 1)
            self.assertTrue(service._screen_active)
        self.assertEqual(factory.active, 0)
        self.assertFalse(service._screen_active)
        self.assertIsNone(service._quality_state)

    def test_session_exception_closes_cached_peer_and_unlocks(self):
        service, factory = self.peer()
        with self.assertRaisesRegex(RuntimeError, "fixture"):
            with service.quality_session():
                self.assertEqual(self.assess(service)["status"], "ready")
                raise RuntimeError("fixture")
        self.assertEqual(factory.active, 0)
        with service.quality_session():
            self.assertEqual(self.assess(service)["status"], "ready")
        self.assertEqual(len(factory.engines), 2)
        self.assertEqual(factory.active, 0)

    def test_session_keeps_healthy_peer_after_incomplete_evidence(self):
        for stage in (0, 1):
            with self.subTest(stage=stage):
                def plan(board, limit, options, index):
                    move = next(iter(board.legal_moves))
                    return {"packets": [packet(board, move, depth=9 if index == stage else 10)],
                            "best": move}
                service, factory = self.peer(plan)
                with service.quality_session():
                    first = self.assess(service)
                    self.assertEqual(first["status"], "unverified")
                    diagnostics = first["root_search" if stage == 0 else "child_search"]
                    self.assertEqual(diagnostics["matching_depths"], [9])
                    self.assertEqual(diagnostics["completed_depths"], [9])
                    second = self.assess(service, move="d2d4")
                    self.assertEqual(second["status"], "ready")
                    self.assertTrue(second["engine_reused"])
                    self.assertEqual(len(factory.engines), 1)
                self.assertEqual(factory.active, 0)
    def test_session_entry_is_nonblocking_when_another_thread_owns_screen(self):
        service, _ = self.peer()
        held, release = threading.Event(), threading.Event()
        def hold_lock():
            with service._screen_lock:
                held.set()
                release.wait(1)
        thread = threading.Thread(target=hold_lock)
        thread.start()
        self.addCleanup(thread.join, .5)
        self.addCleanup(release.set)
        self.assertTrue(held.wait(.3))
        start = time.monotonic()
        with self.assertRaises(referee.RefereeUnavailable):
            with service.quality_session():
                self.fail("busy session entered")
        self.assertLess(time.monotonic() - start, .1)

    def test_idle_session_callback_cancels_cached_peer_without_next_audit(self):
        service, factory = self.peer()
        cancelled = threading.Event()
        with service.quality_session():
            self.assertEqual(self.assess(service, cancelled=cancelled.is_set)["status"], "ready")
            cancelled.set()
            self.assertTrue(factory.engines[0].closed.wait(.3))
            value = self.assess(service)
            self.assertEqual((value["status"], value["reason"]), ("unverified", "cancelled"))
            self.assertEqual(len(factory.engines), 1)
        self.assertEqual(factory.active, 0)

    def test_idle_session_position_change_closes_cache_and_stays_exclusive(self):
        service, factory = self.peer()
        with service.quality_session():
            self.assertEqual(self.assess(service)["status"], "ready")
            newer = chess.Board()
            newer.push_uci("e2e4")
            service.update_position(game_id="quality", revision=2, board=newer, active=False)
            self.assertTrue(factory.engines[0].closed.wait(.3))
            value = self.assess(service)
            self.assertEqual(value["status"], "unverified")
            self.assertTrue(service._screen_active)
            self.assertEqual(len(factory.engines), 1)
        self.assertEqual(factory.active, 0)

    def test_idle_session_deadline_closes_cached_peer(self):
        service, factory = self.peer()
        with mock.patch.object(referee, "MAX_QUALITY_SESSION_MS", 120):
            with service.quality_session():
                self.assertEqual(self.assess(service)["status"], "ready")
                self.assertTrue(factory.engines[0].closed.wait(.4))
                value = self.assess(service)
                self.assertEqual((value["status"], value["reason"]), ("unverified", "timeout"))
                self.assertEqual(len(factory.engines), 1)
        self.assertEqual(factory.active, 0)

    def test_live_reference_remains_excluded_between_session_audits(self):
        gate = threading.Event()
        def plan(board, limit, options, index):
            move = next(iter(board.legal_moves))
            return {"packets": [packet(board, move)], "best": move,
                    "gate": gate if index == 0 else None}
        service, factory = self.peer(plan)
        service.update_position(game_id="quality", revision=1, board=chess.Board())
        self.assertTrue(factory.entered.wait(.3))
        with service.quality_session():
            self.assertEqual(self.assess(service)["status"], "ready")
            count = len(factory.searches)
            time.sleep(.04)
            self.assertEqual(len(factory.searches), count)
            self.assertTrue(self.assess(service, move="d2d4")["engine_reused"])
            self.assertEqual(factory.maximum, 1)
        service.cancel()
        self.assertEqual(factory.maximum, 1)


if __name__ == "__main__":
    unittest.main()
