"""Deterministic display-source and stale-generation contracts; no real UCI."""
import copy
from pathlib import Path
import sys
import tempfile
import unittest

import chess

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from stockfish_gauge import StockfishGauge, SOURCE
from analysis_session import AnalysisSession
from analysis_evaluation_tests import StreamingPeer, packet
from hint_state_tests import HintFactory, wait_for


class Referee:
    def __init__(self):
        self.value = {"available": True, "engine_name": "Stockfish test", "binary_sha256": "test"}
        self.calls, self.cancelled, self.closed = [], 0, False

    def snapshot(self):
        return copy.deepcopy(self.value)

    def update_position(self, **kwargs):
        self.calls.append(kwargs)
        self.value.update(game_id=kwargs["game_id"], revision=kwargs["revision"],
                          fen=kwargs["board"].fen(en_passant="fen"),
                          turn="white" if kwargs["board"].turn else "black", status="loading",
                          cp=None, mate=None, depth=None, bound=None, white_share=None)

    def finish(self, cp=123, mate=None, **fields):
        self.value.update(status="ready", cp=cp, mate=mate, depth=15, bound="exact",
                          white_share=.5, pv_uci=["e2e4"], uci="e2e4", **fields)

    def cancel(self):
        self.cancelled += 1

    def close(self):
        self.closed = True


class GaugeTests(unittest.TestCase):
    def setUp(self):
        self.peer = Referee()
        self.service = StockfishGauge(referee=self.peer)
        self.addCleanup(self.service.close)

    def select(self, board=None, revision=0, game_id="game", **kwargs):
        return self.service.update_position(game_id=game_id, revision=revision,
                                            board=board or chess.Board(), **kwargs)

    def assert_unknown(self, value):
        for field in ("cp", "mate", "depth", "bound", "white_share"):
            self.assertIsNone(value[field], field)

    def test_white_black_score_whitelist_and_immutable_snapshots(self):
        board = chess.Board()
        for revision, cp in ((0, 321), (1, -456)):
            self.select(board, revision)
            self.peer.finish(cp)
            value = self.service.snapshot()
            self.assertEqual((value["source"], value["cp"], value["revision"], value["turn"]),
                             (SOURCE, cp, revision, "white" if board.turn else "black"))
            self.assertNotIn("pv_uci", value)
            self.assertNotIn("uci", value)
            value["cp"] = 99999
            self.assertEqual(self.service.snapshot()["cp"], cp)
            if revision == 0:
                board.push_uci("e2e4")

    def test_change_undo_and_new_game_reject_old_generation_even_same_fen(self):
        self.select()
        self.peer.finish(800)
        old = self.peer.snapshot()
        board = chess.Board()
        board.push_uci("e2e4")
        self.select(board, 1)
        self.peer.value = old
        self.assert_unknown(self.service.snapshot())
        self.select(revision=2)
        self.peer.value = old
        self.assert_unknown(self.service.snapshot())
        self.select(game_id="new-game", revision=0)
        self.peer.value = old
        self.assert_unknown(self.service.snapshot())

    def test_repetition_path_is_preserved_and_same_fen_without_history_restarts(self):
        board = chess.Board()
        history = ["g1f3", "g8f6", "f3g1", "f6g8"] * 2
        for move in history:
            board.push_uci(move)
        # Avoid a claimable terminal, while still checking a repeated path.
        board.pop()
        self.select(board)
        self.assertTrue(board.can_claim_threefold_repetition())
        self.assertFalse(board.is_repetition(3))
        self.assertEqual(self.service.snapshot()["status"], "loading")
        self.assertEqual([move.uci() for move in self.peer.calls[-1]["board"].move_stack], history[:-1])
        self.select(chess.Board(board.fen(en_passant="fen")))
        self.assertEqual(len(self.peer.calls), 2)

    def test_prospective_fifty_move_claim_does_not_stop_reference(self):
        board = chess.Board("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 99 60")
        self.assertTrue(board.can_claim_fifty_moves())
        value = self.select(board)
        self.assertEqual(value["status"], "loading")
        board.push_uci("a1a2")
        value = self.select(board, revision=1)
        self.assertEqual((value["status"], value["result"]), ("terminal", "1/2-1/2"))

    def test_same_context_does_not_restart_and_budget_is_independent_bounded(self):
        self.select(budget_ms=9000)
        self.select(budget_ms=9000)
        self.assertEqual(len(self.peer.calls), 1)
        self.assertEqual(self.peer.calls[-1]["budget_ms"], 1500)
        self.peer.finish()
        self.assertEqual(self.service.snapshot()["cp"], 123)

    def test_incomplete_current_packet_stays_neutral(self):
        self.select()
        self.peer.value.update(cp=-999, depth=99, status="loading", bound="exact")
        self.assert_unknown(self.service.snapshot())
        self.assertEqual(self.service.snapshot()["status"], "loading")

    def test_invalid_ready_scores_do_not_become_a_gauge(self):
        for fields in ({"bound": "lower"}, {"cp": True}, {"depth": 0},
                       {"cp": None, "mate": 0}, {"available": False}):
            self.select(revision=len(self.peer.calls))
            self.peer.finish()
            self.peer.value.update(fields)
            value = self.service.snapshot()
            self.assertEqual(value["status"], "error")
            self.assert_unknown(value)

    def test_mate_and_terminal_rules_keep_sources_distinct(self):
        self.select()
        self.peer.finish(None, mate=-3)
        self.assertEqual(self.service.snapshot()["winner"], "black")
        board = chess.Board()
        for move in ("f2f3", "e7e5", "g2g4", "d8h4"):
            board.push_uci(move)
        value = self.select(board, revision=1)
        self.assertEqual((value["source"], value["status"], value["result"], value["winner"]),
                         ("chess_rules", "terminal", "0-1", "black"))
        self.assertEqual(value["white_share"], 0)
        self.assertIsNone(value["cp"])

    def test_disabled_cancel_and_close_clear_values_and_late_packets(self):
        self.select()
        self.peer.finish(900)
        old = self.peer.snapshot()
        self.select(revision=1, active=False)
        self.peer.value = old
        self.assert_unknown(self.service.snapshot())
        self.select(revision=2)
        self.service.cancel()
        self.peer.value = old
        self.assert_unknown(self.service.snapshot())
        self.service.close()
        self.assertTrue(self.peer.closed)
        self.assertEqual(self.service.snapshot()["status"], "unavailable")
        self.assert_unknown(self.select(revision=3))

    def test_missing_stockfish_is_explicit_unknown_without_native_fallback(self):
        service = StockfishGauge()
        self.addCleanup(service.close)
        value = service.update_position(game_id="x", revision=0, board=chess.Board())
        self.assertEqual(value["status"], "unavailable")
        self.assert_unknown(value)
        self.assertFalse(service.metadata()["available"])

    def test_owners_have_distinct_worker_tokens_and_cancellation(self):
        other_peer = Referee()
        other = StockfishGauge(referee=other_peer)
        self.addCleanup(other.close)
        self.select()
        other.update_position(game_id="game", revision=0, board=chess.Board())
        self.assertNotEqual(self.peer.calls[0]["game_id"], other_peer.calls[0]["game_id"])
        cancellations = other_peer.cancelled
        self.service.cancel()
        self.assertEqual(other_peer.cancelled, cancellations)


class AnalysisGaugeTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="regicide-gauge-tests-")
        self.addCleanup(self.folder.cleanup)
        self.binary = Path(self.folder.name) / "fake.exe"
        self.binary.write_bytes(b"never executed")
        self.factory = HintFactory()
        self.peer = Referee()
        self.gauge = StockfishGauge(referee=self.peer)
        self.analysis = AnalysisSession(self.binary, "Regicide test", self.factory, gauge=self.gauge)
        self.addCleanup(self.analysis.close)

    def test_native_recommendation_and_score_remain_separate_from_stockfish(self):
        self.factory.peers.append(StreamingPeer([packet(88, depth=7)], move="e2e4"))
        self.analysis.start(chess.STARTING_FEN, 100, 0)
        self.peer.finish(-731)
        state = wait_for(lambda: self.analysis.snapshot() if
                         self.analysis.snapshot()["hint"]["status"] == "ready" else None)
        self.assertEqual(state["hint"]["move"], "e2e4")
        self.assertEqual(state["evaluation"]["cp"], 88)
        self.assertEqual(state["reference_evaluation"]["cp"], -731)
        self.assertEqual(state["reference_evaluation"]["source"], SOURCE)
        self.assertTrue(state["stockfish_reference"]["enabled"])
        self.assertEqual(len(self.factory.instances), 1)
        self.assertNotIn("pv_uci", state["reference_evaluation"])

    def test_move_undo_stop_and_close_invalidate_reference(self):
        self.factory.peers.append(StreamingPeer([packet(88, depth=7)], move="e2e4"))
        state = self.analysis.start(chess.STARTING_FEN, 100, 0)
        self.peer.finish(400)
        old = self.peer.snapshot()
        self.factory.peers.append(StreamingPeer([packet(-30, pov=chess.BLACK)], move="e7e5"))
        state = self.analysis.move("e2e4", state["revision"])
        self.peer.value = old
        self.assertIsNone(self.analysis.snapshot()["reference_evaluation"]["cp"])
        self.factory.peers.append(StreamingPeer([packet(88)], move="e2e4"))
        state = self.analysis.undo(state["revision"])
        self.peer.value = old
        self.assertIsNone(self.analysis.snapshot()["reference_evaluation"]["cp"])
        state = self.analysis.stop(state["revision"])
        self.assertEqual(state["reference_evaluation"]["status"], "idle")
        self.analysis.close()
        self.assertTrue(self.peer.closed)
        self.assertIsNone(self.gauge.snapshot()["cp"])


if __name__ == "__main__":
    unittest.main()
