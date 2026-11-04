"""Candidate comparison evidence contracts; real searches are separate."""
import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import chess
import chess.engine

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import gambit_candidate_probe as tool
import probe_loss_search as legacy


def packet(move="e2e4", depth=4, **changes):
    value = {"depth": depth, "nodes": 10, "pv": [chess.Move.from_uci(move)],
             "score": chess.engine.PovScore(chess.engine.Cp(15), chess.WHITE)}
    return {**value, **changes}


class Future:
    def result(self, timeout=None):
        return 0


class Analysis:
    def __init__(self, packets, returned):
        self.packets, self.returned = packets, returned
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def __iter__(self):
        return iter(self.packets)
    def wait(self):
        return chess.engine.PlayResult(chess.Move.from_uci(self.returned), None)


class Engine:
    id = {"name": "contract fake"}
    def __init__(self, packets=None, returned="e2e4"):
        checks = ["UseLMR", "UsePositionalEvaluation", "UsePieceActivity", "UseNeuralEvaluation",
                  "UseNNUEEvaluation", "PVS"]
        self.options = {name: chess.engine.Option(name, "check", True, None, None, None) for name in checks}
        self.options["Hash"] = chess.engine.Option("Hash", "spin", 16, 1, 256, None)
        self.packets, self.returned = packets or [packet()], returned
        self.configured, self.calls, self.closed, self.returncode = {}, [], False, Future()
    def configure(self, options):
        self.configured.update(options)
    def ping(self):
        pass
    def analysis(self, board, limit, **kwargs):
        self.calls.append((board.copy(stack=True), limit, kwargs))
        return Analysis(self.packets, self.returned)
    def quit(self):
        self.closed = True
    def close(self):
        self.closed = True


class CandidateProbeTests(unittest.TestCase):
    def test_manifest_preserves_history_and_rejects_corruption_duplicates_and_illegal_roots(self):
        board = chess.Board()
        board.push_uci("e2e4")
        case = {"id": "black", "initial_fen": chess.STARTING_FEN, "history_uci": ["e2e4"],
                "before_fen": board.fen(en_passant="fen"), "candidates": ["e7e5"]}
        self.assertEqual(tool.validate_manifest({"cases": [case]}), [case])
        for changes in ({"history_uci": []}, {"candidates": ["e7e5", "e7e5"]}, {"candidates": ["e2e4"]}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                tool.validate_manifest({"cases": [{**case, **changes}]})
        with self.assertRaises(ValueError):
            tool.validate_manifest({"cases": [case, case]})

    def test_lmr_is_only_configured_difference_and_classical_activity_is_explicit(self):
        first, second = Engine(), Engine()
        on, off = tool.configure(first, True), tool.configure(second, False)
        self.assertEqual({key for key in on if on[key] != off[key]}, {"UseLMR"})
        self.assertTrue(on["UsePositionalEvaluation"] and on["UsePieceActivity"])
        self.assertFalse(on["UseNeuralEvaluation"] or on["UseNNUEEvaluation"])
        del second.options["UseLMR"]
        with self.assertRaisesRegex(ValueError, "UseLMR"):
            tool.configure(second, False)

    def test_exact_evidence_rejects_bounds_wrong_pov_multipv_and_illegal_pv(self):
        board = chess.Board()
        self.assertIsNotNone(tool.validate_packet(board, packet(), node_cap=10))
        for changes in ({"lowerbound": True}, {"upperbound": True}, {"multipv": 2},
                        {"score": chess.engine.PovScore(chess.engine.Cp(1), chess.BLACK)},
                        {"depth": True}, {"nodes": 11}, {"pv": []},
                        {"pv": [chess.Move.from_uci("e2e4"), chess.Move.from_uci("d2d4")]}):
            self.assertIsNone(tool.validate_packet(board, packet(**changes), node_cap=10))
        self.assertIsNone(tool.validate_packet(board, packet(), chess.Move.from_uci("d2d4"), 10))

    def test_query_sends_same_root_searchmoves_and_depth_node_limits_then_closes(self):
        engine, board = Engine(), chess.Board()
        before = board.fen(), list(board.move_stack)
        row = tool.query(Path("unused"), board, "e2e4", 4, 100, 10, True, lambda *_: engine)
        self.assertEqual(row["status"], "complete")
        saved, limit, options = engine.calls[0]
        self.assertEqual(saved.fen(), board.fen())
        self.assertEqual((limit.depth, limit.nodes), (4, 100))
        self.assertEqual(options["root_moves"], [chess.Move.from_uci("e2e4")])
        self.assertEqual(before, (board.fen(), list(board.move_stack)))
        self.assertTrue(row["engine_closed"] and row["position_unchanged"] and engine.closed)
        self.assertEqual(row["process_exit_code"], 0)
        self.assertTrue(tool.query_passed(row))

    def test_incomplete_or_wrong_returned_root_cannot_be_ranked(self):
        incomplete = Engine([packet(depth=3)])
        row = tool.query(Path("unused"), chess.Board(), "e2e4", 4, 100, 10, True, lambda *_: incomplete)
        self.assertEqual(row["status"], "incomplete_requested_depth")
        wrong = Engine(returned="d2d4")
        row = tool.query(Path("unused"), chess.Board(), "e2e4", 4, 100, 10, True, lambda *_: wrong)
        self.assertEqual(row["status"], "failed")
        self.assertTrue(wrong.closed)

    def test_node_overrun_is_retained_as_failure(self):
        engine = Engine([packet(nodes=101)])
        row = tool.query(Path("unused"), chess.Board(), None, 4, 100, 10, False, lambda *_: engine)
        self.assertIn("node cap", row["error"])
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["packets"][0]["nodes"], 101)

    def test_full_completion_required_for_cross_candidate_comparison(self):
        rows = [{"requested_depth": 4, "exact_by_depth": {3: {}, 4: {}}, "status": "complete",
                 "lmr": True, "root_uci": None},
                {"requested_depth": 4, "exact_by_depth": {3: {}}, "status": "incomplete_requested_depth",
                 "lmr": True, "root_uci": "e2e4"}]
        value = tool.compare_depth(rows, 4)
        self.assertFalse(value["all_completed"])
        self.assertEqual(value["common_exact_depth"], 3)
        self.assertTrue(all(row["score"] is None for row in value["values"]))

    def test_running_watchdog_callback_is_joined_and_invalidates_completed_result(self):
        timers = []
        class AlreadyRunningTimer:
            def __init__(self, seconds, callback):
                self.callback, self.joined, self.cancelled = callback, False, False
                timers.append(self)
            def start(self):
                pass
            def cancel(self):
                self.cancelled = True
            def join(self):
                # Model a callback already running when cancel() was called.
                self.callback()
                self.joined = True
        engine = Engine()
        with patch.object(tool.threading, "Timer", AlreadyRunningTimer):
            row = tool.query(Path("unused"), chess.Board(), "e2e4", 4, 100, 10, True, lambda *_: engine)
        self.assertTrue(timers[0].cancelled and timers[0].joined)
        self.assertEqual(row["completed_line"]["depth"], 4)
        self.assertTrue(row["safety_timeout_expired"])
        self.assertEqual(row["status"], "failed")
        self.assertFalse(tool.query_passed(row))
        self.assertFalse(tool.compare_depth([row], 4)["all_completed"])

    def test_abnormal_exit_is_recorded_and_cannot_pass_completed_query(self):
        engine = Engine()
        with patch.object(engine.returncode, "result", return_value=-9):
            row = tool.query(Path("unused"), chess.Board(), "e2e4", 4, 100, 10, True, lambda *_: engine)
        self.assertEqual(row["completed_line"]["depth"], 4)
        self.assertTrue(row["engine_closed"])
        self.assertEqual(row["process_exit_code"], -9)
        self.assertEqual(row["status"], "failed")
        self.assertFalse(tool.query_passed(row))
        self.assertFalse(tool.compare_depth([row], 4)["all_completed"])

    def test_static_parser_retains_legacy_13_and_validates_new_17_columns(self):
        board = chess.Board()
        fields = [board.fen(en_passant="fen"), "w", "0", "1", "2", "3", "4", "20", "0", "0", "0", "ongoing", "1"]
        legacy_row = legacy.parse_static_probe("\t".join(fields) + "\n", [board])[0]
        self.assertNotIn(legacy.ACTIVITY_SCORE_FIELDS[0], legacy_row)
        active = legacy.parse_static_probe("\t".join([*fields, "5", "6", "7", "8"]) + "\n", [board])[0]
        self.assertEqual(active[legacy.ACTIVITY_SCORE_FIELDS[-1]], 8)
        with self.assertRaises(ValueError):
            legacy.parse_static_probe("\t".join([*fields, "5", "6", "7"]) + "\n", [board])

    def test_new_mode_dispatch_does_not_invoke_legacy_required_arguments(self):
        with patch.object(tool, "main", return_value=17) as mocked:
            self.assertEqual(legacy.main(["--root-candidates", "manifest.json"]), 17)
        mocked.assert_called_once_with(["--root-candidates", "manifest.json"])


if __name__ == "__main__":
    unittest.main()
