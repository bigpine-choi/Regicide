"""Evidence-grounded local character dialogue and isolated retry analysis."""

import copy
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

import chess

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from coach_explanations import describe_move, describe_report
from coaching_session import CoachingSession, CoachingRevisionError, _line
from coaching_tests import Factory, FakeEngine, packet, wait_for, SHORT


def row_for(board, actual, best_pv, played_pv, classification="mistake"):
    move = chess.Move.from_uci(actual)
    return {"ply": len(board.move_stack) + 1, "uci": actual, "san": board.san(move),
            "classification": classification,
            "best": _line(board, packet(100, pv=best_pv), "white"),
            "played": _line(board, packet(-100, pv=played_pv), "white")}


class ExplanationTests(unittest.TestCase):
    def test_mate_is_verified_on_actual_pv_board_and_hint_does_not_leak_solution(self):
        board = chess.Board()
        board.push_uci("f2f3")
        board.push_uci("e7e5")
        row = row_for(board, "g2g4", ["b1c3"], ["g2g4", "d8h4"], "allows_mate")
        coach = describe_move(board, row)
        mate = next(fact for fact in coach["evidence"] if fact["kind"] == "checkmate")
        self.assertEqual((mate["line"], mate["ply_index"]), ("played", 2))
        self.assertIn("e1", mate["squares"])
        self.assertIn("in this line", mate["text"])
        self.assertEqual(coach["mood"], "warning")
        for solution in ("Nc3", "b1c3", "c3"):
            self.assertNotIn(solution, coach["hint"])

    def test_capture_facts_name_piece_square_and_finite_net_material(self):
        board = chess.Board("4k3/8/8/8/8/8/q7/R3K3 w Q - 0 1")
        self.assertTrue(board.is_valid())
        row = row_for(board, "a1a2", ["a1a2"], ["a1a2"], "best")
        coach = describe_move(board, row)
        facts = [fact for fact in coach["evidence"] if fact["line"] == "played"]
        capture = next(fact for fact in facts if fact["kind"] == "capture")
        material = next(fact for fact in facts if fact["kind"] == "material")
        self.assertIn("Black's queen on a2", capture["text"])
        self.assertIn("+9", material["text"])
        self.assertIn("this line's endpoint", material["text"])
        self.assertIn("counting pieces only", material["text"])

    def test_identical_best_and_played_lines_have_one_set_of_actual_evidence(self):
        board = chess.Board()
        for uci in ("f2f3", "e7e5", "g2g4"):
            board.push_uci(uci)
        row = row_for(board, "d8h4", ["d8h4"], ["d8h4"], "best")
        facts = describe_move(board, row)["evidence"]
        self.assertEqual(len(facts), 1)
        self.assertEqual((facts[0]["kind"], facts[0]["line"], facts[0]["ply_index"]),
                         ("checkmate", "played", 1))
        # Shared first moves alone are not sufficient: distinct continuations
        # retain their separate provenance and independently verified facts.
        board = chess.Board("4k3/8/2p5/3p4/4P3/8/8/4K3 w - - 0 1")
        row = row_for(board, "e4d5", ["e4d5"], ["e4d5", "c6d5"], "best")
        self.assertEqual({fact["line"] for fact in describe_move(board, row)["evidence"]},
                         {"best", "played"})

    def test_missed_mate_teaches_attack_and_prefers_best_line_evidence(self):
        board = chess.Board("7k/5K2/6Q1/8/8/8/8/8 w - - 0 1")
        row = row_for(board, "g6g5", ["g6g7"], ["g6g5"], "missed_mate")
        coach = describe_move(board, row)
        self.assertIn("checkmate immediately", coach["dialogue"])
        self.assertIn("Qg7#", coach["dialogue"])
        self.assertIn("checks against Black's king", coach["hint"])
        self.assertNotIn("g7", coach["hint"])
        self.assertIn("h8", coach["focus_squares"])
        report = describe_report([{**row, "color": "white"},
                                  {"ply": 2, "color": "black", "classification": "allows_mate"},
                                  {"ply": 3, "color": "white", "classification": "best"}], [2, 1])
        self.assertIn("White has 1 move to revisit; Black has 1", report["summary"])
        self.assertIn("mating threats", report["summary"])
        self.assertLessEqual(report["summary"].count("."), 3)

    def test_exchange_does_not_claim_net_gain_after_recapture(self):
        board = chess.Board("4k3/8/2p5/3p4/4P3/8/8/4K3 w - - 0 1")
        row = row_for(board, "e4d5", ["e4e5"], ["e4d5", "c6d5"])
        facts = describe_move(board, row)["evidence"]
        played = [fact for fact in facts if fact["line"] == "played"]
        self.assertEqual(sum(fact["kind"] == "capture" for fact in played), 2)
        self.assertFalse(any(fact["kind"] == "material" for fact in played))

    def test_en_passant_castle_promotion_and_check_use_legal_positions(self):
        fixtures = [
            ("4k3/8/8/3pP3/8/8/8/4K3 w - d6 0 1", "e5d6", "capture", "d5"),
            ("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1", "e1g1", "castle", "g1"),
            ("4k3/P7/8/8/8/8/8/4K3 w - - 0 1", "a7a8q", "promotion", "a8"),
        ]
        for fen, uci, kind, square in fixtures:
            board = chess.Board(fen)
            row = row_for(board, uci, [uci], [uci], "best")
            coach = describe_move(board, row)
            fact = next(fact for fact in coach["evidence"] if fact["kind"] == kind)
            self.assertIn(square, fact["squares"])
            self.assertEqual(fact["ply_index"], 1)

    def test_invented_or_malformed_pv_cannot_supply_evidence(self):
        board = chess.Board()
        row = row_for(board, "e2e4", ["d2d4"], ["e2e4"])
        row["played"]["pv_uci"] = ["e2e5"]
        row["played"]["pv_san"] = ["Qh4#"]
        coach = describe_move(board, row)
        self.assertFalse(any(fact["line"] == "played" for fact in coach["evidence"]))
        self.assertNotIn("checkmate", coach["dialogue"])
        self.assertFalse(any(fact["kind"] in ("fork", "pin") for fact in coach["evidence"]))

    def test_lessons_are_bounded_chronological_diverse_and_include_positive(self):
        rows = [{"ply": ply, "classification": "blunder"} for ply in (1, 2, 3, 8, 14, 15)]
        rows += [{"ply": 18, "classification": "best"}, {"ply": 19, "classification": "uncertain"},
                 {"ply": 20, "classification": "pending"}]
        coach = describe_report(rows, [1, 2, 3, 8, 14, 15])
        self.assertEqual(len(coach["lesson_plies"]), 5)
        self.assertEqual(coach["lesson_plies"], sorted(coach["lesson_plies"]))
        self.assertTrue({8, 14, 18}.issubset(coach["lesson_plies"]))
        self.assertNotIn(19, coach["lesson_plies"])
        self.assertNotIn(20, coach["lesson_plies"])
        self.assertIn("So far", coach["summary"])
        self.assertEqual(describe_report([], [])["lesson_plies"], [])


class RetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="regicide-character-")
        self.root = Path(self.temp.name)
        self.binary = self.root / "fake.exe"
        self.binary.write_bytes(b"fake engine")
        self.factory = Factory()
        self.session = CoachingSession(self.binary, "Test Regicide", self.root / "games", self.factory)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.session.close)

    def prepare(self, pgn=SHORT, peer=None, color="white"):
        self.factory.peers.append(peer or FakeEngine([packet(20, pv=["e2e4"])]))
        self.session.start(pgn, color, 500, self.session.revision)
        return wait_for(lambda: self.session.snapshot() if self.session.snapshot()["status"] == "completed" else None)

    def retry(self, peer, uci, ply=1):
        self.factory.peers.append(peer)
        state = self.session.snapshot()
        return self.session.retry(ply, uci, state["revision"], state["job_id"])

    def threaded_retry(self, peer, uci="e2e4"):
        self.factory.peers.append(peer)
        state = self.session.snapshot()
        result = {}
        def task():
            try:
                result["value"] = self.session.retry(1, uci, state["revision"], state["job_id"])
            except Exception as exc:
                result["error"] = exc
        thread = threading.Thread(target=task)
        thread.start()
        self.assertTrue(peer.entered.wait(2))
        return thread, result

    def test_packet_and_cached_commentary_regenerated_from_evidence(self):
        state = self.prepare()
        row = state["moves"][0]
        self.assertEqual(state["coach"]["name"], "Rook")
        self.assertEqual(state["coach"]["lesson_plies"], [1])
        self.assertEqual(set(row["retry_legal_moves"]), {move.uci() for move in chess.Board().legal_moves})
        path = next((self.root / "games/coaching").glob("*.json"))
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["report"]["moves"][0]["coach"] = {"dialogue": "Invented fork!"}
        raw["report"]["moves"][0]["retry_legal_moves"] = ["e2e5"]
        path.write_text(json.dumps(raw), encoding="utf-8")
        cached = self.session.start(SHORT, "white", 500, self.session.revision)
        self.assertTrue(cached["cached"])
        self.assertEqual(cached["moves"], state["moves"])
        self.assertEqual(cached["coach"], state["coach"])
        self.assertEqual(self.factory.calls, 1)

    def test_recommended_good_try_again_uncertain_and_no_solution_leak(self):
        self.prepare()
        choices = [
            ("e2e4", [packet(20, pv=["e2e4"])], "recommended", 0),
            ("d2d4", [packet(50, pv=["e2e4"]), packet(25, pv=["d2d4"])], "good", 25),
            ("f2f3", [packet(50, pv=["e2e4"]), packet(-160, pv=["f2f3"])], "try_again", 210),
            ("c2c4", [packet(50, pv=["e2e4"]), packet(75, pv=["c2c4"])], "uncertain", None),
        ]
        before = self.session.snapshot()
        files = {path.name: path.read_bytes() for path in (self.root / "games/coaching").iterdir()}
        for uci, packets, verdict, loss in choices:
            peer = FakeEngine(packets)
            result = self.retry(peer, uci)
            self.assertEqual((result["verdict"], result["loss_cp"]), (verdict, loss))
            self.assertEqual(result["line"]["uci"], uci)
            for solution in ("e4", "e2e4"):
                self.assertNotIn(solution, result["message"])
            self.assertEqual(self.session.snapshot(), before)
            if len(peer.calls) == 2:
                self.assertEqual(peer.calls[0][0].fen(), peer.calls[1][0].fen())
                self.assertEqual(peer.calls[0][1].time, peer.calls[1][1].time)
                self.assertNotEqual(peer.calls[0][2]["game"], peer.calls[1][2]["game"])
        self.assertEqual({path.name: path.read_bytes() for path in (self.root / "games/coaching").iterdir()}, files)

    def test_black_perspective_and_fresh_best_are_not_stale_report_scores(self):
        pgn = '[SetUp "1"]\n[FEN "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1"]\n[Result "*"]\n\n1... e5 *'
        self.prepare(pgn, FakeEngine([packet(10, color=chess.BLACK, pv=["e7e5"])]), "black")
        result = self.retry(FakeEngine([packet(100, color=chess.BLACK, pv=["c7c5"]),
                                        packet(-150, color=chess.BLACK, pv=["e7e5"])]), "e7e5")
        self.assertEqual((result["verdict"], result["loss_cp"]), ("try_again", 250))
        self.assertEqual(result["best"]["uci"], "c7c5")
        self.assertEqual(result["best"]["score_cp_white"], -100)
        self.assertEqual(self.session.snapshot()["moves"][0]["best"]["uci"], "e7e5")

    def test_retry_preserves_repetition_history(self):
        pgn = '[Result "*"]\n\n1. Nf3 Nf6 2. Ng1 Ng8 3. Nf3 Nf6 4. Ng1 Ng8 5. e4 *'
        self.prepare(pgn, FakeEngine())
        peer = FakeEngine([packet(0, pv=["e2e4"])])
        self.retry(peer, "e2e4", ply=9)
        board = peer.calls[0][0]
        self.assertEqual(len(board.move_stack), 8)
        self.assertTrue(board.is_repetition(3))

    def test_invalid_legal_move_revision_and_job_do_not_launch_engine(self):
        state = self.prepare()
        before_calls = self.factory.calls
        for ply, uci in ((0, "e2e4"), (True, "e2e4"), (1, "e2e5"), (1, "e2e4q"), (1, None)):
            with self.assertRaises(ValueError):
                self.session.retry(ply, uci, state["revision"], state["job_id"])
        with self.assertRaises(CoachingRevisionError):
            self.session.retry(1, "e2e4", state["revision"] - 1, state["job_id"])
        with self.assertRaises(CoachingRevisionError):
            self.session.retry(1, "e2e4", state["revision"], "foreign-job")
        self.assertEqual(self.factory.calls, before_calls)
        self.assertEqual(self.session.snapshot(), state)

    def test_single_retry_and_cancel_suppress_late_result(self):
        state = self.prepare()
        peer = FakeEngine([packet(30, pv=["e2e4"])], blocked=True)
        thread, result = self.threaded_retry(peer)
        with self.assertRaisesRegex(ValueError, "다른 다시 두기"):
            self.session.retry(1, "d2d4", state["revision"], state["job_id"])
        cancelled = self.session.cancel(state["revision"])
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(result.get("error"), CoachingRevisionError)
        self.assertNotIn("value", result)
        self.assertTrue(peer.closed)
        self.assertEqual(self.session.snapshot(), cancelled)

    def test_new_start_and_close_cancel_retry(self):
        state = self.prepare()
        peer = FakeEngine([packet(30, pv=["e2e4"])], blocked=True)
        thread, result = self.threaded_retry(peer)
        newer = self.session.start(SHORT, "white", 500, state["revision"])
        thread.join(2)
        self.assertIsInstance(result.get("error"), CoachingRevisionError)
        self.assertEqual(self.session.snapshot(), newer)
        second = FakeEngine([packet(30, pv=["e2e4"])], blocked=True)
        thread, result = self.threaded_retry(second)
        self.session.close()
        thread.join(2)
        self.assertIsInstance(result.get("error"), CoachingRevisionError)
        self.assertTrue(second.closed)

    def test_retry_deadline_closes_peer_and_clears_concurrency_slot(self):
        self.prepare()
        timers = []
        class Timer:
            def __init__(self, interval, function):
                self.interval, self.function = interval, function
                timers.append(self)
            def start(self): pass
            def cancel(self): pass
        with mock.patch("coaching_session.threading.Timer", Timer):
            peer = FakeEngine([packet(20, pv=["e2e4"])], blocked=True)
            thread, result = self.threaded_retry(peer)
            self.assertEqual(timers[0].interval, 5)
            timers[0].function()
            thread.join(2)
        self.assertIsInstance(result.get("error"), ValueError)
        self.assertIn("초과", str(result["error"]))
        self.assertTrue(peer.closed)
        self.assertEqual(self.retry(FakeEngine([packet(20, pv=["e2e4"])]), "e2e4")["verdict"], "recommended")

    def test_engine_failure_does_not_poison_report_or_next_retry(self):
        before = self.prepare()
        with self.assertRaisesRegex(ValueError, "분석하지 못했습니다"):
            self.retry(FakeEngine(fail=True), "e2e4")
        self.assertEqual(self.session.snapshot(), before)
        self.assertEqual(self.retry(FakeEngine([packet(20, pv=["e2e4"])]), "e2e4")["verdict"], "recommended")

    def test_cancelled_report_allows_only_already_analyzed_rows(self):
        peer = FakeEngine([packet(20, pv=["e2e4"])])
        original_analyse = peer.analyse
        def stop_on_second_position(board, limit, **kwargs):
            if board.move_stack:
                peer.blocked = True
            return original_analyse(board, limit, **kwargs)
        peer.analyse = stop_on_second_position
        self.factory.peers.append(peer)
        self.session.start('[Result "*"]\n\n1. e4 e5 *', "white", 500, 0)
        wait_for(lambda: self.session.snapshot()["progress"]["completed"] == 1 and len(peer.calls) >= 2)
        state = self.session.snapshot()
        with self.assertRaises(ValueError):
            self.session.retry(1, "e2e4", state["revision"], state["job_id"])
        cancelled = self.session.cancel(state["revision"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["moves"][1]["retry_legal_moves"], [])
        self.assertIsNone(cancelled["moves"][1]["coach"])
        self.assertEqual(self.retry(FakeEngine([packet(20, pv=["e2e4"])]), "e2e4")["verdict"], "recommended")
        with self.assertRaises(ValueError):
            self.session.retry(2, "e7e5", cancelled["revision"], cancelled["job_id"])

    def test_underpromotion_is_a_legal_retry_and_requires_promotion_kind(self):
        pgn = '[SetUp "1"]\n[FEN "4k3/P7/8/8/8/8/8/4K3 w - - 0 1"]\n[Result "*"]\n\n1. a8=Q+ *'
        state = self.prepare(pgn, FakeEngine([packet(900, pv=["a7a8q"])]))
        self.assertTrue({"a7a8q", "a7a8r", "a7a8b", "a7a8n"}.issubset(state["moves"][0]["retry_legal_moves"]))
        with self.assertRaises(ValueError):
            self.session.retry(1, "a7a8", state["revision"], state["job_id"])
        result = self.retry(FakeEngine([packet(0, pv=["a7a8n"])]), "a7a8n")
        self.assertEqual(result["san"], "a8=N")
        self.assertEqual(result["line"]["pv_frames"][1]["board"]["a8"], "N")

    def test_cancel_during_engine_construction_closes_late_peer(self):
        state = self.prepare()
        entered, release = threading.Event(), threading.Event()
        peer = FakeEngine()
        def delayed_factory(_binary):
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test release missing")
            return peer
        self.session._factory = delayed_factory
        result = {}
        def task():
            try:
                result["value"] = self.session.retry(1, "e2e4", state["revision"], state["job_id"])
            except Exception as exc:
                result["error"] = exc
        thread = threading.Thread(target=task)
        thread.start()
        self.assertTrue(entered.wait(2))
        cancelled = self.session.cancel(state["revision"])
        release.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(result.get("error"), CoachingRevisionError)
        self.assertTrue(peer.closed)
        self.assertFalse(peer.calls)
        self.assertEqual(self.session.snapshot(), cancelled)


if __name__ == "__main__":
    unittest.main()
