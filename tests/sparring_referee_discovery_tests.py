"""Native shallow-discovery boundaries with callbacks only; no real engines."""

import copy
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import chess
import chess.engine
import sparring_referee_discovery as discovery


def packet(board, uci, cp=30, depth=4, pv=None, **extra):
    return {"depth": depth, "score": chess.engine.PovScore(chess.engine.Cp(cp), board.turn),
            "pv": [chess.Move.from_uci(item) for item in (pv if pv is not None else [uci])], **extra}


class Search:
    def __init__(self, board=None, moves=("g1f3", "b1c3", "e2e3")):
        self.board = board if board is not None else chess.Board()
        self.moves = list(moves)
        self.calls, self.packets = [], []

    def __call__(self, budget, **options):
        index = len(self.calls)
        self.calls.append((budget, options))
        move = self.moves[index]
        final = packet(self.board, move, cp=30 + index)
        self.packets.append(final)
        return chess.engine.BestMove(chess.Move.from_uci(move), None), final, {4: final}


class DiscoveryTests(unittest.TestCase):
    def discover(self, search=None, board=None, close=None, **options):
        board = board if board is not None else chess.Board()
        search = search if search is not None else Search(board)
        close = close if close is not None else Mock()
        return discovery.discover(board, search, close, excluded_uci=options.pop("excluded_uci", ["e2e4"]), **options)

    def test_three_rank_exclusion_searches_keep_separate_exact_shallow_evidence(self):
        board, search, close = chess.Board(), Search(), Mock()
        result = self.discover(search, board, close)
        self.assertEqual(result["reason"], "native_candidates_discovered")
        self.assertFalse(result["discard_engine"])
        self.assertEqual([row["uci"] for row in result["native_candidates"]], search.moves)
        self.assertEqual(result["context"], {"before_fen": board.fen(en_passant="fen"), "history_uci": [], "pov": "white"})
        self.assertEqual(result["origin"], discovery.ORIGIN)
        excluded = {"e2e4"}
        for index, (budget, options) in enumerate(search.calls):
            self.assertGreater(budget, 0)
            self.assertLessEqual(budget, 200)
            self.assertEqual(options["depth"], 4)
            self.assertIs(options["publish"], False)
            self.assertIs(options["fresh"], True)
            self.assertEqual(options["position"].fen(), board.fen())
            self.assertEqual({move.uci() for move in options["root_moves"]},
                             {move.uci() for move in board.legal_moves} - excluded)
            row = result["native_candidates"][index]
            self.assertEqual((row["line"]["cp"], row["line"]["cp_white"], row["line"]["depth"]), (30 + index, 30 + index, 4))
            self.assertIs(row["naturalness"]["admitted"], True)
            self.assertEqual(row["source"], discovery.SOURCE)
            self.assertEqual(row["line"]["bound"], "exact")
            self.assertIsNone(row["line"]["mate"])
            self.assertNotIn("loss_cp", row)
            self.assertNotIn("reference_delta_cp", row)
            excluded.add(row["uci"])
        self.assertLessEqual(sum(item[0] for item in search.calls), 600)
        close.assert_not_called()

    def test_requested_budget_is_clamped_and_split_without_extra_processes(self):
        search = Search()
        with patch("chess.engine.SimpleEngine.popen_uci", side_effect=AssertionError("no subprocess")):
            result = self.discover(search, budget_ms=5000, max_candidates=2)
        self.assertEqual(result["budget_ms"], 600)
        self.assertEqual(len(result["native_candidates"]), 2)
        self.assertEqual(len(search.calls), 2)
        self.assertLessEqual(sum(amount for amount, _ in search.calls), 600)

    def test_bad_api_limits_are_rejected_without_searching(self):
        search = Mock()
        for options in ({"budget_ms": 0}, {"budget_ms": True}, {"budget_ms": 20.0},
                        {"max_candidates": 0}, {"max_candidates": 4}, {"max_candidates": True},
                        {"cancelled": 1}):
            with self.assertRaises(ValueError):
                self.discover(search, **options)
        search.assert_not_called()

    def test_inputs_and_returned_evidence_are_independent(self):
        board = chess.Board()
        board.push_uci("e2e4")
        search = Search(board, ("g8f6", "b8c6", "e7e6"))
        before = board.fen(en_passant="fen"), list(board.move_stack)
        excluded = ["e7e5"]
        result = self.discover(search, board, excluded_uci=excluded)
        self.assertEqual((board.fen(en_passant="fen"), board.move_stack), before)
        self.assertEqual(excluded, ["e7e5"])
        self.assertEqual(result["context"]["history_uci"], ["e2e4"])
        self.assertEqual(result["native_candidates"][0]["line"]["cp_white"], -30)
        result["native_candidates"][0]["line"]["pv_uci"].clear()
        self.assertTrue(search.packets[0]["pv"])

    def malformed_packet(self, change, *, final_only=False, packet_key=4):
        def search(budget, **options):
            board, move = options["position"], "g1f3"
            common = packet(board, move)
            final = copy.deepcopy(common)
            if final_only:
                final.update(change)
            else:
                common.update(change)
                final = common
            return chess.engine.BestMove(chess.Move.from_uci(move), None), final, {packet_key: common}
        return self.discover(search, max_candidates=1)

    def test_malformed_finite_exact_depth_or_perspective_evidence_is_rejected(self):
        cases = [{"depth": 3}, {"depth": True}, {"depth": 257}, {"multipv": 2}, {"multipv": True},
                 {"lowerbound": True}, {"upperbound": True}, {"bound": "lower"}, {"score_bound": "upper"},
                 {"mate": 0}, {"score": chess.engine.PovScore(chess.engine.Mate(3), chess.WHITE)},
                 {"score": chess.engine.PovScore(chess.engine.Cp(30), chess.BLACK)},
                 {"score": chess.engine.PovScore(chess.engine.Cp(True), chess.WHITE)},
                 {"cp_white": -30}, {"cp": True}, {"cp": 30.0}, {"pov": "black"}, {"fen": "old"}]
        for change in cases:
            with self.subTest(change=change):
                result = self.malformed_packet(change)
                self.assertEqual(result["native_candidates"], [])
                self.assertEqual(result["reason"], "unreliable_candidate")

    def test_packet_stored_under_depth_four_must_really_have_depth_four(self):
        result = self.malformed_packet({"depth": 5})
        self.assertEqual(result["native_candidates"], [])
        self.assertEqual(result["reason"], "unreliable_candidate")

    def test_full_pv_and_returned_root_identity_are_checked(self):
        for pv in ([], ["b1c3"], ["g1f3", "e2e4"], ["g1f3", "e7e5", "f3f8"], ["g1f3"] * 129):
            with self.subTest(pv=pv[:3]):
                result = self.malformed_packet({"pv": [chess.Move.from_uci(uci) for uci in pv]})
                self.assertEqual(result["native_candidates"], [])
                self.assertEqual(result["reason"], "unreliable_candidate")

    def test_final_packet_cannot_borrow_the_completed_packet_score_or_depth(self):
        for changed in ({"depth": 5}, {"score": chess.engine.PovScore(chess.engine.Cp(40), chess.WHITE)},
                        {"lowerbound": True}, {"pv": [chess.Move.from_uci("b1c3")]}):
            result = self.malformed_packet(changed, final_only=True)
            self.assertEqual(result["native_candidates"], [])
        result = self.malformed_packet({}, packet_key=5)
        self.assertEqual(result["native_candidates"], [])

    def test_deeper_completed_final_is_separate_from_the_depth_four_line(self):
        def search(budget, **options):
            board = options["position"]
            common, final = packet(board, "g1f3", 30), packet(board, "g1f3", 90, depth=5)
            return chess.engine.BestMove(chess.Move.from_uci("g1f3"), None), final, {4: common, 5: final}
        result = self.discover(search, max_candidates=1)
        candidate = result["native_candidates"][0]
        self.assertEqual(candidate["line"]["cp"], 30)
        self.assertEqual(candidate["completed_final_line"]["cp"], 90)

    def test_castling_en_passant_and_underpromotion_preserve_standard_legality(self):
        for fen, uci in (("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1", "e1g1"),
                         ("4k3/8/8/3pP3/8/8/8/4K3 w - d6 0 1", "e5d6"),
                         ("4k3/P7/8/8/8/8/8/4K3 w - - 0 1", "a7a8r")):
            with self.subTest(uci=uci):
                board = chess.Board(fen)
                result = self.discover(Search(board, (uci,)), board, excluded_uci=[], max_candidates=1)
                self.assertEqual(result["native_candidates"][0]["uci"], uci)

    def test_finite_cp_cannot_certify_a_pv_that_ends_in_checkmate(self):
        pv = [chess.Move.from_uci(uci) for uci in ("f2f3", "e7e5", "g2g4", "d8h4")]
        def search(budget, **options):
            row = packet(options["position"], "f2f3", pv=[move.uci() for move in pv])
            return chess.engine.BestMove(pv[0], None), row, {4: row}
        result = self.discover(search, max_candidates=1)
        self.assertEqual(result["native_candidates"], [])
        self.assertEqual(result["reason"], "unreliable_candidate")

    def test_legal_continuation_is_validated_without_mutating_the_parent(self):
        result = self.malformed_packet({"pv": [chess.Move.from_uci(uci) for uci in ("g1f3", "e7e5", "e2e4")]})
        self.assertEqual(len(result["native_candidates"]), 1)

    def test_foreign_illegal_or_null_bestmove_discards_all_and_closes_once(self):
        for best in ("e2e4", "e2e5", "0000", None):
            close = Mock()
            def search(budget, **options):
                value = packet(options["position"], "g1f3")
                return chess.engine.BestMove(chess.Move.from_uci(best) if best else None, None), value, {4: value}
            result = self.discover(search, close=close)
            self.assertEqual(result["native_candidates"], [])
            self.assertEqual(result["reason"], "illegal_or_foreign_candidate")
            self.assertTrue(result["discard_engine"])
            close.assert_called_once()

    def test_repeated_returned_root_is_a_foreign_candidate_and_clears_prior_results(self):
        search, close = Search(moves=("g1f3", "g1f3")), Mock()
        result = self.discover(search, close=close)
        self.assertEqual(result["native_candidates"], [])
        self.assertEqual(result["reason"], "illegal_or_foreign_candidate")
        close.assert_called_once()

    def test_obvious_donation_is_excluded_and_only_natural_alternatives_are_returned(self):
        board = chess.Board("6k1/8/8/2p5/8/2P5/8/R2Q2K1 w - - 0 1")
        search = Search(board, ("d1d4", "a1a2", "g1h1"))
        result = self.discover(search, board, excluded_uci=[])
        self.assertNotIn("d1d4", [row["uci"] for row in result["native_candidates"]])
        self.assertTrue(result["native_candidates"])
        self.assertNotIn(chess.Move.from_uci("d1d4"), search.calls[1][1]["root_moves"])
        self.assertEqual(result["searches"], 3)

    def test_naturalness_admission_requires_true_and_is_not_merely_truthy(self):
        with patch.object(discovery.sparring_play, "naturalness", return_value={"admitted": 1}):
            result = self.discover(max_candidates=1)
        self.assertEqual(result["native_candidates"], [])
        self.assertEqual(result["reason"], "no_natural_candidates")

    def test_early_cancellation_never_calls_search_or_close(self):
        search, close = Mock(), Mock()
        result = self.discover(search, close=close, cancelled=lambda: True)
        self.assertEqual(result["reason"], "cancelled")
        self.assertEqual(result["native_candidates"], [])
        self.assertFalse(result["discard_engine"])
        search.assert_not_called()
        close.assert_not_called()

    def test_broken_cancellation_callback_fails_closed_without_starting_work(self):
        search = Mock()
        result = self.discover(search, cancelled=Mock(side_effect=RuntimeError("fixture")))
        self.assertEqual(result["reason"], "cancelled")
        search.assert_not_called()

    def test_cancellation_interrupts_blocked_callback_and_clears_previous_candidates(self):
        entered, cancelled, closed = threading.Event(), threading.Event(), threading.Event()
        first = Search(moves=("g1f3",))
        def search(budget, **options):
            if not first.calls:
                return first(budget, **options)
            entered.set()
            self.assertTrue(closed.wait(1), "supplied close callback must interrupt the blocked search")
            raise chess.engine.EngineTerminatedError("closed by cancellation")
        close = Mock(side_effect=closed.set)
        result = []
        worker = threading.Thread(target=lambda: result.append(self.discover(search, close=close, cancelled=cancelled.is_set)))
        worker.start()
        self.assertTrue(entered.wait(1))
        cancelled.set()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result[0]["reason"], "cancelled")
        self.assertEqual(result[0]["native_candidates"], [])
        self.assertTrue(result[0]["discard_engine"])
        close.assert_called_once()

    def test_whole_budget_plus_grace_closes_a_blocked_engine(self):
        closed = threading.Event()
        def search(budget, **options):
            self.assertTrue(closed.wait(1), "watchdog failed to close supplied engine")
            raise chess.engine.EngineTerminatedError("watchdog closed")
        close = Mock(side_effect=closed.set)
        started = time.monotonic()
        result = self.discover(search, close=close, budget_ms=40, max_candidates=1)
        self.assertGreaterEqual(time.monotonic() - started, .12)
        self.assertLess(time.monotonic() - started, .6)
        self.assertEqual(result["reason"], "timeout")
        self.assertEqual(result["native_candidates"], [])
        self.assertTrue(result["discard_engine"])
        close.assert_called_once()

    def test_late_result_before_hard_watchdog_is_still_not_accepted(self):
        def search(budget, **options):
            time.sleep(.06)
            return Search()(budget, **options)
        close = Mock()
        result = self.discover(search, close=close, budget_ms=20, max_candidates=1)
        self.assertEqual(result["reason"], "timeout")
        self.assertEqual(result["native_candidates"], [])
        self.assertTrue(result["discard_engine"])
        close.assert_called_once()

    def test_search_exception_clears_all_and_reports_engine_discard(self):
        for exception, reason in ((RuntimeError("fixture"), "search_failed"),
                                  (TimeoutError(), "timeout"), (InterruptedError(), "cancelled")):
            search, close = Mock(side_effect=exception), Mock()
            result = self.discover(search, close=close)
            self.assertEqual(result["reason"], reason)
            self.assertEqual(result["native_candidates"], [])
            self.assertTrue(result["discard_engine"])
            close.assert_called_once()

    def test_close_failure_still_reports_discard_without_exposing_error_text(self):
        result = self.discover(Mock(side_effect=TimeoutError("private fixture detail")),
                               close=Mock(side_effect=RuntimeError("private close detail")))
        self.assertEqual(result["reason"], "timeout")
        self.assertTrue(result["discard_engine"])
        self.assertNotIn("private", repr(result))

    def test_terminal_invalid_or_chess960_positions_do_not_search(self):
        positions = [chess.Board("7k/6Q1/5K2/8/8/8/8/8 b - - 0 1"), chess.Board(None),
                     chess.Board(chess.STARTING_FEN, chess960=True)]
        for board in positions:
            search = Mock()
            result = self.discover(search, board, excluded_uci=[])
            self.assertEqual(result["native_candidates"], [])
            search.assert_not_called()

    def test_all_excluded_or_malformed_exclusions_do_not_search(self):
        for excluded in ([move.uci() for move in chess.Board().legal_moves], ["e2e5"],
                         ["0000"], [True], "e2e4", None):
            search = Mock()
            result = self.discover(search, excluded_uci=excluded)
            self.assertEqual(result["native_candidates"], [])
            search.assert_not_called()

    def test_watchdog_cannot_close_after_a_successful_return(self):
        close = Mock()
        result = self.discover(close=close, budget_ms=20, max_candidates=1)
        self.assertEqual(result["reason"], "native_candidates_discovered")
        time.sleep(.14)
        close.assert_not_called()


if __name__ == "__main__":
    unittest.main()
