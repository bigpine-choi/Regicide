"""Promotion statistics, reserve separation and independent rule replay tests."""
import copy
import json
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import chess
import learning_arena as arena
from see_clean_guard import EngineCpuGuard, ProcessInfo


def rule_game(number, pair, moves, candidate_white, opening_moves=None):
    opening_moves = opening_moves or []
    board = chess.Board()
    searched = []
    for ply, move in enumerate(moves):
        if ply >= len(opening_moves):
            searched.append({"move": move, "elapsed_seconds": .001,
                             "role": "candidate" if board.turn == candidate_white else "baseline",
                             "fen_before": board.fen(en_passant="fen")})
        board.push_uci(move)
    outcome = board.outcome(claim_draw=True)
    return {"game": number, "pair": pair, "white": "candidate" if candidate_white else "baseline",
            "black": "baseline" if candidate_white else "candidate", "opening": "fixture",
            "opening_uci_moves": opening_moves, "initial_fen": chess.STARTING_FEN,
            "uci_moves": moves, "plies": len(moves), "final_fen": board.fen(en_passant="fen"),
            "search_moves": searched,
            "result": outcome.result() if outcome else "*", "completed": outcome is not None,
            "termination": outcome.termination.name.lower() if outcome else "ply_cap", "error": None,
            "candidate_points": (.5 if outcome.winner is None else
                float((outcome.winner == chess.WHITE) == candidate_white)) if outcome else None}


def replay_fixture():
    # Candidate wins with either color. These are rule replay fixtures, not engine
    # performance evidence and never passed to production promotion without mocks.
    black_win = "f2f3 e7e5 g2g4 d8h4".split()
    white_win = "e2e4 f7f6 d2d4 g7g5 d1h5".split()
    games = [rule_game(1, 1, white_win, True), rule_game(2, 1, black_win, False)]
    return {"schema": arena.SCHEMA, "draw_policy": "claim", "errors": [], "completed": True,
            "resource_guard": {"valid_for_comparison": True, "contaminated": False},
            "movetime_ms": 100, "hash_mb": 16, "threads": 1, "requested_pairs": 1,
            "move_response_allowance_seconds": .2,
            "attempt_index": 1, "opening_reserve": {"openings": [{"moves": []}]}, "games": games,
            "decision": arena.promotion_decision([1], 1, complete=True, clean=True)}


class PromotionStatisticsTests(unittest.TestCase):
    def test_all_wins_small_smoke_cannot_promote(self):
        self.assertFalse(arena.promotion_decision([1] * 31, 1, complete=True, clean=True)["passed"])

    def test_strong_32_pair_result_can_pass(self):
        result = arena.promotion_decision([1] * 32, 1, complete=True, clean=True)
        self.assertTrue(result["passed"])
        self.assertAlmostEqual(result["attempt_alpha"], .025)

    def test_short_move_time_is_ineligible_even_when_all_games_win(self):
        result = arena.promotion_decision([1] * 32, 1, complete=True, clean=True, movetime_ms=99)
        self.assertFalse(result["eligible"])
        self.assertFalse(result["passed"])

    def test_even_score_never_passes(self):
        self.assertFalse(arena.promotion_decision([.5] * 1000, 1, complete=True, clean=True)["passed"])

    def test_dirty_or_incomplete_wins_cannot_pass(self):
        for complete, clean in ((False, True), (True, False), (False, False)):
            self.assertFalse(arena.promotion_decision([1] * 100, 1, complete=complete, clean=clean)["passed"])

    def test_alpha_budget_is_summable(self):
        values = [arena.promotion_decision([], index, complete=False, clean=False)["attempt_alpha"]
                  for index in range(1, 10001)]
        self.assertLess(sum(values), .05)
        self.assertGreater(sum(values), .0499)
        self.assertLess(values[-1], values[0])

    def test_invalid_attempts_and_scores_rejected(self):
        for value in (0, -1, 1.5, True):
            with self.assertRaises(ValueError):
                arena.promotion_decision([], value, complete=True, clean=True)
        with self.assertRaises(ValueError):
            arena.promotion_decision([.8], 1, complete=True, clean=True)


class ReserveTests(unittest.TestCase):
    def test_generation_is_reproducible_separate_and_legal(self):
        first = arena.load_openings(arena.DEFAULT_OPENINGS, 32, 1234)
        self.assertEqual(first, arena.load_openings(arena.DEFAULT_OPENINGS, 32, 1234))
        training = json.loads((ROOT / "tests/data/learning_openings_v1.json").read_text(encoding="utf-8"))
        starts = {entry["moves"][0] for entry in training["openings"]}
        for opening in first["openings"]:
            self.assertEqual(len(opening["moves"]), 12)
            self.assertNotIn(opening["moves"][0], starts)
            board = chess.Board()
            for move in opening["moves"]:
                board.push_uci(move)
            self.assertEqual(board.fen(en_passant="fen"), opening["fen_after_opening"])
            self.assertIsNone(board.outcome(claim_draw=True))

    def test_random_seed_changes_declared_schedule(self):
        self.assertNotEqual(arena.load_openings(arena.DEFAULT_OPENINGS, 32, 1)["openings"],
                            arena.load_openings(arena.DEFAULT_OPENINGS, 32, 2)["openings"])


class RuleEvidenceTests(unittest.TestCase):
    def test_full_history_replay_recovers_scores(self):
        self.assertEqual(arena.verify_match_records(replay_fixture())["candidate_pair_score"], 1)

    def test_fake_pass_flag_rejected(self):
        item = replay_fixture()
        item["decision"]["passed"] = True
        with self.assertRaisesRegex(ValueError, "confidence"):
            arena.verify_match_records(item)

    def test_result_and_points_cannot_be_edited(self):
        for field, value in (("result", "0-1"), ("candidate_points", 0),
                             ("termination", "stalemate"), ("final_fen", chess.STARTING_FEN)):
            item = replay_fixture()
            item["games"][0][field] = value
            with self.assertRaises(ValueError):
                arena.verify_match_records(item)


class NativeGuardIdentityTests(unittest.TestCase):
    def provider(self):
        class Provider:
            engine_names = {"regicide.exe", "whale.exe"}
            processes = {os.getpid(): ProcessInfo(os.getpid(), 0, "python.exe", 100, 0),
                         998001: ProcessInfo(998001, os.getpid(), "regicide.exe", 200, 0),
                         998002: ProcessInfo(998002, os.getpid(), "regicide.exe", 201, 0)}

            def snapshot(self):
                return dict(self.processes)

            def is_engine(self, process):
                return process.name in self.engine_names
        return Provider()

    def test_only_two_registered_native_transport_identities_are_exempt(self):
        provider = self.provider()
        guard = EngineCpuGuard(provider, "unused-unit-test-output.jsonl")
        guard.register_transport(998001, "baseline", ["regicide.exe", "uci"])
        guard.register_transport(998002, "candidate", ["regicide.exe", "uci"])
        guard.assert_transports()
        provider.processes[998003] = ProcessInfo(998003, os.getpid(), "regicide.exe", 202, 0)
        guard.sample("unexpected-third-child")
        self.assertTrue(guard.summary["contaminated"])

    def test_external_browser_cpu_increase_invalidates_whole_match(self):
        provider = self.provider()
        provider.processes[998100] = ProcessInfo(998100, 0, "whale.exe", 90, 100)
        guard = EngineCpuGuard(provider, "unused-unit-test-output.jsonl")
        guard.sample("initial")
        provider.processes[998100] = ProcessInfo(998100, 0, "whale.exe", 90, 101)
        guard.sample("browser-became-active")
        self.assertTrue(guard.summary["contaminated"])
        self.assertEqual(guard.summary["external_cpu_increase_ticks"], 1)

    def test_inaccessible_external_engine_fails_closed(self):
        provider = self.provider()
        provider.processes[998100] = ProcessInfo(998100, 0, "regicide.exe", error="access denied")
        guard = EngineCpuGuard(provider, "unused-unit-test-output.jsonl")
        guard.sample("inaccessible-process")
        self.assertTrue(guard.summary["contaminated"])

    def test_illegal_or_post_mate_moves_rejected(self):
        for move in ("a1a8", "a7a6"):
            item = replay_fixture()
            item["games"][0]["uci_moves"].append(move)
            with self.assertRaises(ValueError):
                arena.verify_match_records(item)

    def test_unfinished_game_is_never_draw(self):
        item = replay_fixture()
        item["games"][0] = rule_game(1, 1, ["e2e4"], True)
        with self.assertRaises(ValueError):
            arena.verify_match_records(item)

    def test_wrong_pair_or_opening_rejected(self):
        for field, value in (("pair", 2), ("white", "baseline"), ("opening_uci_moves", ["a2a3"])):
            item = replay_fixture()
            item["games"][0][field] = value
            with self.assertRaises(ValueError):
                arena.verify_match_records(item)


if __name__ == "__main__":
    unittest.main()
