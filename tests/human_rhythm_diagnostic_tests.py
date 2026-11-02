"""Diagnostic fixture and clock-grid checks, with no native engine calls."""
from pathlib import Path
import sys
import unittest

import chess
import chess.engine

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import check_human_rhythm as diagnostic
import engine_pacing


def packet(board, *, depth=6, move="e2e4", **extra):
    return {"depth": depth, "score": chess.engine.PovScore(chess.engine.Cp(20), board.turn),
            "pv": [chess.Move.from_uci(move)], **extra}


class RhythmDiagnosticTests(unittest.TestCase):
    def test_authored_five_positions_are_valid_and_keep_full_history(self):
        values = diagnostic.fixtures()
        self.assertEqual(len(values), 5)
        for fixture, board in values:
            replay = chess.Board(fixture["root_fen"])
            for uci in fixture["history_uci"]:
                replay.push_uci(uci)
            self.assertEqual(replay.fen(en_passant="fen"), fixture["fen"])
            self.assertTrue(board.is_valid())
        self.assertTrue(values[-1][1].move_stack)

    def test_packet_selection_matches_actual_returned_root(self):
        board = chess.Board()
        raw = packet(board)
        packets, selected = diagnostic.packet_evidence(board, [(1, raw)], "e2e4")
        self.assertEqual(selected, raw)
        selected["pv"].clear()
        self.assertTrue(raw["pv"])
        self.assertTrue(packets[6]["pv"])
        self.assertIsNone(diagnostic.packet_evidence(board, [(1, raw)], "d2d4")[1])

    def test_latest_bound_keeps_raw_evidence_and_prior_completed_forecast(self):
        board = chess.Board()
        events = [(1, packet(board, depth=6)), (2, packet(board, depth=7, lowerbound=True))]
        packets, selected = diagnostic.packet_evidence(board, events, "e2e4")
        self.assertEqual(sorted(packets), [6, 7])
        self.assertEqual(selected, events[0][1])
        self.assertTrue(packets[7]["lowerbound"])
        self.assertNotIn("lowerbound", selected)

    def test_partial_progress_does_not_replace_completed_evidence(self):
        board = chess.Board()
        raw = packet(board)
        packets, selected = diagnostic.packet_evidence(board, [(1, raw), (2, {"depth": 9, "nodes": 400})], "e2e4")
        self.assertEqual(list(packets), [6])
        self.assertEqual(selected, raw)

    def test_grid_enforces_safety_and_returns_honest_model_targets(self):
        board = chess.Board()
        packets = {depth: packet(board, depth=depth) for depth in (4, 5, 6)}
        rows = diagnostic.grid(board, packets, packets[6], chess.Move.from_uci("e2e4"), 900,
                               engine_pacing, seeds=("repeat", "another"), clocks=(600000, 180000, 60000, 10000))
        self.assertEqual(len(rows), 8)
        self.assertTrue(all(row["checks"]["allocation_unchanged"] for row in rows))
        self.assertEqual(rows[-1]["remaining_hold_ms"], 0)
        self.assertTrue(all("remaining_hold_ms" in row and "actual_wait_ms" not in row for row in rows))


if __name__ == "__main__":
    unittest.main()
