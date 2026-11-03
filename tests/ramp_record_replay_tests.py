"""Replay recorded exact referee comparisons through the current selector.

These are historical score packets, not fresh engine strength measurements.
"""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import chess
import sparring_referee_tests as support

ROOT = Path(__file__).resolve().parents[1]


class RecordedRampTests(unittest.TestCase):
    setUp = support.SparringRefereeTests.setUp
    peer = support.SparringRefereeTests.peer

    def test_actual_late_concessions_rejected_but_improvement_kept(self):
        data = json.loads((ROOT / "docs/checks/2026-11-03-sparring-ramp/recorded-decisions.json").read_text(encoding="utf-8"))
        for record in data["cases"]:
            with self.subTest(ply=record["ply"]):
                board = chess.Board()
                for uci in record["history_uci"]:
                    board.push_uci(uci)
                self.assertEqual(board.fen(en_passant="fen"), record["before_fen"])
                rows = {row["uci"]: row for row in record["candidates"]}
                self.assertTrue(all(chess.Move.from_uci(uci) in board.legal_moves for uci in rows))
                service, _ = self.peer()

                def replay(_engine, position, _control, _budget, **options):
                    if position.fen() == board.fen():
                        best = min(rows.values(), key=lambda row: row["multipv"])
                        return chess.Move.from_uci(best["uci"]), {best["depth"]: copy.deepcopy(rows)}, set()
                    expected = board.copy(stack=True)
                    expected.push_uci(record["original_selected_uci"])
                    self.assertEqual(position.fen(), expected.fen())
                    row = record["postmove"]
                    return chess.Move.from_uci(row["uci"]), {row["depth"]: {row["uci"]: copy.deepcopy(row)}}, set()

                with patch.object(service, "_analyse", side_effect=replay) as analysis:
                    result = service.screen_candidates(game_id="record-replay", revision=record["ply"],
                        board=board, baseline_move=record["baseline_uci"], candidates=list(rows), budget_ms=1500)
                if record["ply"] in (43, 59):
                    self.assertEqual(result["selected_uci"], record["baseline_uci"])
                    self.assertFalse(result["accepted"])
                    self.assertEqual(analysis.call_count, 1)
                    self.assertGreater(result["ramp_guard"]["floor_cp"], record["original_postmove_cp_own"])
                else:
                    self.assertEqual(result["selected_uci"], record["original_selected_uci"])
                    self.assertTrue(result["accepted"])
                    self.assertEqual(analysis.call_count, 2)


if __name__ == "__main__":
    unittest.main()
