"""Search budget experiment contracts, independent of timed engine results."""
import copy
import json
from pathlib import Path
import sys
import unittest

import chess
import chess.engine

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import check_gambit_search_budget as diagnostic


class Done:
    def done(self):
        return True


class Analysis:
    def __init__(self, move, fail=False):
        self.move, self.fail = move, fail

    def __enter__(self):
        if self.fail:
            raise RuntimeError("continuation failed")
        return self

    def __exit__(self, *args):
        pass

    def __iter__(self):
        yield {"depth": 4, "pv": [self.move],
               "score": chess.engine.PovScore(chess.engine.Cp(10), chess.WHITE)}

    def wait(self):
        return chess.engine.PlayResult(self.move, None)


class Engine:
    id = {"name": "contract fake"}

    def __init__(self, fail_second=False):
        self.calls, self.fail_second, self.closed = [], fail_second, False
        self.returncode = Done()

    def analysis(self, board, limit, **kwargs):
        self.calls.append((limit.time, kwargs["game"], board.fen(), list(board.move_stack)))
        return Analysis(next(iter(board.legal_moves)), self.fail_second and len(self.calls) == 2)

    def quit(self):
        self.closed = True

    def close(self):
        self.closed = True


class BudgetTests(unittest.TestCase):
    def test_all_four_saved_failures_and_legal_history_are_retained(self):
        report = json.loads(diagnostic.DEFAULT_REPORT.read_text(encoding="utf-8"))
        rows = diagnostic.known_failures(report)
        self.assertEqual({row["id"] for row in rows}, {"kings-accepted:9", "kings-accepted:11",
            "morra-developed-defense:25", "budapest-accepted:16"})
        for row in rows:
            self.assertEqual(diagnostic.board_for(row).fen(en_passant="fen"), row["before_fen"])

    def test_corrupt_full_history_is_rejected_before_search(self):
        report = json.loads(diagnostic.DEFAULT_REPORT.read_text(encoding="utf-8"))
        row = diagnostic.known_failures(report)[0]
        row["history_uci"] = row["history_uci"][:-1]
        with self.assertRaises(ValueError):
            diagnostic.board_for(row)

    def test_two_stage_keeps_token_control_is_fresh_and_total_is_equal(self):
        owned = []
        def factory():
            owned.append(Engine())
            return owned[-1]
        board = chess.Board()
        before = board.fen(), list(board.move_stack)
        result = diagnostic.compare_searches(factory, board, 1000, 750, diagnostic.load_audit().safe)
        self.assertEqual([call[0] for call in owned[0].calls], [1., .75])
        self.assertEqual([call[0] for call in owned[1].calls], [1.75])
        self.assertIs(owned[0].calls[0][1], owned[0].calls[1][1])
        self.assertIsNot(owned[0].calls[0][1], owned[1].calls[0][1])
        self.assertEqual(before, (board.fen(), list(board.move_stack)))
        self.assertTrue(all(engine.closed for engine in owned))
        self.assertTrue(all(query["position_unchanged"] for query in result["queries"]))
        self.assertTrue(all(query["completed_line"]["depth"] == 4 for query in result["queries"]))

    def test_failed_continuation_preserves_first_legal_move_and_error(self):
        owned = []
        def factory():
            owned.append(Engine(fail_second=True))
            return owned[-1]
        result = diagnostic.compare_searches(factory, chess.Board(), 1000, 750, diagnostic.load_audit().safe)
        self.assertTrue(result["continuation_fallback_to_baseline"])
        self.assertEqual(result["selected"]["continued"], result["selected"]["baseline"])
        self.assertIn("continuation failed", result["queries"][1]["error"])

    def test_controls_follow_fixture_order_without_using_audit_answers(self):
        report = json.loads(diagnostic.DEFAULT_REPORT.read_text(encoding="utf-8"))
        failures = diagnostic.known_failures(report)
        fixtures = json.loads(diagnostic.DEFAULT_FIXTURES.read_text(encoding="utf-8"))
        controls = diagnostic.declared_controls(fixtures, failures, 4)
        self.assertEqual(len(controls), 4)
        self.assertTrue({tuple(row["history_uci"]) for row in controls}.isdisjoint(
            {tuple(row["history_uci"]) for row in failures}))
        self.assertEqual(controls[0]["id"], "control:" + fixtures["cases"][0]["id"])

    def test_audit_compares_all_arms_at_shared_depth_and_preserves_mates(self):
        class Helper:
            safe = staticmethod(lambda value: value)
            def __init__(self):
                self.index = 0
            def query(self, *args):
                scores = [chess.engine.Cp(100), chess.engine.Cp(20), chess.engine.Mate(-2), chess.engine.Cp(40)]
                value = scores[self.index]
                self.index += 1
                move = args[2] or chess.Move.from_uci("e2e4")
                # Unequal latest depths must not be mixed.
                return {"bestmove": move.uci(), "packets": {
                    4: {"score": chess.engine.PovScore(value, chess.WHITE), "depth": 4, "pv": [move]},
                    4 + self.index: {"score": chess.engine.PovScore(chess.engine.Cp(999), chess.WHITE),
                                     "depth": 4 + self.index, "pv": [move]}}}
        result = diagnostic.audit_selected(None, chess.Board(), {"baseline": "e2e4",
            "continued": "d2d4", "single_equal_total": "g1f3"}, Helper())
        self.assertEqual(result["depth"], 4)
        self.assertEqual(result["loss_vs_free_cp"]["baseline"], 80)
        self.assertIsNone(result["loss_vs_free_cp"]["continued"])
        self.assertEqual(result["scores"]["continued"]["mate_own"], -2)

    def test_packet_rejects_wrong_pov_bounds_multipv_and_illegal_pv(self):
        board = chess.Board()
        packet = {"depth": 4, "pv": [chess.Move.from_uci("e2e4")],
                  "score": chess.engine.PovScore(chess.engine.Cp(10), chess.WHITE)}
        self.assertTrue(diagnostic.exact_packet(board, packet))
        for change in ({"score": chess.engine.PovScore(chess.engine.Cp(10), chess.BLACK)},
                       {"lowerbound": True}, {"upperbound": True}, {"multipv": 2},
                       {"depth": True}, {"pv": [chess.Move.from_uci("e2e4"), chess.Move.from_uci("d2d4")]}):
            self.assertFalse(diagnostic.exact_packet(board, {**packet, **change}))
        self.assertFalse(diagnostic.exact_packet(board, packet, chess.Move.from_uci("d2d4")))

    def test_identical_choices_share_one_audit_query(self):
        class Helper:
            safe = staticmethod(lambda value: value)
            def __init__(self):
                self.calls = 0
            def query(self, engine, board, move, *args):
                self.calls += 1
                move = move or chess.Move.from_uci("e2e4")
                return {"bestmove": move.uci(), "packets": {4: {"depth": 4, "pv": [move],
                    "score": chess.engine.PovScore(chess.engine.Cp(20), board.turn)}}}
        helper = Helper()
        result = diagnostic.audit_selected(None, chess.Board(), dict.fromkeys(
            ["baseline", "continued", "single_equal_total"], "e2e4"), helper)
        self.assertEqual(helper.calls, 2)
        self.assertEqual(len(result["queries"]), 2)
        self.assertEqual(set(result["loss_vs_free_cp"].values()), {0})

    def test_restricted_bestmove_disagreement_prevents_audit_comparison(self):
        class Helper:
            safe = staticmethod(lambda value: value)
            def query(self, engine, board, move, *args):
                move = move or chess.Move.from_uci("e2e4")
                return {"bestmove": "d2d4", "packets": {4: {"depth": 4, "pv": [move],
                    "score": chess.engine.PovScore(chess.engine.Cp(20), board.turn)}}}
        result = diagnostic.audit_selected(None, chess.Board(), {"baseline": "e2e4"}, Helper())
        self.assertEqual(result["status"], "no_common_depth")

    def test_requested_audit_without_common_depth_cannot_pass(self):
        result = {"sources_unchanged": True, "all_owned_engines_closed": True, "results": [{
            "selected": {"baseline": "e2e4", "continued": "e2e4", "single_equal_total": "e2e4"},
            "queries": [{"position_unchanged": True, "error": None} for _ in range(3)],
            "audit": {"status": "no_common_depth"}}]}
        status = diagnostic.completion_status(result, 1, require_audit=True)
        self.assertTrue(status["native_completed"])
        self.assertFalse(status["audit_completed"])
        self.assertFalse(status["passed"])
        result["results"][0]["audit"]["status"] = "exact_common_depth"
        self.assertTrue(diagnostic.completion_status(result, 1, require_audit=True)["passed"])

    def test_missing_case_failed_search_and_missing_audit_stay_separate(self):
        owned = []
        def factory():
            owned.append(Engine(fail_second=True))
            return owned[-1]
        row = diagnostic.compare_searches(factory, chess.Board(), 1000, 750, diagnostic.load_audit().safe)
        result = {"sources_unchanged": True, "all_owned_engines_closed": True, "results": [row]}
        status = diagnostic.completion_status(result, 1)
        self.assertFalse(status["native_completed"])
        self.assertIsNone(status["audit_completed"])
        self.assertFalse(status["passed"])
        result["results"] = []
        self.assertFalse(diagnostic.completion_status(result, 1)["passed"])


if __name__ == "__main__":
    unittest.main()
