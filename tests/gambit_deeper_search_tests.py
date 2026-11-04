"""Gambit reclaims idle time without extending its visible turn or reserves."""
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
import engine_pacing
from play_human import HumanGame


def clock(**changes):
    return {"enabled": True, "status": "running", "running_color": "white",
            "white_ms": 600_000, "black_ms": 600_000, "turn_elapsed_ms": 0, **changes}


def pacing(**changes):
    return {"policy": engine_pacing.POLICY, "enabled": True, "color": "white",
            "reason": "shared_human_recommendation", "target_total_ms": 6150,
            "reserve_ms": 10_000, "post_search_reduction_ms": 0,
            "clarity_kind": "unclassified", "pressure_factor": 1.0, **changes}


class AllocationTests(unittest.TestCase):
    def budgets(self, plan=None, snapshot=None, *, automatic=True, configured=1000):
        plan = pacing() if plan is None else plan
        snapshot = clock() if snapshot is None else snapshot
        before = copy.deepcopy((plan, snapshot))
        ordinary = engine_pacing.search_budget(plan, snapshot, configured, automatic=automatic)
        deeper = engine_pacing.search_budget(plan, snapshot, configured, automatic=automatic, deepen=True)
        self.assertEqual((plan, snapshot), before)
        return ordinary, deeper

    def test_ordinary_six_second_allowance_becomes_real_search(self):
        old, new = self.budgets()
        self.assertEqual((old["total_search_ms"], old["baseline_ms"]), (6000, 2000))
        self.assertEqual(new["baseline_ms"], 6000)
        self.assertEqual(new["total_search_ms"], old["total_search_ms"])
        self.assertEqual(new["deeper_search"]["policy"], "regicide-gambit-search-time-v1")
        self.assertTrue(new["deeper_search"]["active"])
        self.assertEqual(new["deeper_search"]["previous_baseline_ms"], 2000)
        self.assertEqual(new["deeper_search"]["reclaimed_ms"], 4000)

    def test_one_second_optional_verification_reserve_is_preserved(self):
        plan = pacing(analysis_reserve={"active": True, "minimum_baseline_ms": 500, "optional_ms": 1000})
        old, new = self.budgets(plan)
        self.assertEqual((old["baseline_ms"], old["optional_ms"]), (2000, 1000))
        self.assertEqual((new["baseline_ms"], new["optional_ms"]), (5000, 1000))
        self.assertEqual(new["baseline_ms"] + new["optional_ms"], new["total_search_ms"])
        self.assertTrue(new["analysis_reserve_active"])

    def test_visible_post_search_reduction_remains_a_hard_cap(self):
        plan = pacing(post_search_reduction_ms=1500)
        old, new = self.budgets(plan)
        self.assertEqual(old["baseline_ms"], 2000)
        self.assertEqual(new["baseline_ms"], 4500)
        self.assertLessEqual(new["baseline_ms"] + new["optional_ms"] + new["completion_margin_ms"],
                             engine_pacing.wait_target_ms(plan))
        self.assertEqual(new["deeper_search"]["search_target_ms"], 4650)

    def test_optional_reserve_and_shortened_visible_deadline_both_survive(self):
        plan = pacing(post_search_reduction_ms=1500,
                      analysis_reserve={"active": True, "minimum_baseline_ms": 500, "optional_ms": 1000})
        _, new = self.budgets(plan)
        self.assertEqual((new["baseline_ms"], new["optional_ms"]), (3500, 1000))
        self.assertEqual(new["baseline_ms"] + new["optional_ms"] + new["completion_margin_ms"], 4650)

    def test_already_spent_turn_time_is_not_granted_again(self):
        plan = pacing(post_search_reduction_ms=1500)
        snapshot = clock(turn_elapsed_ms=1000)
        _, new = self.budgets(plan, snapshot)
        self.assertEqual(new["baseline_ms"], 3500)
        self.assertEqual(snapshot["turn_elapsed_ms"] + new["baseline_ms"] + new["completion_margin_ms"],
                         engine_pacing.wait_target_ms(plan))

    def test_clock_reserve_limits_idle_time_reclamation(self):
        plan = pacing(reserve_ms=15_000)
        _, new = self.budgets(plan, clock(white_ms=20_000))
        self.assertEqual(new["baseline_ms"], 4850)
        self.assertGreaterEqual(20_000 - new["baseline_ms"] - new["completion_margin_ms"], plan["reserve_ms"])

    def test_manual_and_untimed_allocations_are_exactly_unchanged(self):
        for snapshot, automatic in ((clock(), False), (clock(enabled=False), True), (None, True)):
            with self.subTest(snapshot=snapshot, automatic=automatic):
                plan = pacing()
                old = engine_pacing.search_budget(plan, snapshot, 1000, automatic=automatic)
                new = engine_pacing.search_budget(plan, snapshot, 1000, automatic=automatic, deepen=True)
                self.assertEqual(new, old)

    def test_urgent_clocks_keep_the_existing_allocation_exactly(self):
        for remaining in (1, 500, 2000, engine_pacing.LOW_CLOCK_MS):
            with self.subTest(remaining=remaining):
                old, new = self.budgets(snapshot=clock(white_ms=remaining))
                self.assertEqual(new, old)

    def test_every_obvious_move_kind_keeps_the_existing_quick_path(self):
        for kind in engine_pacing.QUICK_KINDS:
            with self.subTest(kind=kind):
                old, new = self.budgets(pacing(clarity_kind=kind))
                self.assertEqual(new, old)
                self.assertNotIn("deeper_search", new)

    def test_inactive_clock_mismatched_turn_and_disabled_plan_are_unchanged(self):
        for plan, snapshot in ((pacing(), clock(status="paused")),
                               (pacing(), clock(running_color="black")),
                               (pacing(enabled=False), clock()),
                               (pacing(reason="no_recommendation"), clock())):
            with self.subTest(plan=plan, snapshot=snapshot):
                old, new = self.budgets(plan, snapshot)
                self.assertEqual(new, old)

    def test_a_visible_budget_below_existing_search_never_shortens_the_old_search(self):
        old, new = self.budgets(pacing(target_total_ms=2000, post_search_reduction_ms=1500))
        self.assertEqual(new, old)
        self.assertNotIn("deeper_search", new)

    def test_nonfinite_source_allowances_do_not_enable_new_search(self):
        for plan, snapshot in ((pacing(target_total_ms=float("inf")), clock()),
                               (pacing(reserve_ms=float("nan")), clock()),
                               (pacing(), clock(white_ms=float("inf")))):
            with self.subTest(plan=plan, snapshot=snapshot):
                old = engine_pacing.search_budget(plan, snapshot, 1000, automatic=True)
                new = engine_pacing.search_budget(plan, snapshot, 1000, automatic=True, deepen=True)
                self.assertEqual(new, old)


class FakeClock:
    def __init__(self):
        self.now = 0.0
    def __call__(self):
        return self.now
    def advance(self, seconds):
        self.now += seconds


class Peer:
    id = {"name": "Gambit search allowance test peer"}
    def __init__(self, now=None):
        self.now, self.calls, self.closed = now, [], False
    def play(self, board, limit, **kwargs):
        self.calls.append((board.copy(stack=True), limit, kwargs))
        if self.now:
            self.now.advance(limit.time or 0)
        move = chess.Move.from_uci("e1g1")
        if move not in board.legal_moves:
            move = next(iter(board.legal_moves))
        return chess.engine.PlayResult(move, None, {"depth": 2, "nodes": 10})
    def quit(self):
        self.closed = True
    def close(self):
        self.closed = True


class HostTests(unittest.TestCase):
    def test_real_host_passes_more_native_search_time_only_for_gambit_mode(self):
        observed = {}
        for mode in ("basic", "gambit"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory(prefix="regicide-gambit-search-test-") as name:
                root = Path(name)
                binary = root / "fake.exe"
                binary.write_bytes(b"fake test peer, never executed")
                now, peers = FakeClock(), []
                def factory(_):
                    peer = Peer(now)
                    peers.append(peer)
                    return peer
                game = HumanGame(binary, root / "games", factory,
                    hint_engine_factory=lambda _: Peer(), evaluation_engine_factory=lambda _: Peer(),
                    opening_variety_enabled=False, monotonic=now,
                    pacing_wait=lambda seconds, event: now.advance(seconds))
                try:
                    game.new_game("black", 1000, game.revision, boss_mode=mode == "gambit", time_control="10+0")
                    # A shared custom midgame root disables unrelated opening
                    # and gambit candidate policies; the host still uses its
                    # real mode-to-search-budget plumbing and real clock.
                    game.board = chess.Board("r3k2r/ppp2ppp/2n5/8/8/2N5/PPP2PPP/R3K2R w KQkq - 0 12")
                    game.revision += 1
                    def fixed_plan(board, snapshot, **kwargs):
                        return pacing(post_search_reduction_ms=1500, root_ply=len(board.move_stack),
                            before_fen=board.fen(en_passant="fen"), root_fen=board.root().fen(en_passant="fen"),
                            history_uci=[move.uci() for move in board.move_stack])
                    with patch.object(engine_pacing, "plan", side_effect=fixed_plan):
                        state = game.engine_move(game.revision)
                    self.assertIsNone(state["error"])
                    self.assertEqual(len(peers[0].calls), 1)
                    self.assertEqual(game.board.peek().uci(), "e1g1")
                    receipt = game._engine_pacing_decisions[1]
                    observed[mode] = {"seconds": peers[0].calls[0][1].time,
                                      "allocation": receipt["allocation"], "elapsed_ms": game._clock.history[1]["elapsed_ms"]}
                finally:
                    game.close()
        self.assertEqual(observed["basic"]["seconds"], 2.0)
        self.assertEqual(observed["gambit"]["seconds"], 4.5)
        self.assertNotIn("deeper_search", observed["basic"]["allocation"])
        self.assertTrue(observed["gambit"]["allocation"]["deeper_search"]["active"])
        self.assertAlmostEqual(observed["basic"]["elapsed_ms"], 4650, delta=1)
        self.assertAlmostEqual(observed["gambit"]["elapsed_ms"], 4650, delta=1)


if __name__ == "__main__":
    unittest.main()
