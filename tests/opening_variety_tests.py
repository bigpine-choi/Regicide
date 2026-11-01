"""Deterministic opening policy and live-host tests; no external engine is used."""

from concurrent.futures import ThreadPoolExecutor
import copy
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import chess
import chess.engine

import human_play_tests as baseline
import opening_variety as variety


def board_after(*moves):
    board = chess.Board()
    for move in moves:
        board.push_uci(move)
    return board


def info(board, move, cp=40, depth=4, **extra):
    return {"depth": depth, "score": chess.engine.PovScore(chess.engine.Cp(cp), board.turn),
            "pv": [chess.Move.from_uci(move)], **extra}


def seed_for(board, candidate):
    baseline_move = "g1f3" if board.turn else "c7c5"
    count = len([move for move in variety.candidate_moves(board) if move != baseline_move])
    weights = {baseline_move: min(6, count), candidate: count}
    for value in range(1024):
        seed = f"{value:032x}"
        if (variety.plan(board, 1000, seed)["candidate_uci"] == candidate and
                (candidate == baseline_move or variety.selected_move(board, seed, weights, weights) == candidate)):
            return seed
    raise AssertionError("seed sample did not cover the requested candidate")


class PolicyTests(unittest.TestCase):
    def test_first_moves_use_the_whole_unique_legal_catalogue_pool(self):
        white = variety.candidate_moves(chess.Board())
        self.assertEqual(len(white), 20)
        self.assertEqual(set(white), {move.uci() for move in chess.Board().legal_moves})
        black = board_after("e2e4")
        candidates = variety.candidate_moves(black)
        self.assertGreater(len(candidates), 4)
        self.assertIn("g8f6", candidates)
        self.assertEqual(len(candidates), len(set(candidates)))
        self.assertTrue(all(chess.Move.from_uci(move) in black.legal_moves for move in candidates))

    def test_seed_diversifies_but_budget_and_polling_do_not_reroll_candidate(self):
        board = board_after("e2e4")
        choices = set()
        for value in range(256):
            seed = f"{value:032x}"
            expected = variety.plan(board, 1000, seed)["candidate_uci"]
            choices.add(expected)
            self.assertEqual(variety.plan(board, 5000, seed)["candidate_uci"], expected)
            self.assertEqual(variety.plan(board.copy(stack=True), 1000, seed)["candidate_uci"], expected)
        self.assertEqual(choices, set(variety.candidate_moves(board)))

    def test_policy_boundaries_keep_full_budget_for_normal_search(self):
        cases = [(chess.Board(), 100, False, "short_budget"),
                 (chess.Board(chess.STARTING_FEN, chess960=True), 1000, False, "custom_root"),
                 (chess.Board("7k/8/8/8/8/8/8/KQ6 w - - 0 1"), 1000, False, "custom_root"),
                 (chess.Board(), 1000, True, "user_forced_game"),
                 (board_after("f2f3", "e7e5", "g2g4", "d8h4"), 1000, False, "terminal"),
                 (board_after("e2e4", "d7d5", "f1b5"), 1000, False, "in_check")]
        for board, budget, forced, reason in cases:
            with self.subTest(reason=reason):
                result = variety.plan(board, budget, "00" * 16, forced=forced)
                self.assertFalse(result["eligible"])
                self.assertEqual(result["reason"], reason)
                self.assertEqual(result["baseline_budget_ms"], budget)
                self.assertEqual(result["candidate_budget_ms"], 0)
        self.assertTrue(variety.plan(chess.Board(), 500, "00" * 16)["eligible"])
        self.assertTrue(variety.plan(board_after("e2e4", "e7e5", "g1f3", "b8c6", "f1b5", "a7a6"), 1000, "00" * 16)["eligible"])

    def test_catalog_failure_and_repeated_positions_are_safe_fallbacks(self):
        with patch.object(variety, "_catalog", side_effect=variety.CatalogError("missing")):
            self.assertEqual(variety.plan(chess.Board(), 1000, "00" * 16)["reason"], "catalog_unavailable")
        # A repeated board may have a large catalogue pool, but policy excludes it.
        repeated = board_after("g1f3", "g8f6", "f3g1", "f6g8")
        self.assertEqual(len(variety.candidate_moves(repeated)), 20)
        self.assertFalse(variety.plan(repeated, 1000, "00" * 16)["eligible"])
        self.assertEqual(variety.plan(repeated, 1000, "00" * 16)["reason"], "repetition")

    def test_same_depth_gate_accepts_25cp_but_rejects_26cp_for_black(self):
        board = board_after("e2e4")
        for score, reason in [(15, "within_tolerance"), (14, "outside_tolerance")]:
            candidate = info(board, "e7e5", score, 4)
            result = variety.assess(board, chess.Move.from_uci("c7c5"),
                                     info(board, "c7c5", 40), "e7e5", {4: candidate}, candidate,
                                     chess.Move.from_uci("e7e5"))
            self.assertEqual(result["reason"], reason)
            self.assertEqual(result["candidate_compared"]["cp_white"], -score)
            self.assertEqual(result["compared_depth"], 4)

    def test_better_deeper_score_cannot_replace_worse_common_depth(self):
        board = board_after("e2e4")
        common, final = info(board, "e7e5", -100, 4), info(board, "e7e5", 200, 6)
        result = variety.assess(board, chess.Move.from_uci("c7c5"),
                                 info(board, "c7c5", 40), "e7e5", {4: common, 6: final}, final,
                                 chess.Move.from_uci("e7e5"))
        self.assertEqual(result["reason"], "outside_tolerance")
        self.assertEqual(result["candidate_final"]["depth"], 6)
        self.assertEqual(result["candidate_final_raw"]["cp"], 200)
        missing = variety.assess(board, chess.Move.from_uci("c7c5"),
                                  info(board, "c7c5", 40), "e7e5", {6: final}, final,
                                  chess.Move.from_uci("e7e5"))
        self.assertEqual(missing["reason"], "no_common_depth")

    def test_deeper_adverse_score_is_a_veto_not_an_admission_shortcut(self):
        board = board_after("e2e4")
        common, final = info(board, "e7e5", 30, 4), info(board, "e7e5", -300, 5)
        result = variety.assess(board, chess.Move.from_uci("c7c5"), info(board, "c7c5"),
                                 "e7e5", {4: common, 5: final}, final, chess.Move.from_uci("e7e5"))
        self.assertEqual(result["reason"], "deeper_candidate_warning")

    def test_mate_bound_illegal_short_or_mismatched_pv_is_never_quality_evidence(self):
        board = board_after("e2e4")
        good = info(board, "e7e5")
        bad = [dict(good, depth=0), dict(good, lowerbound=True), dict(good, upperbound=True),
               dict(good, score=chess.engine.PovScore(chess.engine.Mate(2), chess.BLACK)),
               dict(good, pv=[]), dict(good, pv=[chess.Move.from_uci("e7e4")]),
               dict(good, pv=[chess.Move.from_uci("e7e5"), chess.Move.from_uci("e8e7")]),
               {"depth": 8, "nodes": 300}, {"score": good["score"], "pv": good["pv"]}]
        for value in bad:
            with self.subTest(value=value):
                self.assertIsNone(variety.score_line(board, value, chess.Move.from_uci("e7e5")))
        self.assertIsNone(variety.score_line(board, good, chess.Move.from_uci("c7c5")))

    def test_memo_identity_includes_history_budget_model_binary_seed_and_catalog(self):
        board = board_after("e2e4")
        key = variety.memo_key(board, 1000, "binary", "model", seed="seed", catalog_id="catalog")
        self.assertEqual(key, variety.memo_key(board.copy(stack=True), 1000, "binary", "model", seed="seed", catalog_id="catalog"))
        variants = [(board, 500, "binary", "model", "seed", "catalog"),
                    (board, 1000, "binary2", "model", "seed", "catalog"),
                    (board, 1000, "binary", "model2", "seed", "catalog"),
                    (board, 1000, "binary", "model", "seed2", "catalog"),
                    (board, 1000, "binary", "model", "seed", "catalog2"),
                    (chess.Board(board.fen()), 1000, "binary", "model", "seed", "catalog")]
        for pos, budget, binary, model, seed, catalog in variants:
            self.assertNotEqual(key, variety.memo_key(pos, budget, binary, model, seed=seed, catalog_id=catalog))

    def test_pgn_metadata_is_validated_not_interpolated(self):
        value = {"enabled": True, "policy": variety.POLICY, "seed": "01" * 16}
        self.assertEqual(variety.pgn_headers(value), {"OpeningVarietySeed": "01" * 16,
                                                     "OpeningVarietyPolicy": variety.POLICY})
        for bad in [None, [], {}, dict(value, enabled=False), dict(value, policy="unknown"),
                    dict(value, seed="bad\nheader"), dict(value, seed="0" * 31)]:
            self.assertEqual(variety.pgn_headers(bad), {})


class Stream:
    def __init__(self, owner, board, script):
        self.owner, self.board, self.script = owner, board, script
        self.info = {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def __iter__(self):
        for packet in self.script.get("packets", []):
            self.info.update(packet)
            yield packet
        self.owner.waiting.set()
        if self.script.get("gate") is not None:
            if not self.script["gate"].wait(5):
                raise TimeoutError("test gate")
        if self.script.get("failure"):
            raise self.script["failure"]
        if self.owner.clock is not None:
            self.owner.clock.now += self.script.get("cost_ms", self.owner.cost_ms) / 1000

    def wait(self):
        return chess.engine.BestMove(chess.Move.from_uci(self.script["move"]), None)


class Peer(baseline.FakeEngine):
    def __init__(self):
        super().__init__()
        self.scripts = []
        self.searches = []
        self.waiting = threading.Event()
        self.gates = []
        self.clock = None
        self.cost_ms = 0

    def analysis(self, board, limit, *, game=None, info=None, root_moves=None):
        if not self.scripts and not root_moves:
            raise AssertionError("unexpected engine search")
        self.searches.append({"board": board.copy(stack=True), "limit": limit,
                              "game": game, "root_moves": root_moves,
                              "at_ms": self.clock.now * 1000 if self.clock else None})
        script = self.scripts.pop(0) if self.scripts else {
            "move": root_moves[0].uci(), "packets": [globals()["info"](board, root_moves[0].uci(), -500, limit.depth)]}
        if script.get("gate") is not None:
            self.gates.append(script["gate"])
        return Stream(self, board, script)

    def close(self):
        super().close()
        for gate in self.gates:
            gate.set()


class LivePolicyTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="regicide-opening-variety-")
        self.root = Path(self.folder.name)
        self.binary = self.root / "fake.exe"
        self.binary.write_bytes(b"fake engine, never executed")
        self.peer = Peer()
        self.game = baseline.play_human.HumanGame(self.binary, self.root / "games", lambda _: self.peer,
                                                 evaluation_engine_factory=baseline.FakeFactory())
        self.addCleanup(self.folder.cleanup)
        self.addCleanup(self.game.close)
        self.game._opening_seed = seed_for(board_after("e2e4"), "e7e5")

    def prepare(self, *, candidate_cp=20, candidate_depth=4, failure=None, gate=None):
        board = board_after("e2e4")
        self.peer.scripts = [
            {"move": "c7c5", "packets": [info(board, "c7c5", 40, 4)]},
            {"move": "e7e5", "packets": [info(board, "e7e5", candidate_cp, candidate_depth)],
             "failure": failure, "gate": gate},
        ]
        return self.game.human_move("e2e4", self.game.revision)

    def test_full_baseline_and_multiple_probes_preserve_history_and_model_engine(self):
        before = self.prepare()
        token = self.game._game_token
        after = self.game.engine_move(before["revision"])
        self.assertEqual(after["last_move"], "e7e5")
        self.assertEqual(len(self.peer.searches), 7)
        self.assertEqual(self.peer.searches[0]["limit"].time, 1)
        self.assertIs(self.peer.searches[0]["game"], token)
        self.assertIsNone(self.peer.searches[0]["limit"].depth)
        for call in self.peer.searches[1:]:
            self.assertAlmostEqual(call["limit"].time, 1 / 3)
            self.assertEqual(call["limit"].depth, 4)
        self.assertIsNone(self.peer.searches[0]["root_moves"])
        self.assertEqual(self.peer.searches[1]["root_moves"], [chess.Move.from_uci("e7e5")])
        self.assertIsNot(self.peer.searches[0]["game"], self.peer.searches[1]["game"])
        self.assertTrue(all(call["board"].fen(en_passant="fen") == before["fen"] for call in self.peer.searches))
        self.assertTrue(all(call["board"].move_stack == [chess.Move.from_uci("e2e4")] for call in self.peer.searches))
        record = json.loads((self.game.save_dir / f"{self.game.game_id}.json").read_text(encoding="utf-8"))
        saved = record["opening_variety"]
        self.assertEqual(saved["seed"], self.game._opening_seed)
        self.assertEqual(saved["decisions"][0]["reason"], "accepted_alternative")
        self.assertEqual(saved["decisions"][0]["before_fen"], before["fen"])
        self.assertEqual(saved["decisions"][0]["baseline_budget_ms"], 1000)
        self.assertEqual(saved["decisions"][0]["extra_budget_ms"], 1000)
        self.assertEqual(len(saved["decisions"][0]["probes"]), 6)
        self.assertIn("OpeningVarietySeed", self.game.pgn())

    def test_restricted_score_never_overwrites_live_best_evaluation(self):
        gate = threading.Event()
        before = self.prepare(candidate_cp=999, gate=gate)
        with ThreadPoolExecutor(max_workers=1) as pool:
            request = pool.submit(self.game.engine_move, before["revision"])
            try:
                from hint_state_tests import wait_for
                wait_for(lambda: len(self.peer.searches) == 2)
                value = self.game.evaluation()
                self.assertEqual(value["cp"], -40)
                self.assertEqual(value["source_fen"], before["fen"])
                self.assertEqual(value["depth"], 4)
                self.assertFalse(request.done())
            finally:
                gate.set()
            request.result(timeout=2)

    def test_candidate_failure_keeps_legal_baseline_without_game_error(self):
        before = self.prepare(failure=TimeoutError("candidate peer timeout"))
        after = self.game.engine_move(before["revision"])
        self.assertEqual(after["last_move"], "c7c5")
        self.assertIsNone(after["error"])
        self.assertIsNone(self.game._engine)
        self.assertTrue(self.peer.closed)
        self.assertEqual(self.game._opening_decisions[2]["reason"], "candidate_failed")

    def test_candidate_wrong_move_or_unfinished_depth_keeps_baseline(self):
        before = self.prepare(candidate_depth=3)
        after = self.game.engine_move(before["revision"])
        self.assertEqual(after["last_move"], "c7c5")
        self.assertEqual(self.game._opening_decisions[2]["reason"], "no_qualified_alternative")
        self.assertEqual(self.game._opening_decisions[2]["probes"][0]["reason"], "no_common_depth")

    def test_candidate_returning_another_legal_move_is_rejected(self):
        before = self.prepare()
        self.peer.scripts[1] = {"move": "e7e6", "packets": [info(board_after("e2e4"), "e7e6", 100)]}
        after = self.game.engine_move(before["revision"])
        self.assertEqual(after["last_move"], "c7c5")
        self.assertIsNone(after["error"])
        self.assertEqual(self.game._opening_decisions[2]["reason"], "no_qualified_alternative")
        self.assertEqual(self.game._opening_decisions[2]["probes"][0]["reason"], "candidate_unreliable")

    def test_time_change_during_baseline_keeps_captured_full_and_extra_budgets(self):
        before = self.prepare()
        gate = threading.Event()
        self.peer.scripts[0]["gate"] = gate
        with ThreadPoolExecutor(max_workers=1) as pool:
            request = pool.submit(self.game.engine_move, before["revision"])
            try:
                self.assertTrue(self.peer.waiting.wait(2))
                changed = self.game.set_think_time(5000, self.game.game_id, self.game._settings_revision)
                self.assertEqual(changed["engine"]["active_movetime_ms"], 1000)
                self.assertEqual(changed["engine"]["active_extra_budget_ms"], 1000)
                self.assertEqual(changed["engine"]["extra_budget_ms"], 2000)
                self.assertFalse(request.done())
            finally:
                gate.set()
            after = request.result(timeout=3)
        self.assertEqual(after["engine"]["movetime_ms"], 5000)
        self.assertEqual(self.peer.searches[0]["limit"].time, 1)
        self.assertTrue(all(abs(call["limit"].time - 1 / 3) < .001 for call in self.peer.searches[1:]))
        self.assertEqual(self.game._engine_movetimes[2], 1000)
        self.assertEqual(self.game._opening_decisions[2]["candidate_budget_ms"], 1000)
        self.assertIsNone(after["engine"]["active_extra_budget_ms"])

    def test_total_watchdog_cancels_wedged_candidate_and_keeps_baseline(self):
        before = self.prepare(gate=threading.Event())
        self.peer.timeout = 10  # Candidate timeout must not inherit the primary engine's long grace.
        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=1) as pool:
            after = pool.submit(self.game.engine_move, before["revision"]).result(timeout=3)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(after["last_move"], "c7c5")
        self.assertIsNone(after["error"])
        self.assertTrue(self.peer.closed)
        self.assertIsNone(self.game._engine)
        self.assertEqual(self.game._opening_decisions[2]["reason"], "candidate_failed")

    def test_shared_monotonic_deadline_limits_multiple_probe_commands(self):
        before = self.prepare()
        clock = SimpleNamespace(now=10.0)
        self.peer.clock, self.peer.cost_ms = clock, 250
        self.peer.scripts[0]["cost_ms"] = 1000
        with patch.object(baseline.play_human, "time", SimpleNamespace(monotonic=lambda: clock.now)):
            after = self.game.engine_move(before["revision"])
        self.assertIsNone(after["error"])
        calls = self.peer.searches
        self.assertEqual(calls[0]["limit"].time, 1)
        self.assertEqual(len(calls), 5)  # Four 250ms probes exhaust E, despite a six-candidate cap.
        self.assertAlmostEqual(calls[-1]["limit"].time, .25)
        for call in calls[1:]:
            self.assertLessEqual(call["limit"].time * 1000, 12000 - call["at_ms"] + .001)
        decision = self.game._opening_decisions[2]
        self.assertEqual(decision["baseline_elapsed_ms"], 1000)
        self.assertEqual(decision["extra_elapsed_ms"], 1000)
        self.assertEqual(decision["extra_budget_ms"], 1000)
        self.assertEqual(len(decision["probes"]), 4)

    def test_a_rejected_first_probe_does_not_prevent_a_later_qualified_choice(self):
        board = board_after("e2e4")
        baseline_move = chess.Move.from_uci("c7c5")
        for value in range(1024):
            seed = f"{value:032x}"
            plan = variety.plan(board, 1000, seed)
            proposals = variety.proposals(plan, baseline_move)
            later = proposals[1]
            weights = {"c7c5": 6, later: len(plan["candidates"]) - 1}
            if variety.selected_move(board, seed, weights, weights) == later:
                break
        else:
            self.fail("No fixed seed selected the second qualified proposal")
        self.game._opening_seed = seed
        before = self.game.human_move("e2e4", self.game.revision)
        self.peer.scripts = [
            {"move": "c7c5", "packets": [info(board, "c7c5", 40)]},
            {"move": proposals[0], "packets": [info(board, proposals[0], -100)]},
            {"move": later, "packets": [info(board, later, 20)]},
        ]
        after = self.game.engine_move(before["revision"])
        self.assertEqual(after["last_move"], later)
        decision = self.game._opening_decisions[2]
        self.assertEqual(decision["probes"][0]["reason"], "outside_tolerance")
        self.assertTrue(decision["probes"][1]["admitted"])
        self.assertEqual(decision["selection_weights"], weights)

    def test_late_probe_failure_or_score_conflict_cannot_apply_an_earlier_qualified_move(self):
        for conflict in (False, True):
            with self.subTest(conflict=conflict):
                if self.game.board.move_stack:
                    self.game.new_game("white", 1000, self.game.revision)
                    self.game._opening_seed = seed_for(board_after("e2e4"), "e7e5")
                before = self.prepare()
                board = self.game.board.copy(stack=True)
                later = variety.proposals(variety.plan(board, 1000, self.game._opening_seed), chess.Move.from_uci("c7c5"))[1]
                script = {"move": later, "packets": [info(board, later, 66 if conflict else 20)]}
                if not conflict:
                    script["failure"] = RuntimeError("later probe failed")
                self.peer.scripts.append(script)
                after = self.game.engine_move(before["revision"])
                self.assertEqual(after["last_move"], "c7c5")
                self.assertIsNone(after["error"])
                decision = self.game._opening_decisions[2]
                self.assertTrue(decision["probes"][0]["admitted"])
                self.assertEqual(decision["selection_pool"], ["c7c5"])
                self.assertEqual(decision["reason"], "candidate_score_conflict" if conflict else "candidate_failed")

    def test_mate_baseline_never_spends_verification_budget(self):
        before = self.prepare()
        self.peer.scripts = [{"move": "c7c5", "packets": [info(self.game.board, "c7c5",
                              score=chess.engine.PovScore(chess.engine.Mate(4), chess.BLACK))]}]
        after = self.game.engine_move(before["revision"])
        self.assertEqual(after["last_move"], "c7c5")
        self.assertEqual(len(self.peer.searches), 1)
        self.assertEqual(self.peer.searches[0]["limit"].time, 1)
        self.assertEqual(self.game._opening_decisions[2]["probes"], [])
        self.assertEqual(self.game._opening_decisions[2]["reason"], "baseline_unreliable")

    def test_repetition_disables_probes_but_preserves_full_baseline_and_history(self):
        with self.game._lock:
            self.game.board = board_after("g1f3", "g8f6", "f3g1", "f6g8", "g1f3")
            self.game.revision += 1
            self.game._sync_evaluation()
        board = self.game.board.copy(stack=True)
        self.assertFalse(board.can_claim_threefold_repetition())
        self.peer.scripts = [{"move": "g8f6", "packets": [info(board, "g8f6")]}]
        after = self.game.engine_move(self.game.revision)
        self.assertEqual(after["last_move"], "g8f6")
        self.assertEqual(len(self.peer.searches), 1)
        self.assertEqual(self.peer.searches[0]["limit"].time, 1)
        self.assertEqual(self.peer.searches[0]["board"].move_stack, board.move_stack)
        self.assertEqual(self.game._opening_decisions[6]["reason"], "repetition")

    def test_baseline_is_excluded_from_proposals_so_other_moves_are_still_compared(self):
        self.game._opening_seed = seed_for(board_after("e2e4"), "c7c5")
        before = self.prepare()
        self.peer.scripts.pop()
        after = self.game.engine_move(before["revision"])
        self.assertEqual(after["last_move"], "c7c5")
        self.assertEqual(len(self.peer.searches), 7)
        decision = self.game._opening_decisions[2]
        self.assertEqual(decision["reason"], "no_qualified_alternative")
        self.assertNotIn("c7c5", decision["proposals"])
        self.assertTrue(all(probe["uci"] != "c7c5" for probe in decision["probes"]))

    def test_incomplete_baseline_keeps_legal_fallback_without_candidate_probe(self):
        before = self.prepare()
        self.peer.scripts[0]["packets"] = [info(board_after("e2e4"), "c7c5", 40, 2), {"depth": 9}]
        self.peer.scripts.pop()
        after = self.game.engine_move(before["revision"])
        self.assertEqual(after["last_move"], "c7c5")
        self.assertEqual(len(self.peer.searches), 1)
        self.assertEqual(self.game._opening_decisions[2]["reason"], "baseline_unreliable")
        self.assertEqual(self.game._opening_decisions[2]["baseline"]["depth"], 2)

    def test_undo_reuses_decision_without_another_probe_but_new_budget_rechecks(self):
        before = self.prepare()
        self.game.engine_move(before["revision"])
        seed = self.game._opening_seed
        self.game.undo(self.game.revision)
        self.assertEqual(self.game._opening_decisions, {})
        self.assertEqual(self.game._opening_seed, seed)
        self.game.human_move("e2e4", self.game.revision)
        after = self.game.engine_move(self.game.revision)
        self.assertEqual(after["last_move"], "e7e5")
        self.assertEqual(len(self.peer.searches), 7)
        self.assertTrue(self.game._opening_decisions[2]["reused"])
        self.game.undo(self.game.revision)
        self.game.set_think_time(3000, self.game.game_id, self.game._settings_revision)
        before = self.prepare()
        self.game.engine_move(before["revision"])
        self.assertEqual(len(self.peer.searches), 14)
        self.assertEqual(self.peer.searches[7]["limit"].time, 3)
        self.assertTrue(all(abs(call["limit"].time - 2 / 3) < .001 for call in self.peer.searches[8:]))
        self.assertFalse(self.game._opening_decisions[2]["reused"])

    def test_handicap_cancel_restores_receipts_and_forced_commit_disables_variety(self):
        before = self.prepare()
        self.game.engine_move(before["revision"])
        original = copy.deepcopy(self.game._opening_decisions)
        seed, memo = self.game._opening_seed, copy.deepcopy(self.game._opening_memo)
        files = {p.name: p.read_bytes() for p in self.game.save_dir.iterdir()}
        self.game.begin_handicap(2, self.game.revision)
        self.assertEqual(self.game._opening_decisions, {})
        self.assertEqual({p.name: p.read_bytes() for p in self.game.save_dir.iterdir()}, files)
        self.game.cancel_handicap(self.game.revision)
        self.assertEqual(self.game._opening_decisions, original)
        self.assertEqual(self.game._opening_memo, memo)
        self.assertEqual(self.game._opening_seed, seed)
        self.game.begin_handicap(2, self.game.revision)
        self.game.forced_move("c7c5", self.game.revision)
        self.assertEqual(self.game._opening_decisions, {})
        self.game.human_move("g1f3", self.game.revision)
        board = self.game.board.copy(stack=True)
        self.peer.scripts = [{"move": "d7d6", "packets": [info(board, "d7d6")]}]
        self.game.engine_move(self.game.revision)
        self.assertEqual(self.peer.searches[-1]["limit"].time, 1)
        self.assertEqual(self.game._opening_decisions[4]["reason"], "user_forced_game")

    def test_100ms_uses_one_full_budget_search_and_new_game_gets_new_seed(self):
        original_seed = self.game._opening_seed
        self.game.new_game("white", 100, self.game.revision)
        self.assertNotEqual(self.game._opening_seed, original_seed)
        before = self.prepare()
        self.peer.scripts.pop()
        after = self.game.engine_move(before["revision"])
        self.assertEqual(after["last_move"], "c7c5")
        self.assertEqual(len(self.peer.searches), 1)
        self.assertEqual(self.peer.searches[0]["limit"].time, .1)
        self.assertEqual(self.game._opening_decisions[2]["reason"], "short_budget")

    def test_black_human_undo_retains_varied_white_opening_and_its_receipt(self):
        self.game.new_game("black", 1000, self.game.revision)
        self.game._opening_seed = seed_for(chess.Board(), "e2e4")
        board = chess.Board()
        self.peer.scripts = [
            {"move": "g1f3", "packets": [info(board, "g1f3", 40)]},
            {"move": "e2e4", "packets": [info(board, "e2e4", 30)]},
        ]
        self.game.engine_move(self.game.revision)
        first = copy.deepcopy(self.game._opening_decisions[1])
        self.assertEqual(first["selected_uci"], "e2e4")
        self.assertEqual(first["reason"], "accepted_alternative")
        self.game.human_move("e7e5", self.game.revision)
        board = self.game.board.copy(stack=True)
        candidate = variety.plan(board, 1000, self.game._opening_seed)["candidate_uci"]
        self.peer.scripts = [{"move": candidate, "packets": [info(board, candidate, 40)]}]
        self.game.engine_move(self.game.revision)
        self.game.undo(self.game.revision)
        self.assertEqual([move.uci() for move in self.game.board.move_stack], ["e2e4"])
        self.assertEqual(self.game._opening_decisions, {1: first})


if __name__ == "__main__":
    unittest.main(verbosity=2)
