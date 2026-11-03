"""Focused protocol, clock isolation, censoring and failure-denominator checks."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
import chess
import chess.engine
import compare_sparring_referee as experiment
import play_human
import sparring_play
from boss_strength_match_tests import Peer


class JudgeAnalysis:
    def __init__(self, board, limit):
        self.board, self.limit = board, limit
        self.move = next(iter(board.legal_moves))
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def __iter__(self):
        white_cp = -50 * len(self.board.move_stack)
        relative = white_cp if self.board.turn else -white_cp
        yield {"depth": self.limit.depth or 10, "score": chess.engine.PovScore(chess.engine.Cp(relative), self.board.turn),
               "nodes": self.limit.nodes or 100, "pv": [self.move]}
    def wait(self):
        return chess.engine.BestMove(self.move, None)


class Judge(Peer):
    def __init__(self):
        super().__init__(stockfish=True)
        self.options.update({"Skill Level": object(), "Clear Hash": object()})
        self.analyses = []
    def analysis(self, board, limit, **kwargs):
        self.analyses.append((board.copy(stack=True), limit, kwargs))
        return JudgeAnalysis(board, limit)


class Referee:
    instances = []
    def __init__(self, binary, factory):
        self.binary = binary
        self.contexts, self.screens = [], []
        self.closed = False
        Referee.instances.append(self)
    def snapshot(self):
        return {"available": True, "status": "idle", "binary_sha256": experiment.sha(self.binary),
                "engine_name": "Stockfish controlled referee"}
    def update_position(self, **kwargs):
        self.contexts.append(kwargs)
    def screen_candidates(self, **kwargs):
        self.screens.append(kwargs)
        return {"status": "skipped", "selected_uci": kwargs["baseline_move"].uci()}
    def close(self):
        self.closed = True
    def cancel(self):
        pass


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.own, self.sf = self.folder / "own.exe", self.folder / "sf.exe"
        self.own.write_bytes(b"fake own peer; not executed")
        self.sf.write_bytes(b"fake stockfish peer; not executed")
        self.config = {"variant": "fixture-before", "referee": "off", "source_root": str(ROOT),
                       "binary": str(self.own), "stockfish": str(self.sf), "judge_binary": str(self.sf),
                       "referee_binary": str(self.sf), "output": str(self.folder / "run"),
                       "ratings": [1320], "colors": ["white"], "game_ids": None,
                       "seed": "fixed-pair", "openings": list(experiment.OPENINGS[:1]),
                       "own_ms": 1000, "opponent_ms": 1000, "time_control": "10+5", "max_plies": 40,
                       "close_plies": 40, "judge_depth": 14, "judge_nodes": None,
                       "move_timeout": 3, "judge_timeout": 3, "stage": "all"}
        self.raw = []
        self.runtime = SimpleNamespace(host=play_human, policy=sparring_play,
                                       referee=SimpleNamespace(SparringReferee=Referee))
        Referee.instances.clear()

    def factory(self, path, role, ledger):
        if role == "independent_judge":
            self.assertTrue(all(peer.closed for other_role, peer in self.raw if other_role != "independent_judge"),
                            "offline judging must not overlap any playing process")
            peer = Judge()
        else:
            peer = Peer(stockfish=role != "regicide")
        self.raw.append((role, peer))
        return experiment.RecordedEngine(peer, role, ledger)

    def execute(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return experiment.run(self.config, runtime=self.runtime, factory=self.factory)

    def test_default_eight_cases_share_seed_across_colors_and_shard_ids(self):
        entries = experiment.schedule()
        self.assertEqual(len(entries), 8)
        self.assertEqual([row["game"] for row in entries], list(range(1, 9)))
        for left, right in zip(entries[::2], entries[1::2]):
            self.assertEqual(left["sparring_seed"], right["sparring_seed"])
            self.assertEqual(left["opponent_elo"], right["opponent_elo"])
            self.assertEqual([left["sparring_color"], right["sparring_color"]], ["white", "black"])
        self.assertEqual(entries[4:], experiment.schedule([2100, 2500]))

    def test_wait_override_neither_waits_nor_sets_cancellation_event(self):
        class Base:
            def _wait_for_engine_turn(self, *args):
                raise AssertionError("presentation wait invoked")
        value = experiment.experiment_host(Base)()
        value._clock = SimpleNamespace(snapshot=lambda: {"turn_elapsed_ms": 25})
        cancellation = threading.Event()
        receipt = value._wait_for_engine_turn({"target_total_ms": 10000}, "g", object(), 1, cancellation)
        self.assertFalse(cancellation.is_set())
        self.assertEqual(receipt["waited_ms"], 0)
        self.assertEqual(receipt["target_total_ms"], 10000)

    def test_screen_only_wrapper_disables_display_and_preserves_screen_arguments(self):
        peer = Referee(self.sf, None)
        wrapper = experiment.ScreenOnlyReferee(peer)
        wrapper.update_position(game_id="g", revision=1, board=chess.Board(), active=True, budget_ms=500)
        self.assertFalse(peer.contexts[0]["active"])
        move = chess.Move.from_uci("e2e4")
        wrapper.screen_candidates(baseline_move=move, candidates=[move], budget_ms=1500)
        self.assertEqual(peer.screens[0]["budget_ms"], 1500)

    def test_real_host_path_closes_players_before_offline_judging_and_retains_early_end(self):
        report = self.execute()
        self.assertEqual(report["status"], "completed")
        record = json.loads((Path(self.config["output"]) / "game-001.json").read_text(encoding="utf-8"))
        self.assertEqual(record["plies"], 4)
        self.assertEqual(record["termination"], "checkmate")
        self.assertEqual(len(record["judgements"]), 5)
        self.assertEqual(record["host_configuration"]["clock"]["control_id"], "10+5")
        self.assertFalse(record["host_configuration"]["referee"].get("configured", False))
        metrics = record["close_metrics"]
        self.assertEqual(metrics["missing_or_not_reached"], 36)
        self.assertEqual(metrics["within_100"], 2)
        self.assertEqual(metrics["conservative_100_of_window"], .05)
        self.assertTrue(metrics["early_terminal_before_window"])
        self.assertFalse(metrics["strict_all_window_within_100"])
        self.assertTrue(all(peer.closed for _, peer in self.raw))

    def test_assisted_arm_is_explicitly_configured_with_all_display_updates_inactive(self):
        self.config["referee"] = "on"
        report = self.execute()
        self.assertEqual(report["status"], "completed")
        record = json.loads((Path(self.config["output"]) / "game-001.json").read_text(encoding="utf-8"))
        self.assertTrue(record["host_configuration"]["referee"]["configured"])
        self.assertTrue(Referee.instances)
        self.assertTrue(all(not row["active"] for peer in Referee.instances for row in peer.contexts))
        self.assertTrue(all(peer.closed for peer in Referee.instances))

    def test_play_stage_starts_no_judge_and_separate_judge_stage_does_not_replay_games(self):
        self.config["stage"] = "play"
        first = self.execute()
        self.assertEqual(first["status"], "awaiting_offline_judging")
        self.assertFalse(any(role == "independent_judge" for role, _ in self.raw))
        playing_count = len(self.raw)
        self.config["stage"] = "judge"
        second = self.execute()
        self.assertEqual(second["status"], "completed")
        self.assertTrue(all(role == "independent_judge" for role, _ in self.raw[playing_count:]))

    def test_failure_and_missing_positions_remain_in_planned_denominator(self):
        def illegal_factory(path, role, ledger):
            if role == "opponent":
                peer = Peer(stockfish=True, illegal=True)
                self.raw.append((role, peer))
                return experiment.RecordedEngine(peer, role, ledger)
            return self.factory(path, role, ledger)
        with contextlib.redirect_stdout(io.StringIO()):
            report = experiment.run(self.config, runtime=self.runtime, factory=illegal_factory)
        self.assertEqual(report["status"], "completed_with_errors")
        self.assertEqual(report["summary"]["planned_close_positions"], 40)
        self.assertGreater(report["summary"]["game_errors"], 0)
        record = json.loads((Path(self.config["output"]) / "game-001.json").read_text(encoding="utf-8"))
        self.assertEqual(record["result"], "*")
        self.assertEqual(record["termination"], "engine_error")
        self.assertGreater(record["close_metrics"]["missing_or_not_reached"], 0)

    def test_cap_is_unfinished_and_never_counted_as_draw(self):
        result = experiment.ending(chess.Board(), 0)
        self.assertEqual(result["result"], "*")
        self.assertTrue(result["capped"])
        self.assertFalse(result["legal_result"])

    def test_imbalance_drop_and_raw_own_loss_are_distinct(self):
        record = {"sparring_color": "white", "plies": 3, "termination": "ply_cap", "capped": True,
                  "legal_result": False, "errors": [], "moves": [{"actor": "regicide"}] * 3,
                  "judgements": [{"ply": i, "status": "exact", "cp_white": cp, "mate_white": None}
                                 for i, cp in enumerate([250, -250, 0, -350])]}
        metrics = experiment.close_metrics(record, 40)
        self.assertEqual(metrics["changes"][0]["raw_cp_loss_own"], 500)
        self.assertFalse(metrics["changes"][0]["imbalance_drop"])
        self.assertTrue(metrics["changes"][2]["imbalance_drop"])
        self.assertEqual(metrics["changes"][2]["imbalance_excess_increase_cp"], 250)

    def test_score_packets_cannot_merge_partial_depth_or_accept_foreign_position_pv(self):
        board = chess.Board()
        good = {"depth": 14, "score": chess.engine.PovScore(chess.engine.Cp(12), chess.WHITE),
                "pv": [chess.Move.from_uci("e2e4")]}
        self.assertEqual(experiment.score_packet(board, good)["cp_white"], 12)
        for bad in ({"depth": 15}, {**good, "upperbound": True},
                    {**good, "pv": [chess.Move.from_uci("e2e5")]},
                    {**good, "score": chess.engine.PovScore(chess.engine.Cp(12), chess.BLACK)}):
            self.assertIsNone(experiment.score_packet(board, bad))


if __name__ == "__main__":
    unittest.main()
