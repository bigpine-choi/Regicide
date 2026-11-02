"""Clock-safe real host routing for sparring balance recovery; controlled peers."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import chess

import human_play_tests as base
from opening_variety_tests import Peer, info
from sparring_state_tests import late_game_board

host = base.play_human


class ManualClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class BudgetPeer(Peer):
    def __init__(self):
        super().__init__()
        self.stop_calls = 0

    def analysis(self, board, limit, **options):
        if self.scripts and "cost_ms" not in self.scripts[0]:
            self.scripts[0]["cost_ms"] = round(limit.time * 1000) if limit.time else 0
        stream = super().analysis(board, limit, **options)
        def stop():
            self.stop_calls += 1
            gate = stream.script.get("gate")
            if gate is not None:
                gate.set()
        stream.stop = stop
        return stream


class RecoveryHostTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="regicide-recovery-host-")
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        binary = root / "controlled.exe"
        binary.write_bytes(b"deterministic test peer; never executed")
        self.now, self.waits, self.peer = ManualClock(), [], BudgetPeer()
        self.peer.clock = self.now
        self.game = host.HumanGame(binary, root / "games", lambda _: self.peer,
            hint_engine_factory=base.FakeFactory(), evaluation_engine_factory=base.FakeFactory(),
            opening_variety_enabled=False, monotonic=self.now, pacing_wait=self.wait)
        self.addCleanup(self.game.close)
        observation = patch.object(host.boss_play, "observe_human_move", return_value=(
            {"valid":False,"forced":False,"reason":"controlled_unknown"}, False))
        observation.start()
        self.addCleanup(observation.stop)

    def wait(self, seconds, _event):
        self.waits.append(seconds)
        self.now.advance(seconds)

    def position(self, moves="e2e4 e7e5 g1f3", *, sparring=True):
        board = moves.copy(stack=True) if isinstance(moves, chess.Board) else chess.Board()
        if not isinstance(moves, chess.Board):
            for move in moves.split():
                board.push_uci(move)
        self.game.new_game("black" if board.turn else "white", 1000, self.game.revision,
                           sparring_mode=sparring, time_control="10+0")
        self.game._opening_seed = "diagnostic-readonly-no-engine"
        for ply in range(len(board.move_stack)):
            self.game._clock.complete_move("white" if ply % 2 == 0 else "black", ply + 1)
        self.game.board = board
        self.game._sync_evaluation()
        self.peer.searches.clear()
        self.peer.scripts.clear()
        self.waits.clear()
        return board

    def queue(self, board, move, cp, **extra):
        script = {"move":move, "packets":[info(board, move, cp, depth) for depth in (4,5,6)], **extra}
        self.peer.scripts.append(script)

    def decision(self, board, baseline, selected):
        return {"policy":host.sparring_play.POLICY, "ply":len(board.move_stack) + 1,
            "before_fen":board.fen(en_passant="fen"), "history_uci":[m.uci() for m in board.move_stack],
            "baseline_uci":baseline.uci(), "selected_uci":selected.uci(),
            "phase":host.sparring_play.phase(board), "reason":"controlled_recovery_candidate",
            "selected_cp_own":30, "band_control":{"active":True,"status":"met","selected_cp":30},
            "opponent_observation":{"valid":False,"forced":False,"reason":"controlled_unknown"}}

    def saved_decision(self):
        return self.game._sparring_decisions[len(self.game.board.move_stack)]

    def test_ahead_short_opening_funds_recovery_before_rating_only_and_never_waits(self):
        board = self.position()
        baseline, alternative = list(board.legal_moves)[:2]
        self.queue(board, baseline.uci(), 500)
        self.queue(board, alternative.uci(), 30)
        calls = []
        def choose(position, best, _final, _packets, search, _close, **options):
            calls.append(options)
            self.assertLess(options["movetime_ms"], host.sparring_play.MIN_TOTAL_MS)
            self.assertGreater(options["recovery_budget_ms"], 1000)
            reply, _, _ = search(options["recovery_budget_ms"], root_move=alternative,
                                 publish=False, fresh=True, depth=4)
            self.assertEqual(reply.move, alternative)
            return self.decision(position, best, alternative), False
        with patch.object(host.sparring_play, "choose", side_effect=choose):
            state = self.game.engine_move(self.game.revision)
        self.assertEqual(len(calls), 1)
        self.assertIsNone(state["error"])
        self.assertEqual(state["last_move"], alternative.uci())
        self.assertEqual(self.waits, [], "Recovery work must not be followed by a cosmetic hold")
        self.assertGreater(self.peer.searches[1]["limit"].time, self.peer.searches[0]["limit"].time)
        self.assertGreater(self.game._clock.history[-1]["elapsed_ms"], 1000)
        recovery = self.saved_decision()["balance_recovery"]
        self.assertTrue(recovery["active"])
        self.assertEqual(recovery["direction"], "ahead")

    def test_behind_spends_idle_allowance_on_unrestricted_deeper_search(self):
        board = self.position()
        baseline, stronger = list(board.legal_moves)[:2]
        self.queue(board, baseline.uci(), -250)
        self.queue(board, stronger.uci(), -140)
        with patch.object(host.sparring_play, "choose", side_effect=lambda b,m,*_args,**_kwargs:
                          (self.decision(b,m,m), False)):
            state = self.game.engine_move(self.game.revision)
        self.assertIsNone(state["error"])
        self.assertEqual(state["last_move"], stronger.uci())
        self.assertGreaterEqual(len(self.peer.searches), 2)
        self.assertIsNone(self.peer.searches[1]["root_moves"])
        self.assertGreater(self.peer.searches[1]["limit"].time, .6)
        self.assertEqual(self.waits, [])
        recovery = self.saved_decision()["balance_recovery"]
        self.assertEqual(recovery["direction"], "behind")
        self.assertEqual(state["result"], "*")

    def test_failed_deeper_search_keeps_legal_original_baseline(self):
        board = self.position()
        baseline = next(iter(board.legal_moves))
        self.queue(board, baseline.uci(), -250)
        self.queue(board, baseline.uci(), -250, failure=RuntimeError("controlled recovery failure"))
        with patch.object(host.sparring_play, "choose", side_effect=lambda b,m,*_args,**_kwargs:
                          (self.decision(b,m,m), False)):
            state = self.game.engine_move(self.game.revision)
        self.assertIsNone(state["error"])
        self.assertEqual(state["last_move"], baseline.uci())
        self.assertEqual(state["result"], "*")
        self.assertEqual(self.waits, [])

    def test_recovery_stops_after_twenty_and_never_reappears_at_fifty(self):
        for ply in (40,41,98,99):
            with self.subTest(ply=ply):
                board = self.position(late_game_board(ply))
                baseline = next(iter(board.legal_moves))
                self.queue(board, baseline.uci(), -250)
                with patch.object(host.sparring_play, "choose", side_effect=lambda b,m,*_args,**_kwargs:
                                  (self.decision(b,m,m), False)) as choose:
                    state = self.game.engine_move(self.game.revision)
                self.assertIsNone(state["error"])
                self.assertEqual(len(self.peer.searches), 1)
                for call in choose.call_args_list:
                    self.assertFalse(call.kwargs.get("recovery_budget_ms"))

    def test_recovery_plan_respects_both_twentieth_move_boundaries(self):
        for ply in (38,39,40,41,98,99):
            with self.subTest(ply=ply):
                board = self.position(late_game_board(ply))
                move = next(iter(board.legal_moves))
                clock = self.game._clock.snapshot()
                plan = host.engine_pacing.plan(board, clock, seed=self.game._opening_seed)
                recovery = host.engine_pacing.recovery_plan(board, plan, clock,
                    baseline_info=info(board,move.uci(),-250,4),baseline_move=move)["balance_recovery"]
                self.assertEqual(recovery["active"], ply < 40)

    def test_inside_band_and_normal_mode_keep_recovery_off(self):
        for sparring, cp in ((True,80),(False,500)):
            with self.subTest(sparring=sparring,cp=cp):
                board = self.position(sparring=sparring)
                baseline = next(iter(board.legal_moves))
                self.queue(board,baseline.uci(),cp)
                with patch.object(host.sparring_play,"choose",side_effect=lambda b,m,*_args,**_kwargs:
                                  (self.decision(b,m,m),False)) as choose:
                    state = self.game.engine_move(self.game.revision)
                self.assertIsNone(state["error"])
                self.assertEqual(len(self.peer.searches),1)
                self.assertTrue(all(not call.kwargs.get("recovery_budget_ms") for call in choose.call_args_list))

    def test_engine_zero_during_recovery_discards_the_late_reply(self):
        board = self.position()
        baseline, stronger = list(board.legal_moves)[:2]
        gate = threading.Event()
        self.queue(board,baseline.uci(),-250)
        self.queue(board,stronger.uci(),-120,gate=gate)
        before = board.fen(en_passant="fen")
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.game.engine_move,self.game.revision)
            self.assertTrue(self.peer.waiting.wait(3))
            # Stream.waiting is first set by the baseline; use search count to
            # establish that the gated second native operation has started.
            deadline = threading.Event()
            for _ in range(1000):
                if len(self.peer.searches) >= 2:
                    break
                deadline.wait(.001)
            self.assertEqual(len(self.peer.searches),2)
            state = self.game.state()
            for _ in range(10):
                if state["result"] != "*":
                    break
                state = self.game.adjust_clock("engine",-60000,state["game_id"],state["clock"]["edit_revision"])
            gate.set()
            final = future.result(5)
        self.assertEqual(final["termination"],"time_forfeit")
        self.assertEqual(final["fen"],before)
        self.assertIsNone(final["error"])

    def wait_for_second_search(self):
        pause = threading.Event()
        for _ in range(3000):
            if len(self.peer.searches) >= 2:
                return
            pause.wait(.001)
        self.fail("Extra recovery search did not begin")

    def test_clock_cut_stops_active_recovery_and_keeps_legal_reply(self):
        board = self.position()
        baseline, stronger = list(board.legal_moves)[:2]
        gate = threading.Event()
        self.queue(board,baseline.uci(),-250)
        self.queue(board,stronger.uci(),-140,gate=gate,cost_ms=0)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.game.engine_move,self.game.revision)
            self.wait_for_second_search()
            state = self.game.state()
            for delta in [-60000]*9 + [-10000]*4:
                state = self.game.adjust_clock("engine",delta,state["game_id"],state["clock"]["edit_revision"])
            self.assertTrue(state["engine"]["pacing"]["balance_recovery"]["active"])
            self.assertLess(state["engine"]["pacing"]["target_total_ms"] - state["clock"]["turn_elapsed_ms"],1000)
            self.now.advance(1)
            self.assertTrue(gate.wait(3), "Clock monitor must stop the old long analysis")
            final = future.result(5)
        self.assertGreater(self.peer.stop_calls,0)
        self.assertIsNone(final["error"])
        self.assertEqual(final["result"],"*")
        self.assertIn(final["last_move"], {baseline.uci(), stronger.uci()})
        self.assertEqual(self.waits,[])

    def test_closing_during_recovery_never_applies_late_move(self):
        board = self.position()
        baseline, stronger = list(board.legal_moves)[:2]
        before = board.fen(en_passant="fen")
        gate = threading.Event()
        self.queue(board,baseline.uci(),-250)
        self.queue(board,stronger.uci(),-140,gate=gate,cost_ms=0)
        with ThreadPoolExecutor(max_workers=2) as pool:
            future = pool.submit(self.game.engine_move,self.game.revision)
            self.wait_for_second_search()
            closing = pool.submit(self.game.close)
            pause = threading.Event()
            for _ in range(3000):
                if self.game._closing:
                    break
                pause.wait(.001)
            self.assertTrue(self.game._closing)
            self.assertTrue(gate.wait(3), "Closing must stop recovery promptly")
            future.result(5)
            closing.result(5)
        self.assertGreater(self.peer.stop_calls,0)
        self.assertEqual(self.game.board.fen(en_passant="fen"),before)
        self.assertIsNone(self.game.error)
        self.assertIsNone(self.game._engine)

    def test_unverified_deeper_reply_retains_original_baseline(self):
        board = self.position()
        baseline = next(iter(board.legal_moves))
        self.queue(board,baseline.uci(),-250)
        self.queue(board,"e2e4",200)
        state = self.game.engine_move(self.game.revision)
        self.assertIsNone(state["error"])
        self.assertEqual(state["last_move"],baseline.uci())
        self.assertEqual(self.saved_decision()["balance_recovery"]["outcome"],"unverified_rescue_baseline_retained")
        self.assertEqual(self.waits,[])

    def test_shallow_ahead_confirmation_failure_preserves_legal_baseline(self):
        board = self.position()
        baseline = next(iter(board.legal_moves))
        self.queue(board,baseline.uci(),500,packets=[info(board,baseline.uci(),500,2)])
        self.queue(board,baseline.uci(),500,failure=RuntimeError("controlled confirmation failure"))
        state = self.game.engine_move(self.game.revision)
        self.assertIsNone(state["error"])
        self.assertEqual(state["last_move"],baseline.uci())
        recovery = self.saved_decision()["balance_recovery"]
        self.assertEqual(recovery["baseline_depth"],2)
        self.assertEqual(recovery["outcome"],"rescue_failed_baseline_retained")
        self.assertEqual(self.waits,[])

    def test_shallow_ahead_confirmation_turns_behind_and_uses_strong_search(self):
        board = self.position()
        baseline, confirmed, stronger = list(board.legal_moves)[:3]
        self.queue(board,baseline.uci(),500,packets=[info(board,baseline.uci(),500,2)])
        self.queue(board,confirmed.uci(),-300)
        self.queue(board,stronger.uci(),-180)
        state = self.game.engine_move(self.game.revision)
        self.assertIsNone(state["error"])
        self.assertEqual(state["last_move"],stronger.uci())
        self.assertEqual(len(self.peer.searches),3)
        self.assertTrue(all(call["root_moves"] is None for call in self.peer.searches))
        recovery = self.saved_decision()["balance_recovery"]
        self.assertEqual(recovery["direction"],"behind")
        self.assertEqual(recovery["recovered_cp_own"],-180)
        self.assertEqual(self.waits,[])

    def test_revision_invalidation_stops_recovery_and_discards_reply(self):
        board = self.position()
        baseline, stronger = list(board.legal_moves)[:2]
        before = board.fen(en_passant="fen")
        gate = threading.Event()
        self.queue(board,baseline.uci(),-250)
        self.queue(board,stronger.uci(),-140,gate=gate,cost_ms=0)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.game.engine_move,self.game.revision)
            self.wait_for_second_search()
            # Emulate a state invalidation under the same lock used by host
            # mutations; public undo/new-game correctly reject busy requests.
            with self.game._lock:
                self.game.revision += 1
                self.game._game_token = object()
            self.assertTrue(gate.wait(3), "Stale recovery must stop promptly")
            future.result(5)
        self.assertGreater(self.peer.stop_calls,0)
        self.assertEqual(self.game.board.fen(en_passant="fen"),before)
        self.assertIsNone(self.game.error)

    def test_skip_wait_stops_recovery_but_keeps_a_legal_reply(self):
        board = self.position()
        baseline, stronger = list(board.legal_moves)[:2]
        gate = threading.Event()
        self.queue(board,baseline.uci(),-250)
        self.queue(board,stronger.uci(),-140,gate=gate,cost_ms=0)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.game.engine_move,self.game.revision)
            self.wait_for_second_search()
            result = self.game.skip_engine_wait(self.game.revision)
            self.assertTrue(result["released"])
            self.assertTrue(gate.wait(3), "Closing client release must stop recovery")
            state = future.result(5)
        self.assertGreater(self.peer.stop_calls,0)
        self.assertIsNone(state["error"])
        self.assertIn(state["last_move"],{baseline.uci(),stronger.uci()})
        self.assertEqual(self.waits,[])


if __name__ == "__main__":
    unittest.main()
