"""Experimental partial-recovery policy contracts; controllable fake UCI only.

Reuse the v1 fixtures so the new candidate objective remains subject to the
same completed-depth, sign, ownership and postmove checks. These functional
tests do not estimate game balance, playing strength, or human experience.
"""

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import chess
import sparring_referee as referee
import sparring_referee_tests as fixtures


class SparringRefereeV2Tests(unittest.TestCase):
    setUp = fixtures.SparringRefereeTests.setUp
    peer = fixtures.SparringRefereeTests.peer
    screen = fixtures.SparringRefereeTests.screen

    def scored_peer(self, baseline_cp, proposal_cp, child_cp, *, board=None,
                    mate_baseline=None, child_depth=10):
        """All arguments are from the original Regicide mover's viewpoint."""
        board = board if board is not None else chess.Board()
        own_sign = 1 if board.turn else -1
        baseline, proposal = (list(board.legal_moves)[:2] if board.move_stack
                              else [chess.Move.from_uci("e2e4"), chess.Move.from_uci("d2d4")])
        scores = {baseline.uci(): baseline_cp, proposal.uci(): proposal_cp}

        def plan(position, limit, options, index):
            allowed = options.get("root_moves")
            if allowed:
                ranked = sorted(allowed, key=lambda move: -scores[move.uci()])
                packets = [fixtures.packet(position, move, scores[move.uci()] * own_sign,
                    rank=rank, mate_white=(mate_baseline * own_sign
                        if mate_baseline is not None and move == baseline else None))
                    for rank, move in enumerate(ranked, 1)]
                return {"packets": packets, "best": ranked[0]}
            move = next(iter(position.legal_moves))
            return {"packets": [fixtures.packet(position, move, child_cp * own_sign,
                                                 depth=child_depth)], "best": move}

        service, factory = self.peer(fixtures.FakeFactory(plan))
        return service, factory, board, baseline, proposal

    def assert_selection(self, baseline_cp, proposal_cp, child_cp, accepted, *, board=None):
        service, factory, board, baseline, proposal = self.scored_peer(
            baseline_cp, proposal_cp, child_cp, board=board)
        before = board.fen(en_passant="fen"), list(board.move_stack)
        result = self.screen(service, board, baseline, proposal)
        self.assertEqual(result["accepted"], accepted, result)
        self.assertEqual(result["selected_uci"], (proposal if accepted else baseline).uci(), result)
        self.assertEqual((board.fen(en_passant="fen"), board.move_stack), before)
        self.assertEqual(result["cp_white"],
                         (proposal_cp if accepted else baseline_cp) * (1 if board.turn else -1))
        self.assertEqual(result["policy"], "regicide-stockfish-referee-v2")
        self.assertTrue(all(engine.closed.is_set() for engine in factory.engines))
        return result, factory

    def test_ahead_position_can_move_closer_without_entering_the_close_band(self):
        result, factory = self.assert_selection(400, 120, 130, True)
        self.assertEqual(len(factory.searches), 2)
        self.assertEqual(result["postmove"]["cp_own"], 130)
        self.assertEqual(result["postmove"]["disagreement_cp"], 10)
        self.assertEqual(result["band"]["lower_cp_own"], -80)
        self.assertEqual(result["band"]["upper_cp_own"], 80)
        self.assertGreater(result["postmove"]["cp_own"], result["band"]["upper_cp_own"])

    def test_observed_pilot_outside_band_case_is_no_longer_forced_to_baseline(self):
        result, _ = self.assert_selection(254, 135, 140, True)
        self.assertEqual(result["cp"], 135)

    def test_partial_concession_must_keep_the_ahead_position_safety_floor(self):
        result, factory = self.assert_selection(400, 120, -81, False)
        self.assertEqual(len(factory.searches), 2)
        self.assertEqual(result["postmove"]["cp_own"], -81)
        self.assertTrue(result["reason"].startswith("postmove_"), result)

    def test_losing_position_can_improve_even_below_the_close_band(self):
        result, factory = self.assert_selection(-400, -150, -140, True)
        self.assertEqual(len(factory.searches), 2)
        self.assertEqual(result["postmove"]["cp_own"], -140)
        self.assertLess(result["postmove"]["cp_own"], -80)

    def test_partial_recovery_signs_follow_the_original_black_mover(self):
        board = chess.Board()
        board.push_uci("e2e4")
        for baseline_cp, proposal_cp, child_cp in ((400, 120, 130), (-400, -150, -140)):
            with self.subTest(baseline_cp=baseline_cp):
                result, _ = self.assert_selection(baseline_cp, proposal_cp, child_cp, True, board=board)
                self.assertEqual(result["postmove"]["cp_white"], -child_cp)
                self.assertEqual(result["postmove"]["cp_own"], child_cp)

    def test_balanced_baseline_is_not_replaced_by_an_outside_band_candidate(self):
        for proposal_cp in (120, -150):
            with self.subTest(proposal_cp=proposal_cp):
                result, factory = self.assert_selection(0, proposal_cp, proposal_cp, False)
                self.assertEqual(len(factory.searches), 1, "retaining the baseline needs no child search")
                self.assertIsNone(result["postmove"])

    def test_mate_baseline_stays_separate_from_finite_partial_recovery(self):
        service, factory, board, baseline, proposal = self.scored_peer(400, 120, 130, mate_baseline=3)
        result = self.screen(service, board, baseline, proposal)
        self.assertFalse(result["accepted"])
        self.assertEqual(result["selected_uci"], baseline.uci())
        self.assertEqual(result["reason"], "mate_baseline_not_comparable")
        self.assertEqual(len(factory.searches), 1)

    def test_outside_band_child_still_requires_completed_depth(self):
        service, factory, board, baseline, proposal = self.scored_peer(400, 120, 130, child_depth=3)
        result = self.screen(service, board, baseline, proposal)
        self.assertFalse(result["accepted"])
        self.assertEqual(result["selected_uci"], baseline.uci())
        self.assertEqual(result["reason"], "postmove_incomplete")
        self.assertEqual(len(factory.searches), 2)

    def test_partial_recovery_does_not_relax_postmove_disagreement(self):
        result, _ = self.assert_selection(400, 120, 210, False)
        self.assertEqual(result["reason"], "postmove_score_disagreement")

    def test_new_receipts_identify_v2_and_keep_v1_readable(self):
        self.assertEqual(referee.POLICY, "regicide-stockfish-referee-v2")
        self.assertIn("regicide-stockfish-referee-v1", referee.SUPPORTED_POLICIES)
        self.assertIn(referee.POLICY, referee.SUPPORTED_POLICIES)

    # Reuse the original regression assertions verbatim at the critical phase
    # boundaries, rather than loosening them for the new early-game objective.
    test_late_ramp_keeps_baseline_preference = (
        fixtures.SparringRefereeTests.test_phase_ramp_moves_toward_baseline_and_never_forces_tiny_concession_late)
    test_move_fifty_still_skips_every_external_search = (
        fixtures.SparringRefereeTests.test_move_fifty_skips_every_external_search)


if __name__ == "__main__":
    unittest.main()
