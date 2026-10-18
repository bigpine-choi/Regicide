"""Rejection-only fixed-final-bound stopping; no invented games or weaker gate."""

import contextlib
import copy
import io
import itertools
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import chess
import chess.engine
import learning_arena as ARENA
import learning_registry as REGISTRY
from collect_learning_data import OPTIONS
from learning_arena_tests import rule_game

VALUES = (0, .25, .5, .75, 1)
BLACK_WIN = "f2f3 e7e5 g2g4 d8h4".split()
WHITE_WIN = "e2e4 f7f6 d2d4 g7g5 d1h5".split()


class MockGuard:
    instances = []
    dirty = False

    def __init__(self, provider, output, interval):
        self.output = output
        self.summary = {"contaminated": False, "failure": None, "external_cpu_increase_ticks": 0,
                        "scope": "explicit mocked resource guard for orchestration test"}
        self.registered = []
        self.__class__.instances.append(self)

    def start(self):
        self.output.write_text('{"mocked":true}\n')

    def register_transport(self, pid, role, argv):
        self.registered.append((pid, role))

    def assert_transports(self):
        if len(self.registered) != 2 or len({pid for pid, _ in self.registered}) != 2:
            raise RuntimeError("bad test transports")

    def sample(self, phase):
        if self.dirty and phase == "after-complete-pair-before-futility":
            self.summary.update(contaminated=True, failure={"reason": "synthetic dirty sample"})

    def assert_clean(self):
        if self.summary["contaminated"]:
            raise RuntimeError("synthetic external CPU contamination")

    def close(self):
        return None


class MockEngine:
    serial = 0

    def __init__(self):
        type(self).serial += 1
        pid = 99000 + type(self).serial
        self.transport = SimpleNamespace(get_pid=lambda: pid)
        self.options = {key: object() for key in (*OPTIONS, "UseNeuralEvaluation", "EvalFile")}
        self.id = {"name": "Regicide mocked arena transport"}

    def configure(self, values):
        return None

    def ping(self):
        return None

    def quit(self):
        return None

    def close(self):
        return None


class ArenaFutilityTests(unittest.TestCase):
    def setUp(self):
        MockGuard.instances.clear()
        MockGuard.dirty = False
        MockEngine.serial = 0

    def mock_arena(self, temporary, *, wins=False, partial=False, stop=True):
        root = Path(temporary).resolve()
        binary, candidate, output = root / "regicide", root / "candidate.nn", root / "arena"
        binary.write_bytes(b"mocked native engine")
        candidate.write_bytes(b"mocked model")
        reserve = {"openings": [{"name": f"fixture-{index}", "moves": [], "fen_after_opening": chess.STARTING_FEN}
                                 for index in range(32)]}
        def play(args, engines, record):
            if partial and record["game"] == 2:
                record.update(completed=False, result="*", termination="ply_cap")
                return
            candidate_white = record["white"] == "candidate"
            white_wins = candidate_white == wins
            legal = rule_game(record["game"], record["pair"], WHITE_WIN if white_wins else BLACK_WIN,
                              candidate_white)
            legal["opening"] = record["opening"]
            record.update(legal)
        with patch.object(ARENA, "validate_native_model", return_value={"mocked": True}), \
             patch.object(ARENA, "load_openings", return_value=reserve), \
             patch.object(ARENA, "EngineCpuGuard", MockGuard), \
             patch.object(ARENA, "WindowsProcesses", return_value=object()), \
             patch.object(ARENA, "LinuxProcesses", return_value=object()), \
             patch.object(chess.engine.SimpleEngine, "popen_uci", side_effect=lambda *a, **kw: MockEngine()), \
             patch.object(ARENA.comparison, "play_one", side_effect=play), \
             contextlib.redirect_stdout(io.StringIO()):
            result = ARENA.run_arena(binary, candidate, None, output, pairs=32, movetime_ms=100,
                                      attempt_index=6, stop_when_impossible=stop)
        return result, binary, candidate, output

    def test_six_zero_pairs_are_futile_but_five_can_still_pass(self):
        self.assertFalse(ARENA.futility_certificate([0] * 5, 32, 6)["stop_early"])
        certificate = ARENA.futility_certificate([0] * 6, 32, 6)
        self.assertTrue(certificate["stop_early"])
        self.assertEqual(certificate["observed_pairs"], 6)
        self.assertEqual(certificate["optimistic_final_decision"]["completed_pairs"], 32)
        self.assertFalse(certificate["optimistic_final_decision"]["passed"])

    def test_possible_perfect_prefix_never_stops_and_full_schedule_is_not_early(self):
        for count in range(33):
            with self.subTest(count=count):
                self.assertFalse(ARENA.futility_certificate([1] * count, 32, 6)["stop_early"])
        self.assertFalse(ARENA.futility_certificate([0] * 32, 32, 6)["stop_early"])

    def test_input_scores_not_mutated_or_replaced_by_hypothetical_wins(self):
        scores = [0] * 6
        certificate = ARENA.futility_certificate(scores, 32, 6)
        self.assertEqual(scores, [0] * 6)
        self.assertEqual(certificate["observed_pair_scores"], scores)
        self.assertEqual(certificate["unplayed_pairs"], 26)
        certificate["observed_pair_scores"].append(1)
        self.assertEqual(scores, [0] * 6)

    def test_bad_scores_counts_times_and_attempts_rejected(self):
        for scores, planned, attempt, ms in (([True], 32, 6, 100), ([.8], 32, 6, 100),
                ([float("nan")], 32, 6, 100), ([0] * 33, 32, 6, 100), ([], 31, 6, 100),
                ([], True, 6, 100), ([], 32, 0, 100), ([], 32, 6, 99), ([], 32, 6, True)):
            with self.subTest(scores=scores, planned=planned, attempt=attempt, ms=ms), self.assertRaises(ValueError):
                ARENA.futility_certificate(scores, planned, attempt, movetime_ms=ms)

    def test_exhaustive_late_continuations_cannot_turn_any_rejection_into_pass(self):
        checked, rejected, possible = 0, 0, 0
        for observed in (30, 31):
            for quarter_deficit in range(33):
                whole, remainder = divmod(quarter_deficit, 4)
                scores = [0] * whole + ([1 - remainder / 4] if remainder else [])
                scores += [1] * (observed - len(scores))
                certificate = ARENA.futility_certificate(scores, 32, 6)
                completions = [ARENA.promotion_decision(scores + list(tail), 6, complete=True, clean=True)["passed"]
                               for tail in itertools.product(VALUES, repeat=32 - observed)]
                if certificate["stop_early"]:
                    rejected += 1
                    self.assertFalse(any(completions))
                if any(completions):
                    possible += 1
                    self.assertFalse(certificate["stop_early"])
                checked += len(completions)
        self.assertEqual(checked, 990)
        self.assertGreater(rejected, 0)
        self.assertGreater(possible, 0)

    def test_native_orchestration_records_only_twelve_real_rule_games(self):
        with tempfile.TemporaryDirectory() as temporary:
            report, _, _, output = self.mock_arena(temporary)
            self.assertEqual(report["status"], "rejected_early_futile")
            self.assertFalse(report["completed"])
            self.assertFalse(report["eligible"])
            self.assertFalse(report["promotion_passed"])
            self.assertFalse(report["resource_guard"]["valid_for_comparison"])
            self.assertTrue(report["valid_for_rejection"])
            self.assertEqual(len(report["games"]), 12)
            self.assertEqual(len(report["opening_reserve"]["openings"]), 32)
            self.assertEqual(report["requested_pairs"], 32)
            self.assertEqual(report["decision"]["completed_pairs"], 6)
            self.assertEqual(report["decision"]["candidate_pair_score"], 0)
            self.assertEqual([game["candidate_points"] for game in report["games"]], [0] * 12)
            self.assertEqual(json.loads((output / "arena.json").read_text())["games"], report["games"])
            before = copy.deepcopy(report)
            self.assertTrue(ARENA.verify_early_rejection(report)["stop_early"])
            self.assertEqual(report, before)

    def test_default_option_keeps_full_unchanged_gate_for_losses(self):
        with tempfile.TemporaryDirectory() as temporary:
            report, _, _, _ = self.mock_arena(temporary, stop=False)
            self.assertEqual(report["status"], "complete")
            self.assertEqual(len(report["games"]), 64)
            self.assertTrue(report["eligible"])
            self.assertFalse(report["promotion_passed"])
            self.assertIsNone(report["futility_certificate"])
            self.assertFalse(report["valid_for_rejection"])

    def test_possible_winner_reaches_full_gate_and_passes_original_verifier(self):
        with tempfile.TemporaryDirectory() as temporary:
            report, _, _, _ = self.mock_arena(temporary, wins=True)
            self.assertEqual(report["status"], "complete")
            self.assertEqual(len(report["games"]), 64)
            self.assertTrue(report["promotion_passed"])
            self.assertTrue(ARENA.verify_match_records(report)["passed"])

    def test_dirty_or_incomplete_pair_remains_failed_and_never_certified(self):
        for dirty in (False, True):
            with self.subTest(dirty=dirty), tempfile.TemporaryDirectory() as temporary:
                MockGuard.dirty = dirty
                report, _, _, _ = self.mock_arena(temporary, partial=not dirty)
                self.assertEqual(report["status"], "failed")
                self.assertFalse(report["valid_for_rejection"])
                self.assertFalse(report["promotion_passed"])
                self.assertIsNone(report["futility_certificate"])

    def test_forged_certificate_outcomes_or_incomplete_flags_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            report, _, _, _ = self.mock_arena(temporary)
            for mutation in ("certificate", "points", "result", "half_pair", "passed", "complete", "guard", "missing_flag"):
                changed = copy.deepcopy(report)
                if mutation == "certificate":
                    changed["futility_certificate"]["optimistic_final_decision"]["candidate_pair_score"] = 0
                elif mutation == "points":
                    changed["games"][0]["candidate_points"] = 1
                elif mutation == "result":
                    changed["games"][0]["result"] = "1-0"
                elif mutation == "half_pair":
                    changed["games"].pop()
                elif mutation == "passed":
                    changed["promotion_passed"] = True
                elif mutation == "complete":
                    changed["completed"] = True
                elif mutation == "guard":
                    changed["resource_guard"]["contaminated"] = True
                else:
                    changed["valid_for_rejection"] = False
                with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                    ARENA.verify_early_rejection(changed)

    def test_early_rejection_cannot_be_used_by_registry_promotion(self):
        with tempfile.TemporaryDirectory() as temporary:
            report, binary, candidate, _ = self.mock_arena(temporary)
            with patch.object(REGISTRY, "validate_native_model") as native:
                with self.assertRaisesRegex(ValueError, "complete games"):
                    REGISTRY.promote(Path(temporary) / "registry", binary, candidate, report, expected_champion_sha=None)
                native.assert_not_called()

    def test_unsafe_optional_configuration_rejected_before_outputs_or_engines(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "no-output"
            for kwargs in ({"pairs": 31}, {"movetime_ms": 99}, {"stop_when_impossible": 1}):
                configuration = {"stop_when_impossible": True, **kwargs}
                with self.subTest(kwargs=kwargs), patch.object(chess.engine.SimpleEngine, "popen_uci") as native:
                    with self.assertRaises(ValueError):
                        ARENA.run_arena("missing", "missing", None, output, **configuration)
                    native.assert_not_called()
                self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
