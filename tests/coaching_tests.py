"""Deterministic coaching evidence: comparison, replay, cancellation and cache."""

import copy
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

import chess
import chess.engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from coaching_session import (CoachingSession, CoachingRevisionError, _classify,
                              _annotation, _line, _score, MAX_PV_PLIES, SCHEMA)


PGN = '[White "Human"]\n[Black "Regicide"]\n[Result "0-1"]\n\n1. f3 e5 2. g4 Qh4# 0-1\n'
SHORT = '[Result "*"]\n\n1. e4 *\n'


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(.01)
    raise AssertionError("timed out waiting for coaching")


def packet(cp=0, *, mate=None, color=chess.WHITE, pv=(), **extra):
    score = chess.engine.Cp(cp) if mate is None else chess.engine.Mate(mate)
    return {"score": chess.engine.PovScore(score, color), "depth": 6, "nodes": 100,
            "pv": [chess.Move.from_uci(move) for move in pv], **extra}


class FakeEngine:
    def __init__(self, replies=None, blocked=False, fail=False):
        self.replies = list(replies or [])
        self.blocked = blocked
        self.fail = fail
        self.calls = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.closed = False

    def analyse(self, board, limit, **kwargs):
        self.calls.append((board.copy(stack=True), limit, kwargs))
        self.entered.set()
        if self.blocked:
            if not self.release.wait(5):
                raise TimeoutError("test release missing")
        if self.fail:
            raise chess.engine.EngineTerminatedError("controlled failure")
        if self.replies:
            return self.replies.pop(0)
        move = kwargs.get("root_moves", [next(iter(board.legal_moves))])[0]
        return packet(pv=[move.uci()])

    def close(self):
        self.closed = True
        self.release.set()


class Factory:
    def __init__(self):
        self.peers = []
        self.calls = 0

    def __call__(self, _binary):
        self.calls += 1
        return self.peers.pop(0)


class CoachingTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="regicide-coaching-unit-")
        self.root = Path(self.folder.name)
        self.binary = self.root / "fake.exe"
        self.binary.write_bytes(b"fake engine never executed")
        self.factory = Factory()
        self.session = CoachingSession(self.binary, "Regicide test", self.root / "games", self.factory)
        self.addCleanup(self.folder.cleanup)
        self.addCleanup(self.session.close)

    def ready(self):
        return wait_for(lambda: self.session.snapshot() if self.session.snapshot()["status"] in ("completed", "error") else None)

    def start(self, peer, pgn=SHORT, human="white", ms=500):
        self.factory.peers.append(peer)
        return self.session.start(pgn, human, ms, self.session.revision)

    def test_start_has_every_replay_position_and_pending_rows(self):
        peer = FakeEngine(blocked=True)
        state = self.start(peer, PGN)
        self.assertEqual(state["status"], "running")
        self.assertEqual(state["progress"], {"completed": 0, "total": 4})
        self.assertEqual([row["classification"] for row in state["moves"]], ["pending"] * 4)
        self.assertTrue(all(row["annotation"] is None for row in state["moves"]))
        board = chess.Board()
        for index, uci in enumerate(["f2f3", "e7e5", "g2g4", "d8h4"]):
            self.assertEqual(state["frames"][index]["fen"], board.fen(en_passant="fen"))
            board.push_uci(uci)
        self.assertTrue(board.is_checkmate())
        self.assertEqual(state["frames"][4]["fen"], board.fen(en_passant="fen"))
        self.assertEqual(state["settings"]["estimated_seconds"], 4)

    def test_white_loss_uses_same_root_and_fresh_search_game_token(self):
        peer = FakeEngine([packet(150, pv=["d2d4", "d7d5"]), packet(-75, pv=["e2e4", "e7e5"])])
        self.start(peer)
        state = self.ready()
        row = state["moves"][0]
        self.assertEqual((row["classification"], row["loss_cp"]), ("blunder", 225))
        self.assertEqual(state["critical_plies"], [1])
        self.assertEqual(len(peer.calls), 2)
        self.assertEqual(peer.calls[0][0].fen(), peer.calls[1][0].fen())
        self.assertEqual(peer.calls[0][1].time, peer.calls[1][1].time)
        self.assertNotEqual(peer.calls[0][2]["game"], peer.calls[1][2]["game"])
        self.assertEqual(peer.calls[1][2]["root_moves"], [chess.Move.from_uci("e2e4")])
        self.assertEqual(row["best"]["pv_frames"][0]["fen"], row["fen_before"])
        self.assertEqual(row["played"]["pv_frames"][1]["fen"], row["fen_after"])
        self.assertIn("2.25", row["explanation"])

    def test_black_loss_normalized_to_white_perspective(self):
        pgn = '[SetUp "1"]\n[FEN "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1"]\n[Result "*"]\n\n1... e5 *'
        peer = FakeEngine([packet(150, color=chess.BLACK, pv=["c7c5"]), packet(-70, color=chess.BLACK, pv=["e7e5"])])
        self.start(peer, pgn, human="black")
        state = self.ready()
        row = state["moves"][0]
        self.assertEqual(row["best"]["score_cp_white"], -150)
        self.assertEqual(row["played"]["score_cp_white"], 70)
        self.assertEqual((row["classification"], row["loss_cp"]), ("blunder", 220))
        self.assertEqual(state["summary"]["human_analyzed"], 1)

    def test_best_same_move_compares_all_other_roots_without_researching_actual(self):
        peer = FakeEngine([packet(24, pv=["e2e4", "e7e5"])])
        self.start(peer)
        row = self.ready()["moves"][0]
        self.assertEqual((row["classification"], row["loss_cp"]), ("best", 0))
        self.assertEqual(len(peer.calls), 2)
        self.assertEqual(set(peer.calls[1][2]["root_moves"]),
                         set(chess.Board().legal_moves) - {chess.Move.from_uci("e2e4")})
        self.assertEqual(peer.calls[0][0].fen(), peer.calls[1][0].fen())
        self.assertEqual(peer.calls[0][1].time, peer.calls[1][1].time)
        self.assertNotEqual(peer.calls[0][2]["game"], peer.calls[1][2]["game"])
        self.assertEqual(row["best"], row["played"])
        self.assertEqual(row["annotation"]["symbol"], "★")
        state = self.session.snapshot()
        state["moves"][0]["best"]["pv_frames"][0]["board"].clear()
        self.assertTrue(self.session.snapshot()["moves"][0]["best"]["pv_frames"][0]["board"])

    def test_great_candidate_needs_exact_nonlosing_best_and_losing_alternative(self):
        self.session._cache_dir = None
        fixtures = [
            (packet(0, pv=["e2e4"]), packet(-200, pv=["d2d4"]), "!!", 2),
            (packet(100, pv=["e2e4"]), packet(-199, pv=["d2d4"]), "★", 2),
            (packet(-1, pv=["e2e4"]), packet(-300, pv=["d2d4"]), "★", 1),
            (packet(100, pv=["e2e4"], lowerbound=True), packet(-300, pv=["d2d4"]), "…", 1),
            (packet(100, pv=["e2e4"]), packet(-300, pv=["d2d4"], upperbound=True), "★", 2),
            (packet(mate=3, pv=["e2e4"]), packet(mate=-4, pv=["d2d4"]), "!!", 2),
            (packet(mate=-3, pv=["e2e4"]), packet(mate=-4, pv=["d2d4"]), "★", 1),
            (packet(100, pv=["e2e4"]), packet(200, pv=["d2d4"]), "★", 2),
            (packet(100, pv=["e2e4"]), packet(-300, pv=["e2e4"]), "★", 2),
            (packet(100, pv=["e2e4"]), packet(-300, pv=["d2d5"]), "★", 2),
        ]
        for best, alternative, symbol, calls in fixtures:
            with self.subTest(symbol=symbol, best=best, alternative=alternative):
                peer = FakeEngine([best, alternative])
                self.start(peer)
                row = self.ready()["moves"][0]
                self.assertEqual(row["annotation"]["symbol"], symbol)
                self.assertEqual(len(peer.calls), calls)
                self.assertNotEqual(row["classification"], "great")
                if symbol == "!!":
                    self.assertEqual(row["classification"], "best")
                    self.assertEqual(row["annotation"]["label"], "탁월 후보")
                    self.assertEqual(row["annotation"]["kind"], "great")
                    self.assertIn("같은 제한 시간", row["annotation"]["reason"])
                    self.assertIn("증명한 것은 아니며", row["annotation"]["reason"])

    def test_great_candidate_uses_black_perspective_and_exact_actual_frame(self):
        pgn = '[SetUp "1"]\n[FEN "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1"]\n[Result "*"]\n\n1... e5 *'
        peer = FakeEngine([packet(20, color=chess.BLACK, pv=["e7e5"]),
                           packet(-220, color=chess.BLACK, pv=["c7c5"])])
        self.start(peer, pgn, human="white")
        row = self.ready()["moves"][0]
        annotation = row["annotation"]
        self.assertEqual((annotation["symbol"], annotation["square"], annotation["uci"]), ("!!", "e5", "e7e5"))
        self.assertEqual((annotation["ply"], annotation["fen_after"]), (row["ply"], row["fen_after"]))
        self.assertEqual(annotation["scope"], "actual_move")
        self.assertTrue(annotation["provisional"])
        self.assertEqual(row["alternative"]["score_cp_white"], 220)

    def test_only_legal_move_is_never_great_and_needs_no_alternative_search(self):
        pgn = '[SetUp "1"]\n[FEN "r7/8/8/8/8/2k5/8/K7 w - - 0 1"]\n[Result "*"]\n\n1. Kb1 *'
        peer = FakeEngine([packet(0, pv=["a1b1"])])
        self.start(peer, pgn)
        row = self.ready()["moves"][0]
        self.assertEqual(len(peer.calls), 1)
        self.assertIsNone(row["alternative"])
        self.assertEqual(row["annotation"]["symbol"], "★")

    def test_annotation_mappings_and_special_move_destination(self):
        board = chess.Board()
        expected = {"best": "★", "good": "✓", "inaccuracy": "?!", "mistake": "?",
                    "blunder": "??", "allows_mate": "??", "missed_mate": "?", "uncertain": "…"}
        best = _line(board, packet(0, pv=["e2e4"]), "white")
        row = {"ply": 1, "uci": "e2e4", "fen_after": "after", "best": best, "alternative": None}
        for kind, symbol in expected.items():
            self.assertEqual(_annotation(board, {**row, "classification": kind})["symbol"], symbol)
        self.assertIsNone(_annotation(board, {**row, "classification": "pending"}))
        for fen, uci, square in [
                ("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1", "e1g1", "g1"),
                ("r3k2r/8/8/8/8/8/8/R3K2R b KQkq - 0 1", "e8c8", "c8"),
                ("4k3/8/8/3pP3/8/8/8/4K3 w - d6 0 1", "e5d6", "d6"),
                ("4k3/P7/8/8/8/8/8/4K3 w - - 0 1", "a7a8n", "a8")]:
            before = chess.Board(fen)
            after = before.copy()
            after.push_uci(uci)
            annotation = _annotation(before, {**row, "uci": uci, "fen_after": after.fen(en_passant="fen"), "classification": "good"})
            self.assertEqual(annotation["square"], square)
            self.assertEqual(annotation["fen_after"], after.fen(en_passant="fen"))

    def test_great_cache_regenerates_annotations_and_rejects_wrong_alternative(self):
        self.start(FakeEngine([packet(20, pv=["e2e4"]), packet(-250, pv=["d2d4"])]))
        state = self.ready()
        path = next((self.root / "games/coaching").glob("*.json"))
        stored = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(stored["identity"]["schema"], SCHEMA)
        self.assertGreater(SCHEMA, 1)
        stored["report"]["moves"][0]["annotation"] = {"symbol": "wrong", "square": "a1"}
        stored["report"]["moves"][0]["alternative"]["pv_frames"][1]["board"].clear()
        path.write_text(json.dumps(stored), encoding="utf-8")
        reused = self.session.start(SHORT, "white", 500, self.session.revision)
        self.assertTrue(reused["cached"])
        self.assertEqual(reused["moves"], state["moves"])
        self.assertEqual(self.factory.calls, 1)
        for replacement in (stored["report"]["moves"][0]["best"], {**stored["report"]["moves"][0]["alternative"], "pv_uci": ["d2d5"]}):
            changed = copy.deepcopy(stored)
            changed["report"]["moves"][0]["alternative"] = replacement
            path.write_text(json.dumps(changed), encoding="utf-8")
            fresh = self.start(FakeEngine([packet(20, pv=["e2e4"]), packet(0, pv=["d2d4"])]))
            self.assertFalse(fresh["cached"])
            self.assertEqual(self.ready()["moves"][0]["annotation"]["symbol"], "★")
        changed = copy.deepcopy(stored)
        changed["report"]["moves"][0]["played"]["score_cp_white"] = -900
        path.write_text(json.dumps(changed), encoding="utf-8")
        fresh = self.start(FakeEngine([packet(20, pv=["e2e4"]), packet(0, pv=["d2d4"])]))
        self.assertFalse(fresh["cached"])
        self.assertEqual(self.ready()["moves"][0]["annotation"]["symbol"], "★")

    def test_cancel_during_alternative_search_never_publishes_candidate(self):
        class BlockingAlternative(FakeEngine):
            def __init__(self):
                super().__init__([packet(10, pv=["e2e4"])])
                self.alternative_entered = threading.Event()
            def analyse(self, board, limit, **kwargs):
                if self.calls:
                    self.blocked = True
                    self.alternative_entered.set()
                return super().analyse(board, limit, **kwargs)
        peer = BlockingAlternative()
        self.start(peer)
        self.assertTrue(peer.alternative_entered.wait(2))
        cancelled = self.session.cancel(self.session.revision)
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertIsNone(cancelled["moves"][0]["annotation"])
        self.session._worker.join(2)
        self.assertFalse(self.session._worker.is_alive())
        self.assertEqual(self.session.snapshot(), cancelled)

    def test_loss_thresholds_and_search_inconsistency(self):
        for loss, kind in [(0, "good"), (49, "good"), (50, "inaccuracy"), (99, "inaccuracy"),
                           (100, "mistake"), (199, "mistake"), (200, "blunder"), (-1, "uncertain")]:
            best = _line(chess.Board(), packet(100, pv=["d2d4"]), "white")
            played = _line(chess.Board(), packet(100 - loss, pv=["e2e4"]), "white")
            actual = _classify(best, played, "white", False)
            self.assertEqual(actual, (kind, loss if loss >= 0 else None))

    def test_mates_have_winner_not_giant_cp(self):
        board = chess.Board()
        best = _line(board, packet(mate=3, pv=["d2d4"]), "white")
        played = _line(board, packet(1000, pv=["e2e4"]), "white")
        self.assertEqual(_classify(best, played, "white", False), ("missed_mate", None))
        played = _line(board, packet(mate=-1, pv=["e2e4"]), "white")
        finite = _line(board, packet(-900, pv=["d2d4"]), "white")
        self.assertEqual(_classify(finite, played, "white", False), ("allows_mate", None))
        self.assertIsNone(played["score_cp_white"])
        self.assertEqual(played["winner"], "black")
        white_mate_zero = _score({"score": chess.engine.PovScore(chess.engine.MateGiven, chess.WHITE)})
        black_mate_zero = _score({"score": chess.engine.PovScore(chess.engine.Mate(0), chess.WHITE)})
        self.assertEqual((white_mate_zero["mate_white"], black_mate_zero["mate_white"]), (0, 0))
        self.assertEqual((white_mate_zero["winner"], black_mate_zero["winner"]), ("white", "black"))

    def test_bound_or_illegal_line_withholds_grade(self):
        for reply in [packet(100, pv=["d2d4"], lowerbound=True), packet(100, pv=["d2d5"]),
                      {"pv": [chess.Move.from_uci("d2d4")]}, packet(100, pv=[])]:
            peer = FakeEngine([reply, packet(0, pv=["e2e4"])])
            self.start(peer)
            row = self.ready()["moves"][0]
            self.assertEqual(row["classification"], "uncertain")
            # Avoid cache reuse in this loop by removing only own temporary report.
            for cache in (self.root / "games/coaching").glob("*.json"):
                cache.unlink()
        bound = _score(packet(20, color=chess.BLACK, lowerbound=True))
        self.assertEqual(bound["bound"], "upper")

    def test_forced_line_must_start_at_played_move(self):
        peer = FakeEngine([packet(120, pv=["d2d4"]), packet(0, pv=["c2c4"])])
        self.start(peer)
        row = self.ready()["moves"][0]
        self.assertEqual(row["classification"], "uncertain")
        self.assertIsNone(row["played"])

    def test_atomic_validation_and_revisions(self):
        peer = FakeEngine(blocked=True)
        self.start(peer)
        self.assertTrue(peer.entered.wait(2))
        before = self.session.snapshot()
        for pgn, color, ms, revision in [("bad chess", "white", 500, self.session.revision),
                                          (SHORT, "pink", 500, self.session.revision),
                                          (SHORT, "white", True, self.session.revision),
                                          (SHORT, "white", 800, self.session.revision)]:
            with self.assertRaises(ValueError):
                self.session.start(pgn, color, ms, revision)
            self.assertEqual(self.session.snapshot(), before)
        with self.assertRaises(CoachingRevisionError):
            self.session.cancel(self.session.revision - 1)
        self.assertFalse(peer.closed)

    def test_cancel_bounded_and_late_worker_cannot_publish(self):
        peer = FakeEngine([packet(100, pv=["e2e4"])], blocked=True)
        self.start(peer)
        self.assertTrue(peer.entered.wait(2))
        started = time.monotonic()
        cancelled = self.session.cancel(self.session.revision)
        self.assertLess(time.monotonic() - started, .5)
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertTrue(peer.closed)
        next_peer = FakeEngine([packet(50, pv=["e2e4"])])
        initial = self.start(next_peer)
        state = self.ready()
        self.assertEqual(state["job_id"], initial["job_id"])
        self.assertEqual(state["moves"][0]["best"]["score_cp_white"], 50)

    def test_engine_failure_preserves_replay_and_reports_error(self):
        self.start(FakeEngine(fail=True), PGN)
        state = self.ready()
        self.assertEqual(state["status"], "error")
        self.assertEqual(len(state["frames"]), 5)
        self.assertTrue(state["error"])
        self.assertFalse(state["critical_plies"])

    def test_complete_cache_reuses_result_and_preserves_originals(self):
        game_dir = self.root / "games"
        game_dir.mkdir()
        archive = game_dir / "original.json"
        archive.write_text('{"not":"modified"}')
        self.start(FakeEngine([packet(12, pv=["e2e4"])]))
        first = self.ready()
        second = self.session.start(SHORT, "white", 500, self.session.revision)
        self.assertTrue(second["cached"])
        self.assertEqual(second["status"], "completed")
        self.assertEqual(first["moves"], second["moves"])
        self.assertEqual(self.factory.calls, 1)
        self.assertEqual(archive.read_text(), '{"not":"modified"}')

    def test_cache_evaluator_and_profile_identity(self):
        self.start(FakeEngine([packet(12, pv=["e2e4"])]))
        self.ready()
        second_factory = Factory()
        second_factory.peers.append(FakeEngine([packet(120, pv=["e2e4"])]))
        second = CoachingSession(self.binary, "Regicide test", self.root / "games", second_factory, evaluation_id="new-model-sha")
        self.addCleanup(second.close)
        state = second.start(SHORT, "white", 500, 0)
        self.assertFalse(state["cached"])
        wait_for(lambda: second.snapshot()["status"] == "completed")
        self.assertEqual(second_factory.calls, 1)
        peer = FakeEngine([packet(30, pv=["e2e4"])])
        result = self.start(peer, ms=1500)
        self.assertFalse(result["cached"])
        self.ready()

    def test_corrupt_cache_is_ignored_and_failed_cache_write_is_nonfatal(self):
        self.start(FakeEngine([packet(12, pv=["e2e4"])]))
        self.ready()
        path = next((self.root / "games/coaching").glob("*.json"))
        path.write_text('{"corrupt":true}')
        result = self.start(FakeEngine([packet(14, pv=["e2e4"])]))
        self.assertFalse(result["cached"])
        self.assertEqual(self.ready()["moves"][0]["best"]["score_cp_white"], 14)
        self.session._cache_dir = self.binary  # A file cannot be a cache directory.
        self.start(FakeEngine([packet(15, pv=["e2e4"])]), ms=1500)
        self.assertEqual(self.ready()["status"], "completed")

    def test_cached_illegal_pv_is_rejected_and_display_fields_recomputed(self):
        self.start(FakeEngine([packet(12, pv=["e2e4"])]))
        self.ready()
        path = next((self.root / "games/coaching").glob("*.json"))
        stored = json.loads(path.read_text(encoding="utf-8"))
        altered = copy.deepcopy(stored)
        altered["report"]["moves"][0]["best"]["pv_uci"] = ["e2e5"]
        path.write_text(json.dumps(altered), encoding="utf-8")
        result = self.start(FakeEngine([packet(18, pv=["e2e4"])]))
        self.assertFalse(result["cached"])
        self.assertEqual(self.ready()["moves"][0]["best"]["score_cp_white"], 18)
        altered = json.loads(path.read_text(encoding="utf-8"))
        row = altered["report"]["moves"][0]
        row["classification"], row["label"], row["explanation"] = "blunder", "wrong", "invented claim"
        row["best"]["pv_frames"][0]["board"].clear()
        path.write_text(json.dumps(altered), encoding="utf-8")
        reused = self.session.start(SHORT, "white", 500, self.session.revision)
        self.assertTrue(reused["cached"])
        self.assertEqual(reused["moves"][0]["classification"], "best")
        self.assertNotIn("invented", reused["moves"][0]["explanation"])
        self.assertTrue(reused["moves"][0]["best"]["pv_frames"][0]["board"])

    def test_opponent_mate_loss_included_in_both_side_critical_list(self):
        pgn = '[SetUp "1"]\n[FEN "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1"]\n[Result "*"]\n\n1... e5 *'
        peer = FakeEngine([packet(150, color=chess.BLACK, pv=["c7c5"]), packet(mate=-3, color=chess.BLACK, pv=["e7e5"])])
        self.start(peer, pgn, human="white")
        state = self.ready()
        self.assertEqual(state["moves"][0]["classification"], "allows_mate")
        self.assertEqual(state["moves"][0]["played"]["winner"], "white")
        self.assertEqual(state["critical_plies"], [1])
        self.assertEqual(state["summary"]["human_analyzed"], 0)
        self.assertEqual(state["summary"]["opponent_analyzed"], 1)
        self.assertEqual(state["summary"]["human_critical_count"], 0)
        self.assertEqual(state["summary"]["opponent_critical_count"], 1)
        self.assertEqual(state["summary"]["by_color"]["black"]["critical_plies"], [1])

    def test_both_sides_blunders_are_ranked_with_correct_perspective_and_cached(self):
        pgn = '[Result "*"]\n\n1. e4 e5 2. Nf3 Nc6 *'
        peer = FakeEngine([
            packet(120, pv=["d2d4"]), packet(-180, pv=["e2e4"]),
            packet(100, color=chess.BLACK, pv=["c7c5"]),
            packet(-250, color=chess.BLACK, pv=["e7e5"]),
            packet(0, pv=["g1f3"]), packet(0, pv=["b1c3"]),
            packet(0, color=chess.BLACK, pv=["b8c6"]), packet(0, color=chess.BLACK, pv=["g8f6"]),
        ])
        self.start(peer, pgn, human="white")
        state = self.ready()
        self.assertEqual([(row["classification"], row["loss_cp"]) for row in state["moves"]],
                         [("blunder", 300), ("blunder", 350), ("best", 0), ("best", 0)])
        self.assertEqual(state["critical_plies"], [2, 1])
        summary = state["summary"]
        self.assertEqual((summary["total_analyzed"], summary["human_analyzed"], summary["opponent_analyzed"]), (4, 2, 2))
        self.assertEqual((summary["critical_count"], summary["human_critical_count"], summary["opponent_critical_count"]), (2, 1, 1))
        for color, ply in (("white", 1), ("black", 2)):
            side = summary["by_color"][color]
            self.assertEqual((side["analyzed"], side["critical_count"], side["critical_plies"]), (2, 1, [ply]))
            self.assertEqual((side["counts"]["blunder"], side["counts"]["best"]), (1, 1))
        self.assertEqual(summary["all_counts"]["blunder"], 2)
        self.assertEqual(summary["counts"], summary["human_counts"])
        self.assertEqual(summary["opponent_counts"], summary["by_color"]["black"]["counts"])
        self.assertIn("상대 수 2개", summary["message"])
        # Older cached searches remain usable; their one-sided summaries and
        # critical list are rebuilt without launching another engine search.
        path = next((self.root / "games/coaching").glob("*.json"))
        cached = json.loads(path.read_text(encoding="utf-8"))
        cached["report"]["summary"] = {"counts": summary["human_counts"], "human_analyzed": 2, "critical_count": 1}
        cached["report"]["critical_plies"] = [1]
        path.write_text(json.dumps(cached), encoding="utf-8")
        result = self.session.start(pgn, "white", 500, self.session.revision)
        self.assertTrue(result["cached"])
        self.assertEqual(self.factory.calls, 1)
        self.assertEqual(result["critical_plies"], [2, 1])
        self.assertEqual(result["summary"], summary)

    def test_full_history_repetition_castle_ep_and_promotion_frames(self):
        for pgn, expected in [
            ('[Result "*"]\n\n1. Nf3 Nf6 2. Ng1 Ng8 3. Nf3 Nf6 *', "f6"),
            ('[SetUp "1"]\n[FEN "r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1"]\n[Result "*"]\n\n1. O-O O-O-O *', "c8"),
            ('[SetUp "1"]\n[FEN "4k3/8/8/3pP3/8/8/8/4K3 w - d6 0 1"]\n[Result "*"]\n\n1. exd6 *', "d6"),
            ('[SetUp "1"]\n[FEN "4k3/P7/8/8/8/8/8/4K3 w - - 0 1"]\n[Result "*"]\n\n1. a8=Q+ *', "a8")]:
            peer = FakeEngine()
            self.start(peer, pgn)
            state = self.ready()
            self.assertEqual(state["status"], "completed")
            self.assertIn(expected, state["frames"][-1]["board"])
            for board, _limit, _kwargs in peer.calls:
                self.assertEqual(len(board.move_stack), next(i for i, frame in enumerate(state["frames"]) if frame["fen"] == board.fen(en_passant="fen")))
            if "exd6" in pgn:
                self.assertNotIn("d5", state["frames"][-1]["board"])
            if "a8=Q" in pgn:
                self.assertEqual(state["frames"][-1]["board"]["a8"], "Q")

    def test_pv_clipped_to_documented_bound(self):
        board = chess.Board()
        moves = ["g1f3", "g8f6", "f3g1", "f6g8"] * 4
        line = _line(board, packet(0, pv=moves), "white")
        self.assertEqual(len(line["pv_uci"]), MAX_PV_PLIES)
        self.assertEqual(len(line["pv_frames"]), MAX_PV_PLIES + 1)
        self.assertTrue(line["pv_truncated"])


if __name__ == "__main__":
    unittest.main()
