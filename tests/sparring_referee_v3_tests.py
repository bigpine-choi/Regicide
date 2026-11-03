"""v3 pool/discovery/host contracts with controlled peers only; no native engines."""

import copy
from pathlib import Path
import sys
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import chess
import chess.engine
import sparring_referee_pool as pool
import sparring_referee_discovery as discovery
import sparring_referee_pool_tests as pool_fixtures
import sparring_referee_host_tests as host_fixtures
import sparring_state_tests as state_fixtures
from opening_variety_tests import Peer, Stream, info

host = host_fixtures.host
EARLY_HISTORY = "e2e4 e7e5 g1f3 b8c6 f1c4 g8f6 d2d3 f8c5".split()


def board_at(ply=8):
    if ply > len(EARLY_HISTORY):
        return state_fixtures.late_game_board(ply)
    board = chess.Board()
    for uci in EARLY_HISTORY[:ply]:
        board.push_uci(uci)
    return board


def partial_probe(board, uci="g1f3", cp=4, depth=4):
    return {"uci": uci, "admitted": False, "reason": "no_common_depth",
            "naturalness": {"admitted": True}, "compared_depth": None,
            "candidate_final": pool_fixtures.line(board, uci, cp, depth),
            "candidate_final_raw": {"cp": cp, "depth": depth}}


class DepthFourSearch:
    def __init__(self, board, moves):
        self.board, self.moves, self.calls = board.copy(stack=True), list(moves), []

    def __call__(self, budget, **options):
        uci = self.moves[len(self.calls)]
        self.calls.append((budget, options))
        packet = info(self.board, uci, 4, 4)
        return chess.engine.BestMove(chess.Move.from_uci(uci), None), packet, {4: packet}


class MonitorVisibleStream(Stream):
    """Keep each discovery active across at least one 20ms clock-monitor tick."""
    def __iter__(self):
        yield from super().__iter__()
        time.sleep(.05)

    def stop(self):
        self.owner.unexpected_discovery_stops += 1


class MonitorVisiblePeer(Peer):
    def __init__(self):
        super().__init__()
        self.unexpected_discovery_stops = 0

    def analysis(self, board, limit, **options):
        result = super().analysis(board, limit, **options)
        return MonitorVisibleStream(self, result.board, result.script) if options.get("root_moves") else result


class PartialProbeReuseTests(unittest.TestCase):
    def setUp(self):
        self.board = chess.Board()
        self.decision = pool_fixtures.receipt(self.board)
        self.decision["probes"] = [partial_probe(self.board)]

    def test_partial_completed_native_score_has_its_own_depth_and_no_cross_depth_loss(self):
        result = pool.build_pool(self.board, self.decision)
        self.assertEqual([row["origin"] for row in result], ["baseline", "proposal", "partial_probe"])
        evidence = result[-1]["native_evidence"]
        self.assertEqual(evidence["line"]["depth"], 4)
        self.assertEqual(evidence["baseline"]["depth"], 6)
        self.assertEqual(evidence["line"]["cp"], 4)
        self.assertFalse(evidence["native_comparison_available"])
        self.assertNotIn("loss_cp", evidence)
        self.assertNotIn("reference_delta_cp", evidence)
        self.assertNotIn("candidate_compared", evidence)
        self.assertEqual(self.decision["probes"][0]["reason"], "no_common_depth")

    def test_only_explicit_unfinished_comparison_with_natural_completed_depth_is_reused(self):
        for change in ({"reason": "candidate_failed"}, {"admitted": True},
                       {"naturalness": {"admitted": False}}, {"error": "TimeoutError"}):
            with self.subTest(change=change):
                candidate = partial_probe(self.board)
                candidate.update(change)
                decision = {**self.decision, "probes": [candidate]}
                self.assertEqual(len(pool.build_pool(self.board, decision)), 2)
        for depth in (0, 3):
            decision = {**self.decision, "probes": [partial_probe(self.board, depth=depth)]}
            self.assertEqual(len(pool.build_pool(self.board, decision)), 2)

    def test_raw_partial_evidence_must_be_exact_finite_and_match_completed_depth(self):
        for change in ({"bound": "lower"}, {"score_bound": "upper"},
                       {"lowerbound": True}, {"upperbound": True}, {"mate": 3},
                       {"mate_white": -2}, {"depth": 5}, {"cp": 5}, {"cp": 4.0}):
            with self.subTest(change=change):
                decision = copy.deepcopy(self.decision)
                decision["probes"][0]["candidate_final_raw"].update(change)
                self.assertEqual(len(pool.build_pool(self.board, decision)), 2)

    def test_foreign_or_illegal_partial_pv_never_expands_the_native_pool(self):
        for change in ({"pv_uci": ["b1c3"]}, {"pv_uci": ["g1f3", "e2e4"]},
                       {"before_fen": "foreign"}, {"mate": 1}, {"bound": "lower"}):
            with self.subTest(change=change):
                decision = copy.deepcopy(self.decision)
                decision["probes"][0]["candidate_final"].update(change)
                self.assertEqual(len(pool.build_pool(self.board, decision)), 2)


class NativeDiscoveryHostTests(unittest.TestCase):
    # Reuse only the fixture setup, not its complete inherited test suite.
    def setUp(self):
        host_fixtures.SparringRefereeHostTests.setUp(self)

    def prepare(self, ply=8, *, clock=False, baseline_only=True):
        board = board_at(ply)
        self.game.new_game("black" if board.turn else "white", 1000, self.game.revision,
                           sparring_mode=True, time_control="10+5" if clock else None)
        if clock:
            for completed in range(1, ply + 1):
                self.game._clock.complete_move("white" if completed % 2 else "black", completed)
        self.game.board = board.copy(stack=True)
        self.game._engine = self.peer
        self.game._sync_evaluation()
        moves = [move.uci() for move in board.legal_moves]
        baseline = "b1c3" if "b1c3" in moves else moves[0]
        proposal = baseline if baseline_only else next(uci for uci in moves if uci != baseline)
        decision = pool_fixtures.receipt(board, baseline, proposal)
        decision["probes"] = []
        return board, decision

    def apply(self, board, decision, search=None, close=None):
        baseline = info(board, decision["baseline_uci"], 100, 6)
        return self.game._apply_sparring_referee(board, copy.deepcopy(decision), baseline, {6: baseline},
            mock.Mock(side_effect=AssertionError("fallback must not search")),
            self.game.game_id, self.game._game_token, self.game.revision,
            discovery_search=search, close_engine=close)

    def alternatives(self, board, decision, count=3):
        excluded = {decision["baseline_uci"], decision["selected_uci"]}
        return [move.uci() for move in board.legal_moves if move.uci() not in excluded][:count]

    def test_real_discovery_path_fills_four_native_roots_and_keeps_native_and_sf_scores_separate(self):
        board, decision = self.prepare()
        search = DepthFourSearch(board, self.alternatives(board, decision))
        close = mock.Mock()
        with mock.patch.object(discovery.sparring_play, "naturalness", return_value={"admitted": True}):
            result = self.apply(board, decision, search, close)
        receipt = result["referee"]
        self.assertEqual(receipt["native_discovery"]["budget_ms"], 600)
        self.assertEqual(len(receipt["native_pool"]), 4)
        self.assertEqual(receipt["native_pool"][1]["origin"], "shallow_discovery")
        self.assertEqual(len(self.referee.screens[0]["candidates"]), 4)
        self.assertEqual(result["selected_uci"], search.moves[0])
        self.assertFalse(result["native_comparison_available"])
        self.assertIsNone(result["loss_cp"])
        self.assertIsNone(result["compared_depth"])
        self.assertEqual(result["native_candidate_depth"], 4)
        self.assertEqual(result["selected_cp_own"], 4)
        self.assertEqual(result["candidate_compared"]["depth"], 4)
        self.assertEqual(receipt["cp_white"], 777)
        self.assertEqual(result["regicide_proposal"], decision)
        self.assertLessEqual(sum(budget for budget, _ in search.calls), 600)
        for _, options in search.calls:
            self.assertEqual(options["depth"], 4)
            self.assertFalse(options["publish"])
            self.assertTrue(options["fresh"])
            self.assertEqual(options["position"].move_stack, board.move_stack)
        close.assert_not_called()

    def test_exact_three_minute_clock_boundary_allows_bounded_discovery(self):
        board, decision = self.prepare(clock=True)
        for remaining, expected in ((179_999, False), (180_000, True), (600_000, True)):
            with self.subTest(remaining=remaining):
                search = DepthFourSearch(board, self.alternatives(board, decision))
                clock = {"enabled": True, "white_ms": remaining, "black_ms": remaining}
                with mock.patch.object(self.game._clock, "snapshot", return_value=clock), \
                     mock.patch.object(discovery.sparring_play, "naturalness", return_value={"admitted": True}):
                    result = self.apply(board, decision, search, mock.Mock())
                self.assertEqual(bool(search.calls), expected)
                self.assertEqual(result["referee"]["native_discovery"] is not None, expected)

    def test_discovery_keeps_deeper_completed_native_final_without_inventing_a_depth_comparison(self):
        board, decision = self.prepare()
        roots = self.alternatives(board, decision)
        calls = []
        def search(budget, **options):
            uci = roots[len(calls)]
            calls.append((budget, options))
            common, final = info(board, uci, 4, 4), info(board, uci, 90, 5)
            return chess.engine.BestMove(chess.Move.from_uci(uci), None), final, {4: common, 5: final}
        with mock.patch.object(discovery.sparring_play, "naturalness", return_value={"admitted": True}):
            result = self.apply(board, decision, search, mock.Mock())
        evidence = result["referee"]["native_pool"][1]["native_evidence"]
        self.assertEqual(evidence["completed_final_line"]["depth"], 5)
        self.assertEqual(result["candidate_compared"]["depth"], 4)
        self.assertEqual(result["candidate_final"]["depth"], 5)
        self.assertEqual(result["native_final_depth"], 5)
        self.assertEqual(result["candidate_final"]["cp"], 90)
        self.assertEqual(result["band_control"]["selected_final_cp"], 90)
        self.assertFalse(result["native_comparison_available"])
        self.assertIsNone(result["loss_cp"])
        self.assertEqual(result["referee"]["cp_white"], 777)

    def test_before_eight_after_forty_forced_and_analysis_only_positions_do_not_expand(self):
        for ply, forced, analysis_only in ((7, False, False), (40, False, False),
                                            (8, True, False), (8, False, True)):
            with self.subTest(ply=ply, forced=forced, analysis_only=analysis_only):
                board, decision = self.prepare(ply)
                self.game._forced_plies = {1} if forced else set()
                search = mock.Mock(side_effect=AssertionError("ineligible discovery"))
                with mock.patch.object(self.game, "_sparring_analysis_only", return_value=analysis_only):
                    result = self.apply(board, decision, search, mock.Mock())
                search.assert_not_called()
                self.assertIsNone(result["referee"]["native_discovery"])
                self.assertEqual(result["selected_uci"], decision["baseline_uci"])

    def test_existing_four_root_pool_needs_no_extra_native_search(self):
        board, decision = self.prepare(baseline_only=False)
        roots = self.alternatives(board, decision, 2)
        decision["probes"] = [pool_fixtures.probe(board, uci) for uci in roots]
        search = mock.Mock(side_effect=AssertionError("already full"))
        result = self.apply(board, decision, search, mock.Mock())
        search.assert_not_called()
        self.assertEqual(len(result["referee"]["native_pool"]), 4)
        self.assertIsNone(result["referee"]["native_discovery"])

    def test_real_helper_search_exception_retains_baseline_discards_engine_and_can_restart(self):
        board, decision = self.prepare()
        close = mock.Mock(side_effect=self.peer.close)
        result = self.apply(board, decision, mock.Mock(side_effect=RuntimeError("controlled search")), close)
        self.assertEqual(result["selected_uci"], decision["baseline_uci"])
        self.assertTrue(result["referee"]["native_discovery"]["discard_engine"])
        self.assertEqual(result["referee"]["native_discovery"]["reason"], "search_failed")
        self.assertIsNone(self.game._engine)
        self.assertTrue(self.peer.closed)
        close.assert_called_once()
        replacement = Peer()
        with mock.patch.object(self.game, "_factory", return_value=replacement) as factory:
            self.game._ensure_engine()
        factory.assert_called_once_with(self.binary)
        self.assertIs(self.game._engine, replacement)

    def test_foreign_context_and_malformed_candidates_cannot_authorize_discovery_moves(self):
        board, decision = self.prepare()
        uci = self.alternatives(board, decision, 1)[0]
        row = {"uci": uci, "line": pool_fixtures.line(board, uci, 4, 4), "naturalness": {"admitted": True}}
        valid = {"before_fen": board.fen(en_passant="fen"), "history_uci": [m.uci() for m in board.move_stack], "pov": "white"}
        fixtures = [({**valid, "before_fen": "foreign"}, [row]),
                    ({**valid, "history_uci": []}, [row]), ({**valid, "pov": "black"}, [row]),
                    (valid, [None, {"uci": "e2e5"}, {**row, "line": None}]), (valid, "malformed")]
        for context, rows in fixtures:
            with self.subTest(context=context, rows=rows):
                self.game._engine = self.peer
                payload = {"context": context, "native_candidates": rows, "discard_engine": True, "reason": "controlled_failure"}
                with mock.patch.object(host, "discover_referee_candidates", return_value=payload):
                    result = self.apply(board, decision, mock.Mock(), mock.Mock())
                self.assertEqual(result["selected_uci"], decision["baseline_uci"])
                self.assertEqual(len(result["referee"]["native_pool"]), 1)
                self.assertIsNone(self.game._engine)

    def test_engine_move_discovery_survives_expired_recovery_deadline_and_old_pacing_target(self):
        self.peer = MonitorVisiblePeer()
        board, decision = self.prepare(clock=True)
        roots = self.alternatives(board, decision)
        self.peer.scripts = [{"move": decision["baseline_uci"],
                              "packets": [info(board, decision["baseline_uci"], 200, 6)]}]
        self.peer.scripts += [{"move": uci, "packets": [info(board, uci, 4, 4)]} for uci in roots]
        original_recovery = host.engine_pacing.recovery_plan

        def short_recovery(*args, **kwargs):
            result = original_recovery(*args, **kwargs)
            result["balance_recovery"].update(active=True, direction="ahead", baseline_depth=6,
                                               budget_ms=1, reason="controlled_expiring_recovery")
            result["target_total_ms"] = 0
            return result

        def choose(position, best, final, packets, search, close, **options):
            # This runs after the host established recovery_deadline. The
            # independent discovery budget must outlive that expired phase.
            self.assertEqual(options["movetime_ms"], 0)
            time.sleep(.025)
            self.game._active_pacing["clock_adjusted"] = True
            self.game._active_pacing["target_total_ms"] = 0
            return copy.deepcopy(decision), False

        # This fixture supplies only discovery packets. The independent final
        # safety gate has its own host tests with child/root/child packets.
        with mock.patch.object(host.sparring_safety, "inspect", return_value={"risky": False}), \
             mock.patch.object(host.engine_pacing, "recovery_plan", side_effect=short_recovery), \
             mock.patch.object(host.sparring_play, "choose", side_effect=choose), \
             mock.patch.object(discovery.sparring_play, "naturalness", return_value={"admitted": True}), \
             mock.patch.object(self.game, "_wait_for_engine_turn", return_value=None):
            state = self.game.engine_move(self.game.revision)
        self.assertIsNone(state["error"])
        self.assertEqual(state["last_move"], roots[0])
        self.assertEqual(len(self.peer.searches), 4)
        self.assertEqual(self.peer.unexpected_discovery_stops, 0)
        self.assertFalse(self.peer.closed)
        receipt = self.game._sparring_decisions[len(board.move_stack) + 1]
        self.assertGreater(receipt["balance_recovery"]["elapsed_ms"], 1)
        self.assertEqual(receipt["referee"]["native_discovery"]["reason"], "native_candidates_discovered")
        self.assertEqual(receipt["referee"]["native_discovery"]["budget_ms"], 600)
        self.assertEqual(receipt["selected_cp_own"], 4)
        self.assertEqual(receipt["referee"]["cp_white"], 777)
        for search in self.peer.searches[1:]:
            self.assertEqual(search["limit"].depth, 4)
            self.assertGreater(search["limit"].time, 0)
            self.assertEqual(search["board"].move_stack, board.move_stack)


if __name__ == "__main__":
    unittest.main()
