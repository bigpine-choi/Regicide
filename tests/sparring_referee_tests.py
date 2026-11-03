"""Local referee boundaries with controllable fake UCI peers; no engine installed.

The real official engine can be checked separately. These tests assert score
perspective, comparable rounds, baseline fallback, cancellation and ownership.
"""

import copy
from pathlib import Path
import random
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import chess
import chess.engine
import sparring_referee as referee


def packet(board, move, cp_white=20, *, depth=10, rank=1, mate_white=None):
    move = chess.Move.from_uci(move) if isinstance(move, str) else move
    sign = 1 if board.turn else -1
    score = chess.engine.Cp(cp_white * sign) if mate_white is None else chess.engine.Mate(mate_white * sign)
    return {"depth": depth, "score": chess.engine.PovScore(score, board.turn),
            "pv": [move], "multipv": rank, "nodes": 1234}


class FakeAnalysis:
    def __init__(self, engine, packets, best, gate=None, delay=0):
        self.engine, self.packets, self.best = engine, packets, best
        self.gate, self.delay = gate, delay

    def __enter__(self):
        return self

    def __exit__(self, *arguments):
        return False

    def __iter__(self):
        self.engine.factory.entered.set()
        if self.gate is not None:
            while not self.gate.wait(.005):
                if self.engine.closed.is_set():
                    raise chess.engine.EngineTerminatedError("fixture closed")
        if self.delay:
            end = time.monotonic() + self.delay
            while time.monotonic() < end:
                if self.engine.closed.wait(.005):
                    raise chess.engine.EngineTerminatedError("fixture closed")
        for row in self.packets:
            if self.engine.closed.is_set():
                raise chess.engine.EngineTerminatedError("fixture closed")
            yield copy.deepcopy(row)

    def wait(self):
        if self.engine.closed.is_set():
            raise chess.engine.EngineTerminatedError("fixture closed")
        return chess.engine.BestMove(self.best, None)


class FakeEngine:
    def __init__(self, factory):
        self.factory = factory
        self.id = {"name": factory.name}
        self.options = {key: object() for key in ("Threads", "Hash", "Skill Level", "UCI_LimitStrength", "UCI_ShowWDL")}
        self.closed = threading.Event()
        self.settings = None

    def configure(self, settings):
        self.settings = dict(settings)

    def analysis(self, board, limit, **options):
        with self.factory.lock:
            index = len(self.factory.searches)
            self.factory.searches.append({"board": board.copy(stack=True), "limit": limit,
                                          "options": options, "engine": self})
        data = self.factory.plan(board, limit, options, index)
        return FakeAnalysis(self, **data)

    def close(self):
        if not self.closed.is_set():
            self.closed.set()
            with self.factory.lock:
                self.factory.active -= 1


class FakeFactory:
    def __init__(self, plan=None, name="Stockfish fixture"):
        self.name, self.plan = name, plan or self.default_plan
        self.lock = threading.Lock()
        self.entered = threading.Event()
        self.engines, self.searches = [], []
        self.active, self.maximum = 0, 0

    def __call__(self, binary):
        engine = FakeEngine(self)
        with self.lock:
            self.engines.append(engine)
            self.active += 1
            self.maximum = max(self.maximum, self.active)
        return engine

    @staticmethod
    def default_plan(board, limit, options, index):
        allowed = options.get("root_moves")
        if allowed:
            return {"packets": [packet(board, move, (300 if rank == 1 else 20) * (1 if board.turn else -1), rank=rank)
                                 for rank, move in enumerate(allowed, 1)], "best": allowed[0]}
        move = next(iter(board.legal_moves))
        return {"packets": [packet(board, move, 20)], "best": move}


def long_board(plies):
    for seed in range(40):
        rng, board = random.Random(seed), chess.Board()
        for _ in range(plies):
            if board.is_game_over(claim_draw=True):
                break
            moves = [move for move in board.legal_moves
                     if not _terminal_child(board, move)]
            if len(moves) < 2:
                break
            board.push(rng.choice(moves))
        if len(board.move_stack) == plies and board.legal_moves.count() >= 2 and not board.is_game_over(claim_draw=True):
            return board
    raise AssertionError("could not prepare a deterministic live long-game fixture")


def _terminal_child(board, move):
    child = board.copy(stack=True)
    child.push(move)
    return child.is_game_over(claim_draw=True)


class SparringRefereeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.binary = Path(self.temporary.name) / "stockfish-fixture.exe"
        self.binary.write_bytes(b"never executed: fake UCI factory")

    def peer(self, factory=None, binary="fixture"):
        factory = factory or FakeFactory()
        service = referee.SparringReferee(self.binary if binary == "fixture" else binary, factory)
        self.addCleanup(service.close)
        return service, factory

    def completed(self, service):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            value = service.snapshot()
            if value["status"] not in ("loading",):
                return value
            time.sleep(.005)
        self.fail("fixture reference did not complete")

    def screen(self, service, board=None, baseline="e2e4", proposal="d2d4", **options):
        board = board or chess.Board()
        return service.screen_candidates(game_id="game", revision=1, board=board,
                                         baseline_move=baseline, candidates=[proposal], **options)

    def test_missing_binary_does_not_spawn_or_break_baseline(self):
        service, factory = self.peer(binary=None)
        self.assertFalse(service.snapshot()["available"])
        value = service.update_position(game_id="game", revision=1, board=chess.Board())
        self.assertEqual(value["status"], "unavailable")
        result = self.screen(service)
        self.assertEqual((result["selected_uci"], result["reason"]), ("e2e4", "referee_unavailable"))
        self.assertEqual(factory.engines, [])

    def test_constructor_is_lazy_and_critical_settings_are_full_strength(self):
        service, factory = self.peer()
        self.assertEqual(factory.engines, [])
        service.update_position(game_id="game", revision=1, board=chess.Board())
        value = self.completed(service)
        self.assertEqual(value["status"], "ready")
        self.assertEqual(factory.engines[0].settings, {"Threads": 1, "Hash": 32, "Skill Level": 20,
                                                     "UCI_LimitStrength": False, "UCI_ShowWDL": True})
        self.assertEqual(value["source"], "local_stockfish_referee")
        self.assertEqual(value["bound"], "exact")
        self.assertIsNone(value["white_share"])
        self.assertTrue(factory.engines[0].closed.is_set())

    def test_black_position_is_still_reported_from_white_view(self):
        board = chess.Board()
        board.push_uci("e2e4")
        def plan(position, limit, options, index):
            move = next(iter(position.legal_moves))
            row = packet(position, move, -85)
            row["wdl"] = chess.engine.PovWdl(chess.engine.Wdl(600, 300, 100), chess.BLACK)
            return {"packets": [row], "best": move}
        service, _ = self.peer(FakeFactory(plan))
        service.update_position(game_id="black", revision=4, board=board)
        value = self.completed(service)
        self.assertEqual((value["cp"], value["turn"]), (-85, "black"))
        self.assertEqual(value["wdl_white"], {"wins": 100, "draws": 300, "losses": 600})
        self.assertEqual(value["white_share"], .25)
        value["pv_uci"].clear()
        self.assertTrue(service.snapshot()["pv_uci"])

    def test_non_stockfish_identity_is_unavailable_and_cannot_select(self):
        service, factory = self.peer(FakeFactory(name="Regicide not an external referee"))
        result = self.screen(service)
        self.assertEqual(result["selected_uci"], "e2e4")
        self.assertEqual(result["reason"], "engine_identity_or_options_failed")
        self.assertFalse(result["available"])
        self.assertTrue(factory.engines[0].closed.is_set())

    def test_packet_requires_own_depth_score_legal_pv_and_root_perspective(self):
        board = chess.Board()
        good = packet(board, "e2e4")
        cases = [{"depth": 11}, {"depth": 11, "score": good["score"]},
                 {**good, "lowerbound": True}, {**good, "depth": True},
                 {**good, "score": chess.engine.PovScore(chess.engine.Cp(20), chess.BLACK)},
                 {**good, "pv": [chess.Move.from_uci("e2e5")]},
                 {**good, "pv": good["pv"] * 129}, {**good, "multipv": 2}]
        for info in cases:
            with self.subTest(info=info):
                self.assertIsNone(referee._packet(board, info, multipv=1))
        self.assertEqual(referee._packet(board, good, multipv=1)["cp"], 20)

    def test_mate_is_separate_from_cp(self):
        board = chess.Board()
        value = referee._packet(board, packet(board, "e2e4", mate_white=3), multipv=1)
        self.assertEqual(value["mate"], 3)
        self.assertIsNone(value["cp"])

    def test_only_regicide_supplied_candidates_can_be_selected(self):
        service, factory = self.peer()
        result = self.screen(service)
        self.assertTrue(result["accepted"])
        self.assertEqual(result["selected_uci"], "d2d4")
        self.assertEqual({row["uci"] for row in result["candidates"]}, {"e2e4", "d2d4"})
        self.assertEqual({move.uci() for move in factory.searches[0]["options"]["root_moves"]}, {"e2e4", "d2d4"})
        self.assertEqual(len(factory.searches), 2)
        self.assertNotIn("root_moves", factory.searches[1]["options"])
        self.assertEqual(factory.searches[1]["board"].peek().uci(), "d2d4")
        self.assertEqual(result["postmove"]["cp_own"], 20)

    def test_black_candidate_and_child_signs_are_consistent(self):
        board = chess.Board()
        board.push_uci("e2e4")
        def plan(position, limit, options, index):
            allowed = options.get("root_moves")
            if allowed:
                return {"packets": [packet(position, allowed[0], -300, rank=1),
                                     packet(position, allowed[1], -20, rank=2)], "best": allowed[0]}
            move = next(iter(position.legal_moves))
            return {"packets": [packet(position, move, -15)], "best": move}
        service, _ = self.peer(FakeFactory(plan))
        result = self.screen(service, board, "e7e5", "d7d5")
        self.assertTrue(result["accepted"])
        self.assertEqual(result["cp_white"], -20)
        self.assertEqual(result["postmove"]["cp_own"], 15)
        self.assertEqual(result["postmove"]["cp_white"], -15)

    def test_baseline_preferred_when_nearest_to_target(self):
        def plan(board, limit, options, index):
            moves = options["root_moves"]
            return {"packets": [packet(board, moves[0], 20, rank=1), packet(board, moves[1], 65, rank=2)], "best": moves[0]}
        service, factory = self.peer(FakeFactory(plan))
        result = self.screen(service)
        self.assertFalse(result["accepted"])
        self.assertEqual(result["selected_uci"], "e2e4")
        self.assertEqual(result["reason"], "reference_prefers_regicide_baseline")
        self.assertEqual(len(factory.searches), 1)

    def test_bad_candidate_requests_preserve_baseline(self):
        service, factory = self.peer()
        for candidates, reason in [(["e2e5"], "illegal_candidate"), (["not-a-move"], "invalid_candidate"),
                                   (["a2a3"] * 5, "candidate_limit"), (["e2e4"], "no_alternative")]:
            result = service.screen_candidates(game_id="game", revision=1, board=chess.Board(),
                                               baseline_move="e2e4", candidates=candidates)
            self.assertEqual(result["reason"], reason)
            self.assertEqual(result["selected_uci"], "e2e4")
        self.assertEqual(factory.engines, [])

    def test_missing_common_depth_or_bound_packet_cannot_certify_candidate(self):
        for kind in ("missing", "depth", "bound", "foreign"):
            def plan(board, limit, options, index, kind=kind):
                moves = options["root_moves"]
                rows = [packet(board, moves[0], 300, rank=1)]
                if kind != "missing":
                    row = packet(board, moves[1], 20, depth=9 if kind == "depth" else 10, rank=2)
                    if kind == "bound":
                        row["upperbound"] = True
                    if kind == "foreign":
                        row["pv"] = [chess.Move.from_uci("g1f3")]
                    rows.append(row)
                return {"packets": rows, "best": moves[0]}
            service, _ = self.peer(FakeFactory(plan))
            result = self.screen(service)
            self.assertEqual(result["reason"], "incomplete_candidate_round")
            self.assertEqual(result["selected_uci"], "e2e4")

    def test_completed_common_round_does_not_borrow_later_partial_depth(self):
        def plan(board, limit, options, index):
            moves = options.get("root_moves")
            if moves:
                return {"packets": [packet(board, moves[0], 300, rank=1), packet(board, moves[1], 20, rank=2),
                                     packet(board, moves[0], 400, depth=11, rank=1)], "best": moves[0]}
            move = next(iter(board.legal_moves))
            return {"packets": [packet(board, move, 20, depth=10)], "best": move}
        service, _ = self.peer(FakeFactory(plan))
        result = self.screen(service)
        self.assertTrue(result["accepted"])
        self.assertEqual(result["depth"], 10)

    def test_postmove_bad_score_or_mate_reverts_to_baseline(self):
        for kind in ("floor", "disagreement", "mate", "shallow"):
            def plan(board, limit, options, index, kind=kind):
                moves = options.get("root_moves")
                if moves:
                    return {"packets": [packet(board, moves[0], 300, rank=1),
                                         packet(board, moves[1], -70 if kind == "disagreement" else 20, rank=2)], "best": moves[0]}
                move = next(iter(board.legal_moves))
                row = packet(board, move, -200 if kind == "floor" else 70 if kind == "disagreement" else 20,
                             depth=2 if kind == "shallow" else 10, mate_white=-3 if kind == "mate" else None)
                return {"packets": [row], "best": move}
            service, _ = self.peer(FakeFactory(plan))
            result = self.screen(service)
            self.assertFalse(result["accepted"])
            self.assertEqual(result["selected_uci"], "e2e4")
            self.assertTrue(result["reason"].startswith("postmove_"))
            self.assertEqual(result["cp"], 300, "rejected proposal score must not label the retained baseline")

    def test_phase_ramp_moves_toward_baseline_and_never_forces_tiny_concession_late(self):
        board = long_board(96)
        moves = list(board.legal_moves)[:2]
        def plan(position, limit, options, index):
            allowed = options["root_moves"]
            sign = 1 if position.turn else -1
            return {"packets": [packet(position, allowed[0], 300 * sign, rank=1),
                                 packet(position, allowed[1], 20 * sign, rank=2)], "best": allowed[0]}
        service, _ = self.peer(FakeFactory(plan))
        result = self.screen(service, board, moves[0], moves[1])
        self.assertEqual(result["selected_uci"], moves[0].uci())
        self.assertEqual(result["reason"], "reference_prefers_regicide_baseline")
        self.assertAlmostEqual(result["band"]["ramp"], 56 / 58)
        self.assertAlmostEqual(result["band"]["target_cp_own"], 20 + 56 / 58 * 280)

    def test_move_fifty_skips_every_external_search(self):
        board = long_board(98)
        moves = list(board.legal_moves)[:2]
        service, factory = self.peer()
        result = self.screen(service, board, moves[0], moves[1])
        self.assertEqual(result["reason"], "full_strength_regicide_baseline")
        self.assertEqual(result["selected_uci"], moves[0].uci())
        self.assertEqual(factory.engines, [])

    def test_explicit_host_cancellation_never_starts_or_selects_an_alternative(self):
        service, factory = self.peer()
        result = self.screen(service, cancelled=lambda: True)
        self.assertEqual(result["selected_uci"], "e2e4")
        self.assertEqual(result["reason"], "cancelled")
        self.assertEqual(factory.engines, [])

    def test_deadline_closes_owned_engine_and_preserves_baseline(self):
        gate = threading.Event()
        def plan(board, limit, options, index):
            moves = options["root_moves"]
            return {"packets": [], "best": moves[0], "gate": gate}
        service, factory = self.peer(FakeFactory(plan))
        start = time.monotonic()
        result = self.screen(service, budget_ms=250)
        self.assertEqual(result["reason"], "timeout")
        self.assertEqual(result["selected_uci"], "e2e4")
        self.assertLess(time.monotonic() - start, .6)
        self.assertTrue(factory.engines[0].closed.is_set())

    def test_live_position_change_discards_old_process_result(self):
        gate = threading.Event()
        def plan(board, limit, options, index):
            move = next(iter(board.legal_moves))
            return {"packets": [packet(board, move, 90 if not board.move_stack else -30)],
                    "best": move, "gate": gate if not board.move_stack else None}
        factory = FakeFactory(plan)
        service, _ = self.peer(factory)
        service.update_position(game_id="game", revision=1, board=chess.Board())
        self.assertTrue(factory.entered.wait(1))
        board = chess.Board()
        board.push_uci("e2e4")
        service.update_position(game_id="game", revision=2, board=board)
        result = self.completed(service)
        self.assertEqual((result["revision"], result["cp"], result["fen"]), (2, -30, board.fen(en_passant="fen")))
        self.assertTrue(factory.engines[0].closed.is_set())
        self.assertEqual(factory.maximum, 1)

    def test_screen_priority_cancels_live_before_starting_its_owned_process(self):
        gate = threading.Event()
        def plan(board, limit, options, index):
            if index == 0:
                move = next(iter(board.legal_moves))
                return {"packets": [packet(board, move)], "best": move, "gate": gate}
            return FakeFactory.default_plan(board, limit, options, index)
        factory = FakeFactory(plan)
        service, _ = self.peer(factory)
        service.update_position(game_id="game", revision=1, board=chess.Board())
        self.assertTrue(factory.entered.wait(1))
        result = self.screen(service)
        service.cancel()
        self.assertTrue(result["accepted"])
        self.assertTrue(factory.engines[0].closed.is_set())
        self.assertEqual(factory.maximum, 1)

    def test_host_position_change_during_screen_prevents_late_selection(self):
        gate = threading.Event()
        def plan(board, limit, options, index):
            moves = options["root_moves"]
            return {"packets": [], "best": moves[0], "gate": gate}
        factory = FakeFactory(plan)
        service, _ = self.peer(factory)
        answers = []
        thread = threading.Thread(target=lambda: answers.append(self.screen(service)), daemon=True)
        thread.start()
        self.assertTrue(factory.entered.wait(1))
        board = chess.Board()
        board.push_uci("e2e4")
        service.update_position(game_id="game", revision=2, board=board, active=False)
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(answers[0]["selected_uci"], "e2e4")
        self.assertFalse(answers[0]["accepted"])

    def test_close_cancels_owned_reference_without_touching_other_peer(self):
        gate = threading.Event()
        def plan(board, limit, options, index):
            move = next(iter(board.legal_moves))
            return {"packets": [packet(board, move)], "best": move, "gate": gate}
        service, factory = self.peer(FakeFactory(plan))
        unrelated = FakeFactory()(self.binary)
        self.addCleanup(unrelated.close)
        service.update_position(game_id="game", revision=1, board=chess.Board())
        self.assertTrue(factory.entered.wait(1))
        service.close()
        service._worker.join(1)
        self.assertTrue(factory.engines[0].closed.is_set())
        self.assertFalse(unrelated.closed.is_set())
        self.assertNotEqual(service.snapshot()["status"], "ready")

    def test_board_and_candidate_inputs_are_unchanged(self):
        service, _ = self.peer()
        board = chess.Board()
        original = board.fen(en_passant="fen"), list(board.move_stack)
        candidates = [chess.Move.from_uci("d2d4")]
        saved = list(candidates)
        service.screen_candidates(game_id="game", revision=1, board=board,
                                  baseline_move="e2e4", candidates=candidates)
        self.assertEqual((board.fen(en_passant="fen"), board.move_stack), original)
        self.assertEqual(candidates, saved)

    def test_changed_binary_cannot_keep_old_hash_metadata_and_start_new_work(self):
        service, factory = self.peer()
        self.binary.write_bytes(b"changed executable payload")
        result = self.screen(service)
        self.assertEqual(result["selected_uci"], "e2e4")
        self.assertFalse(result["available"])
        self.assertEqual(factory.engines, [])

    def test_cancel_during_screen_does_not_resurrect_live_observer(self):
        screen_entered, restarted, gate = threading.Event(), threading.Event(), threading.Event()
        def plan(board, limit, options, index):
            if index >= 2:
                restarted.set()
            if options.get("root_moves"):
                screen_entered.set()
                return {"packets": [], "best": options["root_moves"][0], "gate": gate}
            return FakeFactory.default_plan(board, limit, options, index)
        factory = FakeFactory(plan)
        service, _ = self.peer(factory)
        service.update_position(game_id="game", revision=1, board=chess.Board())
        self.assertEqual(self.completed(service)["status"], "ready")
        answers = []
        thread = threading.Thread(target=lambda: answers.append(self.screen(service)), daemon=True)
        thread.start()
        self.assertTrue(screen_entered.wait(1))
        service.cancel()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertFalse(answers[0]["accepted"])
        self.assertEqual(answers[0]["selected_uci"], "e2e4")
        self.assertFalse(restarted.wait(.1))
        self.assertEqual(len(factory.searches), 2)
        self.assertNotEqual(service.snapshot()["status"], "ready")

    def test_cold_start_does_not_consume_live_search_allowance(self):
        factory = FakeFactory()
        def slow_factory(binary):
            time.sleep(.5)
            return factory(binary)
        service, _ = self.peer(slow_factory)
        service.update_position(game_id="cold", revision=1, board=chess.Board(), budget_ms=100)
        value = self.completed(service)
        self.assertEqual(value["status"], "ready")
        self.assertGreaterEqual(value["startup_ms"], 450)
        self.assertAlmostEqual(factory.searches[0]["limit"].time, .1)

    def test_screen_budget_still_includes_cold_start(self):
        factory = FakeFactory()
        def slow_factory(binary):
            time.sleep(.3)
            return factory(binary)
        service, _ = self.peer(slow_factory)
        result = self.screen(service, budget_ms=250)
        self.assertFalse(result["accepted"])
        self.assertEqual(result["selected_uci"], "e2e4")
        self.assertEqual(result["reason"], "timeout")


if __name__ == "__main__":
    unittest.main()
