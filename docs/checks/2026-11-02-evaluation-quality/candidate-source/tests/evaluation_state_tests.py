"""Current-position evaluation: opponent search plus independent human-turn search.

Usage: python tests/evaluation_state_tests.py
Fake UCI analysis streams make partial packets, sign conventions, cancellation
boundaries and HTTP responsiveness deterministic without an engine binary.
"""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

import chess
import chess.engine

import human_play_tests as baseline


play_human = baseline.play_human


class AnalysisStream:
    def __init__(self, owner, board):
        self.owner = owner
        self.board = board.copy(stack=True)
        move = self.owner.next_move or next(iter(self.board.legal_moves))
        self.move = chess.Move.from_uci(move) if isinstance(move, str) else move
        self.info = {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def __iter__(self):
        for packet in self.owner.packets:
            if isinstance(packet, BaseException):
                raise packet
            packet = dict(packet)
            # Regicide's completed-depth UCI records include their principal
            # variation. SimpleAnalysisResult.info accumulates partial records.
            if "score" in packet and packet.get("depth", 0) >= 1:
                packet.setdefault("pv", [self.move])
            self.info.update(packet)
            yield packet
        # A real UCI search may continue thinking after its latest completed
        # depth. Keep the game lock occupied while independent readers poll.
        if self.owner.gate is not None:
            self.owner.waiting.set()
            if not self.owner.gate.wait(5):
                raise TimeoutError("test stream gate was not released")

    def wait(self):
        return chess.engine.BestMove(self.move, None)


class StreamingEngine(baseline.FakeEngine):
    def __init__(self):
        super().__init__()
        self.packets = [{"depth": 2,
                         "score": chess.engine.PovScore(chess.engine.Cp(80), chess.BLACK)}]
        self.next_move = None
        self.gate = None
        self.waiting = threading.Event()
        self.analysis_calls = []

    def analysis(self, board, limit, *, game=None, info=None):
        self.analysis_calls.append((board.copy(stack=True), limit, game, info))
        return AnalysisStream(self, board)

    def play(self, *_args, **_kwargs):
        raise AssertionError("streaming move must use exactly its analysis search")


from analysis_evaluation_tests import StreamingPeer, packet
from hint_state_tests import HintFactory, wait_for


class EvaluationStateTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="regicide-evaluation-tests-")
        self.root = Path(self.folder.name)
        self.binary = self.root / "test-peer.exe"
        self.binary.write_bytes(b"test UCI peer; never executed")
        self.peer = StreamingEngine()
        self.first = StreamingPeer([packet(35)], blocked=True)
        self.evaluators = HintFactory([self.first])
        self.game = play_human.HumanGame(self.binary, self.root / "games", lambda _: self.peer,
                                        evaluation_engine_factory=self.evaluators,
                                        opening_variety_enabled=False)
        self.addCleanup(self.folder.cleanup)
        self.addCleanup(self.game.close)
        self.assertTrue(self.first.waiting.wait(2))

    def revision(self):
        return self.game.state()["revision"]

    def human(self, move="e2e4"):
        return self.game.human_move(move, self.revision())

    def engine(self, move="e7e5", helper=None):
        self.evaluators.peers.append(helper or StreamingPeer([packet(225)]))
        self.peer.next_move = move
        return self.game.engine_move(self.revision())

    def ready(self):
        return wait_for(lambda: self.game.evaluation()
                        if self.game.evaluation()["status"] == "ready" else None)

    def assert_unknown(self, evaluation):
        for key in ("cp", "mate", "depth", "white_share", "source_fen"):
            self.assertIsNone(evaluation[key], key)

    def identity(self, evaluation, state=None):
        state = state or self.game.state()
        for key in ("game_id", "revision", "fen", "turn"):
            self.assertEqual(evaluation[key], state[key], key)
        if evaluation["cp"] is not None or evaluation["mate"] is not None:
            self.assertEqual(evaluation["source_fen"], state["fen"])

    def fresh_helper(self, peer, *, color="white", ms=100):
        self.evaluators.peers.append(peer)
        state = self.game.new_game(color, ms, self.revision())
        # A Black human initially waits for the White opponent's real search.
        if color == "black":
            self.peer.next_move = "e2e4"
            state = self.game.engine_move(state["revision"])
        return state

    def test_initial_human_turn_streams_without_hints_or_opponent_search(self):
        value = self.game.evaluation()
        self.identity(value)
        self.assertEqual(value["status"], "searching")
        self.assertEqual(value["cp"], 35)
        self.assertFalse(self.game.hint()["enabled"])
        self.assertEqual(self.peer.analysis_calls, [])
        self.assertEqual(len(self.evaluators.instances), 1)
        self.assertEqual(self.first.analysis_calls[0][1].time, 3)
        self.assertEqual(value["budget_ms"], 3000)
        self.first.release.set()
        self.assertEqual(self.ready()["cp"], 35)

    def test_engine_turn_reuses_only_opponent_search_then_fresh_human_position(self):
        self.human()
        self.assert_unknown(self.game.evaluation())
        self.assertTrue(self.first.close_event.wait(2))
        before = self.game.state()
        self.peer.next_move = "e7e5"
        self.peer.gate = threading.Event()
        fresh = StreamingPeer([packet(225)], blocked=True)
        self.evaluators.peers.append(fresh)
        with ThreadPoolExecutor(max_workers=1) as pool:
            search = pool.submit(self.game.engine_move, before["revision"])
            try:
                self.assertTrue(self.peer.waiting.wait(2))
                value = self.game.evaluation()
                self.identity(value, before)
                self.assertEqual(value["cp"], -80)
                self.assertEqual(value["depth"], 2)
                self.assertEqual(value["source"], "search")
                self.assertEqual(len(self.evaluators.instances), 1)
                self.assertFalse(search.done())
            finally:
                self.peer.gate.set()
            after = search.result(timeout=3)
        self.assertTrue(fresh.waiting.wait(2))
        value = self.game.evaluation()
        self.identity(value, after)
        self.assertEqual(value["cp"], 225)
        self.assertNotEqual(value["source_fen"], before["fen"])
        self.assertEqual(value["source"], "search")
        self.assertEqual(len(self.peer.analysis_calls), 1)
        self.assertEqual(len(self.evaluators.instances), 2)
        self.assertEqual(fresh.analysis_calls[0][0].move_stack, self.game.board.move_stack)
        self.assertFalse(self.peer.closed)

    def test_new_human_position_never_carries_previous_score_while_waiting(self):
        self.human()
        fresh = StreamingPeer([], blocked=True)
        after = self.engine(helper=fresh)
        self.assertTrue(fresh.waiting.wait(2))
        self.identity(self.game.evaluation(), after)
        self.assert_unknown(self.game.evaluation())
        self.assertEqual(self.game.evaluation()["status"], "searching")
        self.assertEqual(self.game.evaluation()["source"], "search")

    def test_white_pov_bounds_and_black_human_turn(self):
        for info, expected, bound in [
            (packet(140, pov=chess.BLACK, lowerbound=True), -140, "upper"),
            (packet(-60, pov=chess.BLACK, upperbound=True), 60, "lower"),
            (packet(125, pov=chess.WHITE), 125, "exact"),
        ]:
            with self.subTest(expected=expected):
                self.fresh_helper(StreamingPeer([info]), color="black", ms=250)
                value = self.ready()
                self.identity(value)
                self.assertEqual(value["cp"], expected)
                self.assertEqual(value["bound"], bound)
                self.assertEqual(value["turn"], "black")
                self.assertEqual(self.evaluators.instances[-1].analysis_calls[0][1].time, 3)
                self.assertEqual(value["budget_ms"], 3000)
                self.assertAlmostEqual(value["white_share"], .5 + .5 * expected / (abs(expected) + 400))

    def test_partial_packets_preserve_completed_score_depth(self):
        self.fresh_helper(StreamingPeer([
            packet(30, depth=1), packet(-75, depth=4),
            {"string": "tt probes 20"}, {"nodes": 1000, "depth": 7},
            {"score": chess.engine.PovScore(chess.engine.Cp(999), chess.WHITE)},
            packet(999, depth=8, lowerbound=True, upperbound=True),
        ]))
        value = self.ready()
        self.assertEqual(value["cp"], -75)
        self.assertEqual(value["depth"], 4)

    def test_missing_score_and_helper_failure_do_not_break_game(self):
        for peer in [StreamingPeer([packet(0, depth=0)]),
                     StreamingPeer([packet(200)], wait_error=RuntimeError("helper failed"))]:
            with self.subTest(peer=peer):
                self.fresh_helper(peer)
                wait_for(lambda: self.game.evaluation()["status"] == "unavailable")
                self.assert_unknown(self.game.evaluation())
                self.assertIsNone(self.game.state()["error"])
                self.assertFalse(self.peer.closed)

    def test_mate_sign_and_zero_keep_white_perspective_winner(self):
        for score, pov, distance, winner in [
            (chess.engine.Mate(3), chess.BLACK, -3, "black"),
            (chess.engine.Mate(2), chess.WHITE, 2, "white"),
            (chess.engine.Mate(0), chess.BLACK, 0, "white"),
            (chess.engine.MateGiven, chess.BLACK, 0, "black"),
        ]:
            with self.subTest(score=score):
                self.fresh_helper(StreamingPeer([{"depth": 3, "score": chess.engine.PovScore(score, pov)}]))
                value = self.ready()
                self.assertIsNone(value["cp"])
                self.assertEqual(value["mate"], distance)
                self.assertEqual(value["winner"], winner)
                self.assertEqual(value["white_share"], 1 if winner == "white" else 0)

    def test_plain_play_evaluator_fallback_uses_current_position(self):
        peer = baseline.FakeEngine()
        peer.replies.append(chess.engine.PlayResult(chess.Move.from_uci("e2e4"), None,
                            {"depth": 3, "score": chess.engine.PovScore(chess.engine.Cp(50), chess.WHITE)}))
        self.fresh_helper(peer)
        self.assertEqual(self.ready()["cp"], 50)
        self.identity(self.game.evaluation())
        self.assertEqual(len(peer.calls), 1)

    def test_snapshot_and_evaluation_search_preserve_board_and_archive(self):
        state, pgn = self.game.state(), self.game.pgn()
        records = {p.name: p.read_bytes() for p in self.game.save_dir.iterdir()}
        value = self.game.evaluation()
        value["revision"], value["cp"] = -1, 99999
        self.first.release.set()
        self.ready()
        self.identity(self.game.evaluation(), state)
        self.assertEqual(self.game.state(), state)
        self.assertEqual(self.game.pgn(), pgn)
        self.assertEqual({p.name: p.read_bytes() for p in self.game.save_dir.iterdir()}, records)
        self.assertEqual(self.game.evaluation()["cp"], 35)

    def test_hint_toggle_does_not_cancel_or_restart_live_evaluation(self):
        self.game._hints._factory = HintFactory()
        self.game.set_hint_enabled(True)
        wait_for(lambda: self.game.hint()["status"] == "ready")
        self.game.set_hint_enabled(False)
        self.assertFalse(self.first.closed)
        self.assertEqual(len(self.evaluators.instances), 1)
        self.assertEqual(self.game.evaluation()["cp"], 35)

    def test_late_helper_packet_cannot_replace_current_engine_turn(self):
        self.first.close_releases = False
        self.first.after_gate = [packet(9999, depth=12)]
        state = self.human()
        self.assertTrue(self.first.close_event.wait(2))
        self.first.release.set()
        wait_for(lambda: self.game._live_evaluation._cancel is None)
        self.assert_unknown(self.game.evaluation())
        self.identity(self.game.evaluation(), state)

    def test_same_fen_new_game_discards_old_generation(self):
        old = self.game.state()
        self.first.close_releases = False
        self.first.after_gate = [packet(9999)]
        fresh = StreamingPeer([packet(-90)])
        state = self.fresh_helper(fresh)
        self.assertEqual(old["fen"], state["fen"])
        self.assertNotEqual(old["game_id"], state["game_id"])
        self.assert_unknown(self.game.evaluation())
        self.first.release.set()
        self.assertEqual(self.ready()["cp"], -90)
        self.identity(self.game.evaluation(), state)

    def test_undo_restarts_from_full_current_history_and_rejects_late_score(self):
        self.human()
        old = StreamingPeer([packet(777)], blocked=True, after_gate=[packet(9999)])
        self.engine(helper=old)
        self.assertTrue(old.waiting.wait(2))
        old.close_releases = False
        fresh = StreamingPeer([packet(45)])
        self.evaluators.peers.append(fresh)
        state = self.game.undo(self.revision())
        self.assert_unknown(self.game.evaluation())
        old.release.set()
        self.assertEqual(self.ready()["cp"], 45)
        self.identity(self.game.evaluation(), state)
        searched = fresh.analysis_calls[0][0]
        self.assertEqual(searched.move_stack, self.game.board.move_stack)
        self.assertEqual(searched.root().fen(), self.game.board.root().fen())

    def test_terminal_checkmate_draw_and_resignation_override_search(self):
        cases = (
            ("white", "7k/5K2/6Q1/8/8/8/8/8 w - - 0 1", "g6g7", "white", 1, "1-0"),
            ("black", "8/8/8/8/8/6q1/5k2/7K b - - 0 1", "g3g2", "black", 0, "0-1"),
            ("white", "7k/8/8/8/8/8/r7/1K6 w - - 0 1", "b1a2", None, .5, "1/2-1/2"),
        )
        for color, fen, move, winner, share, result in cases:
            with self.subTest(result=result):
                self.game.new_game(color, 100, self.revision())
                self.game.board = chess.Board(fen)
                self.human(move)
                value = self.game.evaluation()
                self.assertEqual(value["status"], "terminal")
                self.assertEqual(value["result"], result)
                self.assertEqual(value["winner"], winner)
                self.assertEqual(value["white_share"], share)
        self.game.new_game("white", 100, self.revision())
        self.game.resign(self.revision())
        value = self.game.evaluation()
        self.assertEqual(value["status"], "terminal")
        self.assertEqual(value["winner"], "black")
        self.assertIsNone(value["mate"])

    def test_handicap_preview_pauses_and_cancel_restarts_evaluation(self):
        self.human()
        self.engine()
        self.ready()
        self.game.begin_handicap(2, self.revision())
        self.assert_unknown(self.game.evaluation())
        self.assertEqual(self.game.evaluation()["status"], "waiting")
        self.evaluators.peers.append(StreamingPeer([packet(55)]))
        self.game.cancel_handicap(self.revision())
        self.assertEqual(self.ready()["cp"], 55)
        self.identity(self.game.evaluation())

    def test_close_cancels_helper_and_never_starts_pending_generation(self):
        self.game.close()
        self.assertTrue(self.first.close_event.wait(2))
        self.assertTrue(self.game._live_evaluation._closed)
        self.assertEqual(len(self.evaluators.instances), 1)
        self.assert_unknown(self.game.evaluation())

    def test_close_during_opponent_search_cannot_restart_closed_helper(self):
        self.human()
        self.peer.next_move = "e7e5"
        self.peer.gate = threading.Event()
        with ThreadPoolExecutor(max_workers=2) as pool:
            search = pool.submit(self.game.engine_move, self.revision())
            self.assertTrue(self.peer.waiting.wait(2))
            closing = pool.submit(self.game.close)
            try:
                wait_for(lambda: self.game._live_evaluation._closed)
                before = self.game.state()
                with self.assertRaises(play_human.RevisionError):
                    self.game.set_think_time(5000, before["game_id"],
                                             before["engine"]["settings_revision"])
                self.assertEqual(self.game.movetime_ms, before["engine"]["movetime_ms"])
            finally:
                self.peer.gate.set()
            self.assertIsNone(search.result(timeout=3)["error"])
            closing.result(timeout=3)
        self.assertEqual(len(self.evaluators.instances), 1)
        self.assertTrue(self.peer.closed)
        self.assert_unknown(self.game.evaluation())

    def test_opponent_failure_and_timeout_clear_scores_without_moving(self):
        self.human()
        self.peer.packets.append(RuntimeError("synthetic stream failure"))
        state = self.engine()
        self.assertIn("synthetic stream failure", state["error"])
        self.assertEqual(self.game.evaluation()["status"], "unavailable")
        self.assert_unknown(self.game.evaluation())

    def test_hanging_opponent_still_times_out_without_applying_move(self):
        self.game.new_game("white", 100, self.revision())
        before = self.human()
        self.peer.next_move = "e7e5"
        self.peer.timeout = .01
        self.peer.gate = threading.Event()
        original_close = self.peer.close
        def close_stream():
            original_close()
            self.peer.gate.set()
        self.peer.close = close_stream
        with ThreadPoolExecutor(max_workers=1) as pool:
            move = pool.submit(self.game.engine_move, before["revision"])
            try:
                after = move.result(timeout=2)
            finally:
                self.peer.gate.set()
        self.assertIn("TimeoutError", after["error"])
        self.assertEqual(after["fen"], before["fen"])
        self.assertEqual(after["moves"], before["moves"])
        self.assert_unknown(self.game.evaluation())

    def test_http_evaluation_does_not_wait_for_game_lock(self):
        server = play_human.HumanServer(self.game)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        def get():
            req = urllib.request.Request(server.origin + "/api/evaluation",
                                         headers={"X-Regicide-Token": server.token})
            with urllib.request.urlopen(req, timeout=1) as response:
                return json.load(response)
        with ThreadPoolExecutor(max_workers=1) as pool, self.game._lock:
            value = pool.submit(get).result(timeout=2)
        self.assertEqual(value["cp"], 35)
        self.identity(value)
        with self.assertRaises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(server.origin + "/api/evaluation", timeout=1)
        self.assertEqual(denied.exception.code, 403)
        denied.exception.close()

    def test_time_setting_keeps_active_budget_and_next_position_uses_new_budget(self):
        before = self.game.state()
        self.game.set_think_time(5000, before["game_id"], before["engine"]["settings_revision"])
        self.assertEqual(len(self.evaluators.instances), 1)
        self.assertEqual(self.first.analysis_calls[0][1].time, 3)
        self.assertEqual(self.game.evaluation()["cp"], 35)
        self.assertEqual(self.game.evaluation()["budget_ms"], 3000)
        self.human()
        fresh = StreamingPeer([packet(90)])
        self.engine(helper=fresh)
        self.ready()
        self.assertEqual(self.peer.analysis_calls[-1][1].time, 5)
        self.assertEqual(fresh.analysis_calls[0][1].time, 5)
        self.assertEqual(self.game.evaluation()["budget_ms"], 5000)

    def test_human_refinement_streams_first_score_then_later_completed_depth(self):
        # A controlled stream models a quick shallow iteration followed by
        # a later completed iteration; no sleep or actual engine is required.
        self.first.after_gate = [packet(145, depth=8)]
        initial = self.game.evaluation()
        self.assertEqual(initial["cp"], 35)
        self.assertEqual(initial["status"], "searching")
        self.assertEqual(initial["budget_ms"], 3000)
        self.assertEqual(self.first.wait_calls, 0)
        self.assertEqual(len(self.evaluators.instances), 1)
        self.first.release.set()
        refined = self.ready()
        self.assertEqual((refined["cp"], refined["depth"]), (145, 8))
        self.assertEqual(refined["source_fen"], initial["source_fen"])
        self.assertEqual(refined["budget_ms"], 3000)
        self.assertEqual(len(self.first.analysis_calls), 1)
        self.assertEqual(self.peer.analysis_calls, [])

    def test_cancelled_long_helper_does_not_delay_opponent_or_publish_late_score(self):
        self.first.close_releases = False
        self.first.after_gate = [packet(9999, depth=20)]
        self.human()
        self.assertTrue(self.first.close_event.wait(2))
        fresh = StreamingPeer([packet(225)])
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                # The old three-second helper is deliberately still blocked.
                # The opponent must finish independently rather than join it.
                request = pool.submit(self.engine, helper=fresh)
                after = request.result(timeout=1)
            self.assertFalse(self.first.release.is_set())
            self.assertEqual(after["last_move"], "e7e5")
            self.assertEqual(self.peer.analysis_calls[-1][1].time, 1)
            self.assertEqual(self.game.movetime_ms, 1000)
            self.assert_unknown(self.game.evaluation())
        finally:
            self.first.release.set()
        self.assertEqual(self.ready()["cp"], 225)
        self.identity(self.game.evaluation(), after)

    def test_fast_opponent_and_ranked_hints_keep_their_budget_beside_refinement(self):
        fast = StreamingPeer([packet(20)], blocked=True)
        self.fresh_helper(fast, ms=100)
        self.assertTrue(fast.waiting.wait(2))
        self.assertEqual(fast.analysis_calls[0][1].time, 3)
        self.assertEqual(self.game.evaluation()["budget_ms"], 3000)
        self.assertEqual(self.game.hint()["movetime_ms"], 100)
        self.human()
        self.engine()
        self.assertEqual(self.peer.analysis_calls[-1][1].time, .1)
        self.assertEqual(self.game.state()["engine"]["movetime_ms"], 100)


if __name__ == "__main__":
    unittest.main(verbosity=2)
