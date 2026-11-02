"""Sparring-only precise evaluation integration; controlled UCI evidence."""
from pathlib import Path
import tempfile
import unittest

import chess
import chess.engine

import human_play_tests as base
from analysis_evaluation_tests import StreamingPeer
from hint_state_tests import HintFactory, wait_for
from game_archive import GameArchive

host = base.play_human


def packet(board, cp=30, depth=6, **extra):
    return {"depth": depth, "score": chess.engine.PovScore(chess.engine.Cp(cp), board.turn),
            "pv": [next(iter(board.legal_moves))], **extra}


class SparringEvaluationStateTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="regicide-precise-evaluation-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        binary = self.root / "fake-engine.exe"
        binary.write_bytes(b"controlled test peer; never executed")
        self.helpers = HintFactory()
        self.game = host.HumanGame(binary, self.root / "games", base.FakeFactory(),
                                   hint_engine_factory=base.FakeFactory(),
                                   evaluation_engine_factory=self.helpers,
                                   opening_variety_enabled=False)
        self.addCleanup(self.game.close)
        wait_for(lambda: self.game._live_evaluation.snapshot()["status"] in ("ready", "unavailable"))

    def new(self, sparring=True, color="white"):
        self.game.new_game(color, 1000, self.game.revision, sparring_mode=sparring)

    def engine_position(self, board):
        with self.game._lock:
            self.game.board = board.copy(stack=True)
            self.game.human_color = not board.turn
            self.game._sync_evaluation(status="searching")

    def test_precision_budget_is_independent_and_only_for_sparring(self):
        for sparring, expected in [(True, 10000), (False, 3000)]:
            with self.subTest(sparring=sparring):
                self.new(sparring)
                state = self.game.state()
                value = self.game.evaluation()
                self.assertEqual(value["budget_ms"], expected)
                self.assertEqual(state["engine"]["movetime_ms"], 1000)
                self.assertEqual(self.game._live_evaluation._context["precision"], sparring)

    def test_saved_and_rebuilt_pgn_identify_the_new_analysis_policies(self):
        self.new()
        replay = GameArchive(self.game.save_dir).get_game(self.game.game_id)
        for key, value in (("SparringEvaluationPolicy", host.sparring_evaluation.POLICY),
                           ("SparringVerificationPolicy", host.sparring_play.POSTMOVE_POLICY)):
            header = f'[{key} "{value}"]'
            self.assertIn(header, self.game.pgn())
            self.assertIn(header, replay["pgn"])

    def test_valid_current_root_scores_are_not_clamped_to_the_sparring_band(self):
        self.new()
        for turn in (chess.WHITE, chess.BLACK):
            board = chess.Board()
            if turn == chess.BLACK:
                board.push_uci("e2e4")
            self.engine_position(board)
            self.game._publish_evaluation(packet(board, 675))
            value = self.game.evaluation()
            self.assertEqual(value["cp"], 675 if turn else -675)
            self.assertEqual(value["source_fen"], board.fen(en_passant="fen"))
            self.assertEqual(value["bound"], "exact")

    def test_bad_or_regressing_packets_cannot_replace_valid_score(self):
        self.new()
        board = chess.Board()
        self.engine_position(board)
        self.game._publish_evaluation(packet(board))
        before = self.game.evaluation()
        for bad in [packet(board, 900, depth=5), packet(board, 900, lowerbound=True),
                    packet(board, 900, upperbound=True), packet(board, 900, multipv=2),
                    packet(board, 900, pv=[]), packet(board, 900, pv=[chess.Move.from_uci("e2e5")]),
                    packet(board, score=chess.engine.PovScore(chess.engine.Cp(900), not board.turn)),
                    packet(board, score=chess.engine.PovScore(chess.engine.Mate(1), board.turn))]:
            with self.subTest(bad=bad):
                self.game._publish_evaluation(bad)
                for key in ("cp", "mate", "depth", "source_fen", "white_share", "bound"):
                    self.assertEqual(self.game.evaluation()[key], before[key])
        self.game._publish_evaluation(packet(board, -250, depth=7))
        self.assertEqual(self.game.evaluation()["cp"], -250)

    def test_new_bad_iteration_cannot_retain_stable_label_or_regress_depth(self):
        self.new()
        board = chess.Board()
        self.engine_position(board)
        for depth in (4, 5, 6):
            self.game._publish_evaluation(packet(board, depth=depth))
        self.assertEqual(self.game.evaluation()["reliability"], "stable")
        self.game._publish_evaluation(packet(board, 900, depth=7, lowerbound=True))
        value = self.game.evaluation()
        self.assertEqual((value["cp"], value["depth"], value["reliability"]), (30, 6, "provisional"))
        self.assertTrue(value["retained_previous_score"])
        self.game._publish_evaluation(packet(board, 700, depth=6))
        self.assertEqual(self.game.evaluation(), value)
        self.game._publish_evaluation(packet(board, 30, depth=7))
        self.assertEqual(self.game.evaluation()["reliability"], "stable")
        self.assertFalse(self.game.evaluation()["retained_previous_score"])

    def test_rule_validated_mate_has_correct_black_sign(self):
        self.new()
        board = chess.Board()
        for move in ("f2f3", "e7e5", "g2g4"):
            board.push_uci(move)
        self.engine_position(board)
        self.game._publish_evaluation(packet(board, score=chess.engine.PovScore(chess.engine.Mate(1), chess.BLACK),
                                             pv=[chess.Move.from_uci("d8h4")]))
        value = self.game.evaluation()
        self.assertEqual(value["mate"], -1)
        self.assertEqual(value["winner"], "black")
        self.assertIsNone(value["cp"])

    def test_precise_human_helper_streams_without_waiting_for_completion(self):
        peer = StreamingPeer([packet(chess.Board(), 47)], blocked=True)
        self.helpers.peers.append(peer)
        self.new()
        self.assertTrue(peer.waiting.wait(2))
        value = wait_for(lambda: self.game.evaluation() if self.game.evaluation().get("cp") == 47 else None)
        self.assertEqual(value["status"], "searching")
        self.assertEqual(value["refinement"]["policy"], host.sparring_evaluation.POLICY)
        self.assertEqual(peer.analysis_calls[0][1].time, 10)
        self.assertEqual(self.game.board.fen(), chess.STARTING_FEN)
        self.assertFalse(self.game.board.move_stack)
        peer.release.set()

    def test_cancelled_precision_helper_never_attaches_old_score_to_new_turn(self):
        board = chess.Board()
        peer = StreamingPeer([packet(board)], blocked=True, after_gate=[packet(board, 900, depth=9)])
        self.helpers.peers.append(peer)
        self.new()
        self.assertTrue(peer.waiting.wait(2))
        self.game.human_move("e2e4", self.game.revision)
        peer.release.set()
        wait_for(lambda: self.game._live_evaluation._cancel is None)
        value = self.game.evaluation()
        self.assertIsNone(value["cp"])
        self.assertEqual(value["fen"], self.game.board.fen(en_passant="fen"))
        self.assertEqual(len(self.game.board.move_stack), 1)


if __name__ == "__main__":
    unittest.main()
