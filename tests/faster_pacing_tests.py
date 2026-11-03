"""Another half-second faster presentation with unchanged search/safety budgets.

Clock/host checks use deterministic engine and time fixtures. They verify
accounting and application, not native latency or playing strength.
"""
import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import chess
import chess.engine

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import engine_pacing as pacing
from game_clock_tests import Now
from human_play_tests import FakeFactory
from play_human import HumanGame


def board_after(line=""):
    board = chess.Board()
    for san in line.split():
        board.push_san(san)
    return board


def clock(board, remaining=600000, elapsed=0):
    return {"enabled": True, "status": "running",
            "running_color": "white" if board.turn else "black",
            "white_ms": remaining, "black_ms": remaining,
            "turn_elapsed_ms": elapsed, "increment_ms": 0}


class FasterPacingTests(unittest.TestCase):
    def test_faster_plan_preserves_original_search_and_recommendation(self):
        for line in ("", "Nc3", "d4 d5 Bf4 Nf6 e3 e6 Nf3 c5"):
            board = board_after(line)
            current = clock(board, elapsed=20)
            with self.subTest(line=line):
                with patch.object(pacing, "POST_SEARCH_REDUCTION_MS", 1000):
                    old = pacing.plan(board, current, seed="faster-audit")
                new = pacing.plan(board, current, seed="faster-audit")
                self.assertEqual(new["post_search_reduction_ms"], 1500)
                self.assertEqual(new["target_total_ms"], old["target_total_ms"])
                self.assertEqual(new["recommended_total_ms"], old["recommended_total_ms"])
                for automatic in (False, True):
                    self.assertEqual(
                        pacing.search_budget(new, current, 3000, automatic=automatic),
                        pacing.search_budget(old, current, 3000, automatic=automatic))

    def test_reduction_applies_after_quiet_and_contact_opening_caps(self):
        for line in ("Nc3", "d4 d5 Bf4 Nf6 e3 e6 Nf3 c5"):
            board = board_after(line)
            current = clock(board, elapsed=100)
            plan = pacing.plan(board, current, seed="faster-audit")
            with self.subTest(line=line):
                self.assertIsNotNone(plan["opening_budget"])
                for previous_reduction in (0, 1000):
                    old = {**plan, "post_search_reduction_ms": previous_reduction}
                    old_wait = pacing.remaining_wait_ms(old, current)
                    self.assertEqual(pacing.remaining_wait_ms(plan, current),
                                     max(0, old_wait - (1500 - previous_reduction)))

    def test_repeated_polling_and_refinement_do_not_subtract_twice(self):
        board = chess.Board()
        current = clock(board)
        plan = pacing.plan(board, current, seed="faster-audit")
        original = copy.deepcopy(plan)
        raw = {depth: {"depth": depth, "score": chess.engine.PovScore(chess.engine.Cp(20), board.turn),
                       "pv": [chess.Move.from_uci("b1c3"), chess.Move.from_uci("d7d5")]}
               for depth in (4, 5, 6)}
        args = {"selected_move": chess.Move.from_uci("b1c3"), "selected_info": raw[6],
                "baseline_packets": raw, "seed": "faster-audit"}
        once = pacing.refine(board, plan, current, **args)
        twice = pacing.refine(board, once, current, **args)
        self.assertEqual(pacing.wait_target_ms(once), pacing.wait_target_ms(twice))
        expected = pacing.remaining_wait_ms(once, current)
        for _ in range(8):
            self.assertEqual(pacing.remaining_wait_ms(once, current), expected)
        self.assertEqual(plan, original)

    def test_fast_or_already_computed_turn_is_never_extended(self):
        board = chess.Board()
        plan = pacing.plan(board, clock(board), seed="faster-audit")
        for target, elapsed in ((100, 0), (700, 0), (4000, 3100), (4000, 5000)):
            with self.subTest(target=target, elapsed=elapsed):
                candidate = {**plan, "target_total_ms": target}
                self.assertEqual(pacing.remaining_wait_ms(candidate, clock(board, elapsed=elapsed)), 0)

    def test_recovery_budget_is_preserved_and_adds_no_wait(self):
        board = chess.Board()
        current = clock(board, elapsed=200)
        plan = pacing.plan(board, current, seed="faster-audit")
        move = chess.Move.from_uci("e2e4")
        for cp in (-250, 250):
            packet = {"depth": 5, "score": chess.engine.PovScore(chess.engine.Cp(cp), board.turn), "pv": [move]}
            args = {"baseline_info": packet, "baseline_move": move}
            old = pacing.recovery_plan(board, {**plan, "post_search_reduction_ms": 0}, current, **args)
            new = pacing.recovery_plan(board, plan, current, **args)
            with self.subTest(cp=cp):
                self.assertTrue(new["balance_recovery"]["active"])
                self.assertEqual(new["balance_recovery"], old["balance_recovery"])
                self.assertEqual(pacing.remaining_wait_ms(new, current), 0)

    def test_safety_low_clock_and_inactive_clock_never_wait(self):
        board = chess.Board()
        plan = {**pacing.plan(board, clock(board)), "target_total_ms": 15000}
        self.assertEqual(pacing.remaining_wait_ms({**plan, "safety_check": {"active": True}}, clock(board)), 0)
        self.assertEqual(pacing.remaining_wait_ms({**plan, "quality_check": {"active": True}}, clock(board)), 0)
        for current in (clock(board, 10000), clock(board, 1000),
                        {**clock(board), "status": "paused"}, {**clock(board), "running_color": "black"}):
            self.assertEqual(pacing.remaining_wait_ms(plan, current), 0)

    def test_legacy_receipt_without_new_metadata_keeps_its_target(self):
        board = chess.Board()
        legacy = {"enabled": True, "color": "white", "target_total_ms": 4000, "reserve_ms": 10000}
        self.assertEqual(pacing.wait_target_ms(legacy), 4000)
        self.assertEqual(pacing.remaining_wait_ms(legacy, clock(board, elapsed=500)), 3500)

    def test_completed_shortened_long_thought_still_triggers_cooldown(self):
        for target, original, reduction in ((29000, 9000, 1500), (44000, 40000, 1500), (29000, 9000, 1000)):
            row = {"refinement": {"kind": "long_deliberation", "original_target_total_ms": original},
                   "completion_reason": "target_reached", "spent_before_wait_ms": 3000,
                   "waited_ms": target - reduction - 3000, "target_total_ms": target,
                   "post_search_reduction_ms": reduction}
            with self.subTest(target=target):
                self.assertTrue(pacing._completed_long_think(row))
                self.assertFalse(pacing._completed_long_think({**row, "waited_ms": row["waited_ms"] - 500}))
                self.assertFalse(pacing._completed_long_think({**row, "completion_reason": "client_closing"}))

    def host_turn(self, reduction, search_seconds=.4):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            binary = root / "fake.exe"
            binary.write_bytes(b"fake")
            now, factory, waits = Now(), FakeFactory(), []
            def wait(seconds, cancel_event):
                waits.append(seconds)
                now.advance(seconds)
            game = HumanGame(binary, root / "games", factory, opening_variety_enabled=False,
                             monotonic=now, pacing_wait=wait)
            try:
                game.new_game("black", 1000, game.revision, time_control="10+0")
                game._opening_seed = "faster-audit"
                peer, original_plan = factory.instances[0], pacing.plan
                original_play = peer.play
                def play(board, limit, *, game=None, info=None, root_moves=None):
                    now.advance(search_seconds)
                    return original_play(board, limit, game=game, info=info)
                peer.play = play
                peer.replies.append("e2e4")
                def controlled_plan(*args, **kwargs):
                    result = original_plan(*args, **kwargs)
                    result.update(target_total_ms=4000, post_search_reduction_ms=reduction)
                    return result
                with patch.object(pacing, "plan", controlled_plan):
                    game.engine_move(game.revision)
                return {"receipt": copy.deepcopy(game._engine_pacing_decisions[1]),
                        "elapsed_ms": game._clock.history[1]["elapsed_ms"],
                        "wait_ms": sum(waits) * 1000,
                        "search_seconds": peer.calls[0][1].time,
                        "move": game.board.peek().uci()}
            finally:
                game.close()

    def test_host_debits_another_half_second_with_identical_search_and_move(self):
        new = self.host_turn(1500)
        for previous_reduction in (0, 1000):
            old = self.host_turn(previous_reduction)
            with self.subTest(previous_reduction=previous_reduction):
                self.assertEqual(old["search_seconds"], new["search_seconds"])
                self.assertEqual(old["move"], new["move"])
                self.assertAlmostEqual(old["elapsed_ms"] - new["elapsed_ms"], 1500 - previous_reduction, delta=1)
                self.assertAlmostEqual(old["wait_ms"] - new["wait_ms"], 1500 - previous_reduction, delta=1)
        self.assertEqual(new["receipt"]["target_total_ms"], 4000)
        self.assertEqual(new["receipt"]["wait_target_total_ms"], 2500)

    def test_host_does_not_cut_search_that_exceeds_shorter_target(self):
        result = self.host_turn(1500, search_seconds=3.5)
        self.assertEqual(result["wait_ms"], 0)
        self.assertAlmostEqual(result["elapsed_ms"], 3500, delta=1)
        self.assertEqual(result["move"], "e2e4")


if __name__ == "__main__":
    unittest.main()
