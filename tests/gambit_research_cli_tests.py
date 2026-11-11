"""Two-budget discovery promotion and replayable research PGN boundaries."""
import copy
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import chess
import chess.pgn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import gambit_discovery
import gambit_research as research


HISTORY = "e2e4 e7e5 g1f3 b8c6 f1c4 f8c5".split()


def candidate(*, uci="b2b4", status="screened", passed=True, cp=24, depth=7,
              unseen=True, classification="unseen_position"):
    return {"uci": uci, "status": status, "bounded_screen_passed": passed,
            "novelty": {"unseen_position": unseen, "classification": classification},
            "native_after_offer": {"status": "complete", "owner_cp": cp, "depth": depth,
                                   "pv_uci": "c5b4 c2c3 b4a5 d2d4".split()}}


def result(rows):
    return {"case_id": "fixture-evans", "history_uci": list(HISTORY), "candidates": rows}


class DiscoveryDriverTests(unittest.TestCase):
    def confirm(self, shallow, deeper=None, **changes):
        options = {"candidates": 4, "nodes": 1000, "deep_multiplier": 4,
                   "reply_limit": 0, "max_loss_cp": 60, "minimum_depth": 4, "stability_cp": 80}
        options.update(changes)
        case = {"case_id": "fixture-evans", "history_uci": list(HISTORY), "owner": "white"}
        with patch.object(gambit_discovery, "discover_case", return_value=deeper) as discover:
            answer = research.confirm_discovery(case, shallow, object(), **options)
            return answer, discover

    def test_stable_two_budget_candidate_uses_larger_independent_request(self):
        shallow = result([candidate(cp=24, depth=7)])
        deeper = result([candidate(cp=11, depth=9)])
        answer, discover = self.confirm(shallow, deeper)
        self.assertTrue(answer["candidates"][0]["two_budget_candidate"])
        self.assertEqual(answer["candidates"][0]["confirmation_reason"], "two_budget_bounded_screen_passed")
        self.assertEqual(discover.call_count, 1)
        self.assertEqual(discover.call_args.args[0]["discovery_nodes"], 4000)
        self.assertEqual(discover.call_args.args[0]["history_uci"], HISTORY)
        self.assertEqual(discover.call_args.kwargs["reply_limit"], 0)
        self.assertEqual(answer["confirmation"], deeper)

    def test_unknown_rejected_or_known_transposition_never_triggers_confirmation(self):
        rows = [candidate(status="unknown", passed=False), candidate(status="rejected", passed=False),
                candidate(unseen=False, classification="known_transposition"),
                candidate(unseen=False, classification="known_transition")]
        # A previously set flag must not survive a failed fresh screen.
        for row in rows:
            row["two_budget_candidate"] = True
        answer, discover = self.confirm(result(rows))
        discover.assert_not_called()
        self.assertTrue(all(row["two_budget_candidate"] is False for row in answer["candidates"]))
        self.assertEqual(answer["confirmation_reason"], "no_source_unseen_screened_candidate")

    def test_deeper_unknown_rejected_missing_or_transposed_candidate_cannot_pass(self):
        deeper_cases = {
            "unknown": result([candidate(status="unknown", passed=False)]),
            "rejected": result([candidate(status="rejected", passed=False)]),
            "not_revisited": result([candidate(uci="d2d4")]),
            "known_transposition": result([candidate(unseen=False, classification="known_transposition")]),
        }
        for name, deeper in deeper_cases.items():
            with self.subTest(name=name):
                answer, _discover = self.confirm(result([candidate()]), deeper)
                self.assertFalse(answer["candidates"][0]["two_budget_candidate"])

    def test_score_shift_depth_regression_or_missing_exact_score_prevents_promotion(self):
        invalid = {"score_shift": {"cp": -100}, "shallower": {"depth": 6},
                   "missing_cp": {"cp": None}, "bool_cp": {"cp": True},
                   "missing_depth": {"depth": None}, "bool_depth": {"depth": True}}
        for name, settings in invalid.items():
            with self.subTest(name=name):
                answer, _discover = self.confirm(result([candidate(cp=24, depth=7)]),
                                                 result([candidate(**settings)]))
                self.assertFalse(answer["candidates"][0]["two_budget_candidate"])

    def test_stability_boundary_is_inclusive_with_no_depth_regression(self):
        answer, _discover = self.confirm(result([candidate(cp=24, depth=7)]),
                                         result([candidate(cp=-56, depth=7)]), stability_cp=80)
        self.assertTrue(answer["candidates"][0]["two_budget_candidate"])

    def test_only_matched_survivors_can_be_promoted(self):
        rows = [candidate(uci="b2b4"), candidate(uci="d2d4", passed=False, status="unknown")]
        answer, _discover = self.confirm(result(rows), result([candidate(uci="b2b4", depth=9),
                                                               candidate(uci="d2d4", depth=9)]))
        flags = {row["uci"]: row["two_budget_candidate"] for row in answer["candidates"]}
        self.assertEqual(flags, {"b2b4": True, "d2d4": False})

    def test_pgn_roundtrip_includes_complete_standard_history_offer_and_native_line(self):
        first = candidate()
        first["two_budget_candidate"] = True
        failed = candidate(uci="d2d4", passed=False, status="unknown")
        failed["native_after_offer"]["pv_uci"] = "e5d4 f3d4".split()
        originals = result([first, failed])
        snapshot = copy.deepcopy(originals)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "discovery.pgn"
            research.export_discovery_pgn(path, [originals])
            stream = io.StringIO(path.read_text(encoding="utf-8"))
            games = [chess.pgn.read_game(stream), chess.pgn.read_game(stream)]
            self.assertIsNone(chess.pgn.read_game(stream))
        for game, row in zip(games, originals["candidates"]):
            self.assertIsNotNone(game)
            self.assertEqual(game.errors, [])
            self.assertEqual(game.board().fen(), chess.STARTING_FEN)
            self.assertEqual(game.headers["ResearchCase"], "fixture-evans")
            self.assertEqual(game.headers["ResearchStatus"], row["status"])
            self.assertEqual(game.headers["Result"], "*")
            self.assertEqual([move.uci() for move in game.mainline_moves()],
                             HISTORY + [row["uci"]] + row["native_after_offer"]["pv_uci"])
            board = chess.Board()
            for move in game.mainline_moves():
                self.assertIn(move, board.legal_moves)
                board.push(move)
            offer_node = list(game.mainline())[len(HISTORY)]
            self.assertIn("Not proof of soundness or historical novelty", offer_node.comment)
        self.assertEqual(originals, snapshot)

    def test_pgn_stops_before_illegal_native_pv_tail(self):
        row = candidate(status="unknown", passed=False)
        row["native_after_offer"]["pv_uci"] = ["c5b4", "c2c3", "a1a8"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "discovery.pgn"
            research.export_discovery_pgn(path, [result([row])])
            game = chess.pgn.read_game(io.StringIO(path.read_text()))
        self.assertEqual(game.errors, [])
        self.assertEqual([move.uci() for move in game.mainline_moves()], HISTORY + ["b2b4", "c5b4", "c2c3"])

    def test_empty_discovery_pgn_contains_no_invented_game(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "discovery.pgn"
            research.export_discovery_pgn(path, [result([])])
            self.assertEqual(path.read_text(), "")

    def test_cli_invalid_two_budget_parameters_fail_before_starting_research(self):
        cases = (["--deep-multiplier", "1"], ["--stability-cp", "-1"],
                 ["--candidates", "0"], ["--reply-limit", "-1"], ["--max-loss-cp", "-1"])
        with patch.object(research, "run", side_effect=AssertionError("must not start")):
            for options in cases:
                with self.subTest(options=options), self.assertRaises(SystemExit):
                    research.main(["discover", "--output", "unused", *options])


if __name__ == "__main__":
    unittest.main()
