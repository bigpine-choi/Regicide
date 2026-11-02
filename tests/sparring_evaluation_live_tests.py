"""Native diagnostic endpoint and anti-lookahead regression tests."""

import sys
from pathlib import Path
import unittest

import chess
import chess.engine

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import check_sparring_evaluation as diagnostic


def packet(depth, cp=20, move="e2e4", **extra):
    return {"depth": depth, "score": chess.engine.PovScore(chess.engine.Cp(cp), chess.WHITE),
            "pv": [chess.Move.from_uci(move)], "nodes": depth * 100, **extra}


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.board = chess.Board()

    def test_baseline_uses_last_packet_before_boundary(self):
        events = [(2900, packet(7, 10)), (3000.01, packet(8, 200))]
        result = diagnostic.endpoints(self.board, events, 11980)
        self.assertEqual(result["baseline_3s"]["result"]["cp"], 10)
        self.assertEqual(result["baseline_3s"]["cutoff_ms"], 3000)

    def test_packet_exactly_on_boundary_counts(self):
        result, _ = diagnostic.at_cutoff(self.board, [(3000, packet(8, 40))], 3000)
        self.assertEqual(result["result"]["cp"], 40)

    def test_converged_before_base_stops_at_base_not_next_packet(self):
        events = [(500, packet(6)), (900, packet(7, 25)), (1200, packet(8, 30)),
                  (5000, packet(9, 500))]
        result = diagnostic.endpoints(self.board, events, 11980)
        self.assertEqual(result["adaptive"]["cutoff_ms"], 3000)
        self.assertEqual(result["adaptive"]["stop_reason"], "converged")
        self.assertEqual(result["adaptive"]["result"]["cp"], 30)

    def test_late_convergence_stops_at_actual_packet(self):
        events = [(1000, packet(6, 100)), (2500, packet(7, -100)), (3500, packet(8, 20)),
                  (4500, packet(9, 25)), (5000, packet(10, 30)), (7000, packet(11, 400))]
        result = diagnostic.endpoints(self.board, events, 11980)
        self.assertEqual(result["adaptive"]["cutoff_ms"], 5000)
        self.assertEqual(result["adaptive"]["result"]["cp"], 30)

    def test_max_budget_does_not_borrow_later_packet(self):
        events = [(1000, packet(6)), (3500, packet(8, 200)), (9999, packet(10, 0)),
                  (10000.01, packet(11, 500))]
        result = diagnostic.endpoints(self.board, events, 11980)
        self.assertEqual(result["adaptive"]["cutoff_ms"], 10000)
        self.assertEqual(result["adaptive"]["stop_reason"], "maximum_budget")
        self.assertEqual(result["adaptive"]["result"]["cp"], 0)
        self.assertEqual(result["own_reference"]["result"]["cp"], 500)

    def test_natural_early_completion_uses_final_packet(self):
        result = diagnostic.endpoints(self.board, [(50, packet(5))], 75)
        self.assertEqual(result["adaptive"]["stop_reason"], "search_completed")
        self.assertEqual(result["baseline_3s"]["cutoff_ms"], 75)
        self.assertEqual(result["adaptive"]["result"]["depth"], 5)

    def test_bound_blocks_convergence_without_replacing_exact_score(self):
        events = [(100, packet(6)), (200, packet(7)), (300, packet(8)),
                  (500, packet(9, 999, lowerbound=True))]
        result = diagnostic.endpoints(self.board, events, 11980)
        self.assertEqual(result["adaptive"]["stop_reason"], "maximum_budget")
        self.assertEqual(result["adaptive"]["result"]["cp"], 20)
        self.assertEqual(result["adaptive"]["rejected_score_packets"], 1)

    def test_partial_depth_cannot_manufacture_score(self):
        events = [(100, packet(6)), (200, {"depth": 10, "nodes": 10000})]
        result, _ = diagnostic.at_cutoff(self.board, events, 3000)
        self.assertEqual(result["result"]["depth"], 6)
        self.assertFalse(result["converged"])

    def test_later_lower_depth_does_not_replace_deeper_result(self):
        events = [(100, packet(9, 80)), (200, packet(3, -500))]
        result, _ = diagnostic.at_cutoff(self.board, events, 3000)
        self.assertEqual(result["result"]["cp"], 80)

    def test_illegal_pv_cannot_become_native_reference(self):
        result, _ = diagnostic.at_cutoff(self.board, [(100, packet(9, 500, "e2e5"))], 3000)
        self.assertIsNone(result["result"])

    def test_out_of_order_receipts_are_rejected(self):
        with self.assertRaises(ValueError):
            diagnostic.endpoints(self.board, [(200, packet(6)), (100, packet(7))], 12000)

    def test_mate_separate_from_cp_convergence(self):
        old = {"result": {"cp": 500, "mate": None, "root": "e2e4", "depth": 5}}
        reference = {"result": {"cp": None, "mate": 2, "root": "e2e4", "depth": 9, "winner": "white"}}
        value = diagnostic.compare_to_reference(old, reference)
        self.assertEqual(value["status"], "mate_separate")
        self.assertNotIn("absolute_own_cp_difference", value)

    def test_summary_reports_regressions_as_well_as_improvements(self):
        rows = []
        for index, (old, new) in enumerate(((200, 100), (10, 10), (10, 50))):
            rows.append({"position": {"id": str(index)}, "status": "ok",
                         "comparisons": {label: {"status": "cp", "absolute_own_cp_difference": cp,
                                                   "same_root": False, "same_display_band": False}
                                         for label, cp in (("baseline_3s", old), ("adaptive", new))},
                         "endpoints": {"baseline_3s": {"cutoff_ms": 3000},
                                       "adaptive": {"cutoff_ms": 10000, "stop_reason": "maximum_budget"}}})
        result = diagnostic.summarize(rows)
        self.assertEqual(result["cp_convergence_counts"], {"closer": 1, "unchanged": 1, "farther": 1})


if __name__ == "__main__":
    unittest.main()
