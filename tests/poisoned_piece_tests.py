"""Legal poisoned-piece hypotheses require a real all-defense tactical proof."""
from pathlib import Path
import sys
import time
import unittest

import chess
import chess.engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import poisoned_piece as poison


def legal_trap():
    board = chess.Board()
    for san in "e4 e5 Nf3 d6 Bc4 Bg4 Nc3 g6".split():
        board.push_san(san)
    return board


def mate_info(board):
    after = board.copy(stack=True)
    after.push_uci("f3e5")
    after.push_uci("g4d1")
    pv = []
    for san in ("Bxf7+", "Ke7", "Nd5#"):
        move = after.parse_san(san)
        pv.append(move)
        after.push(move)
    return {"score": chess.engine.PovScore(chess.engine.Mate(2), chess.WHITE),
            "depth": 6, "pv": pv}


class PoisonedPieceTests(unittest.TestCase):
    def test_offer_can_expose_queen_behind_moved_knight(self):
        board = legal_trap()
        before = board.fen(), tuple(board.move_stack)
        rows = poison.enumerate_offers(board, chess.Move.from_uci("f3e5"))
        self.assertLessEqual(len(rows), 2)
        self.assertEqual(rows[0]["uci"], "f3e5")
        self.assertEqual(rows[0]["capture_uci"], "g4d1")
        self.assertEqual(rows[0]["offered_piece"], "queen")
        self.assertTrue(rows[0]["newly_exposed"])
        self.assertFalse(rows[0]["moved_piece_is_bait"])
        self.assertEqual(rows[0]["apparent_capture_gain_cp"], 600)
        self.assertEqual((board.fen(), tuple(board.move_stack)), before)

    def test_native_mate_claim_is_verified_against_every_legal_defense(self):
        board = legal_trap()
        result = poison.validate_capture_branch(board, "f3e5", "g4d1", mate_info(board))
        self.assertTrue(result["verified"], result)
        self.assertEqual(result["kind"], "forced_mate")
        self.assertEqual(result["proof_witness_uci"], ["c4f7", "e8e7", "c3d5"])
        self.assertEqual(result["minimum_payoff_moves"], 2)
        self.assertEqual(result["maximum_payoff_moves"], 2)
        self.assertTrue(result["delayed_payoff"])
        self.assertFalse(result["deep_combination"])

    def test_unsound_offer_still_has_forced_punishment_if_queen_is_taken(self):
        board = chess.Board()
        for san in "e4 e5 Nf3 Nc6 Bc4 d6 Nc3 Bg4".split():
            board.push_san(san)
        # ...Nxe5 is a legal refusal. It does not invalidate the conditional
        # mate after the different, tempting ...Bxd1 capture.
        refusal = board.copy(stack=True)
        refusal.push_uci("f3e5")
        self.assertIn(chess.Move.from_uci("c6e5"), refusal.legal_moves)
        self.assertTrue(poison.validate_capture_branch(
            board, "f3e5", "g4d1", mate_info(board))["verified"])

    def test_pawn_taking_knight_opens_bishop_ray_to_rook(self):
        board = chess.Board("k5r1/5p2/8/8/5N2/1B6/8/7K w - - 0 1")
        self.assertTrue(board.is_valid())
        before = board.fen(), tuple(board.move_stack)
        offer = poison.enumerate_offers(board, "f4g6")[0]
        self.assertEqual(offer["capture_uci"], "f7g6")
        self.assertEqual(offer["apparent_capture_gain_cp"], 300)
        self.assertEqual(offer["opened_slider_rays"], [{
            "slider_square": "b3", "slider_piece": "bishop", "vacated_square": "f7",
            "target_square": "g8", "target_piece": "rook", "target_cp": 500, "ray_length": 5}])
        info = {"depth": 6, "score": chess.engine.PovScore(chess.engine.Cp(0), chess.WHITE),
                "pv": [chess.Move.from_uci("b3g8")]}
        result = poison.validate_capture_branch(board, "f4g6", "f7g6", info)
        self.assertTrue(result["verified"], result)
        self.assertEqual(result["kind"], "forced_material_gain")
        self.assertEqual(result["required_final_delta_cp"], 150)
        self.assertEqual(result["required_countergain_cp"], 450)
        self.assertEqual(result["pv_final_delta_cp"], 200)
        self.assertEqual(result["pv_countergain_cp"], 500)
        self.assertEqual(result["minimum_payoff_moves"], 1)
        self.assertFalse(result["delayed_payoff"])
        self.assertEqual((board.fen(), tuple(board.move_stack)), before)

    def test_rook_gain_is_rejected_when_defender_can_recapture_bishop(self):
        board = chess.Board("6rk/5p2/8/8/5N2/1B6/8/7K w - - 0 1")
        info = {"depth": 6, "score": chess.engine.PovScore(chess.engine.Cp(500), chess.WHITE),
                "pv": [chess.Move.from_uci("b3g8")]}
        result = poison.validate_capture_branch(board, "f4g6", "f7g6", info)
        self.assertFalse(result["verified"], result)
        self.assertEqual(result["reason"], "not_forcing")

    def test_material_recovery_is_valid_even_when_native_score_is_negative(self):
        # The same motif with an extra black bishop: improving the material
        # balance is useful evidence, not a claim that White wins the game.
        board = chess.Board("k5r1/b4p2/8/8/5N2/1B6/8/7K w - - 0 1")
        self.assertTrue(board.is_valid())
        info = {"depth": 6, "score": chess.engine.PovScore(chess.engine.Cp(-900), chess.WHITE),
                "pv": [chess.Move.from_uci("b3g8")]}
        result = poison.validate_capture_branch(board, "f4g6", "f7g6", info)
        self.assertTrue(result["verified"], result)

    def test_delayed_mating_strategy_records_shorter_and_longer_defenses(self):
        board = chess.Board("5r1k/6pp/4Q3/1p4N1/8/1B6/8/K7 w - - 0 1")
        pv = [chess.Move.from_uci(uci) for uci in
              "g5f7 h8g8 f7h6 g8h8 e6g8 f8g8 h6f7".split()]
        info = {"depth": 6, "score": chess.engine.PovScore(chess.engine.Mate(4), chess.WHITE), "pv": pv}
        result = poison.validate_capture_branch(board, "b3c4", "b5c4", info)
        self.assertTrue(result["verified"], result)
        self.assertEqual(result["minimum_payoff_moves"], 3)
        self.assertEqual(result["maximum_payoff_moves"], 4)
        self.assertEqual(result["proof_witness_uci"], [move.uci() for move in pv])
        self.assertTrue(result["deep_combination"])

    def test_engine_mate_claim_does_not_substitute_for_tactical_proof(self):
        board = legal_trap()
        board.push_uci("a2a3")
        board.push_uci("g8f6")
        after = board.copy(stack=True)
        after.push_uci("f3e5")
        after.push_uci("g4d1")
        fake = {"score": chess.engine.PovScore(chess.engine.Mate(2), chess.WHITE), "depth": 6,
                "pv": [chess.Move.from_uci("c4f7")]}
        result = poison.validate_capture_branch(board, "f3e5", "g4d1", fake)
        self.assertFalse(result["verified"], result)

    def test_wrong_pov_bounds_bad_pv_and_non_capture_are_rejected(self):
        board = legal_trap()
        valid = mate_info(board)
        variants = [dict(valid, depth=1), dict(valid, lowerbound=True), dict(valid, upperbound=True),
                    dict(valid, pv=[chess.Move.from_uci("a1a8")]),
                    dict(valid, score=chess.engine.PovScore(chess.engine.Mate(2), chess.BLACK)),
                    dict(valid, score=chess.engine.PovScore(chess.engine.Mate(-2), chess.WHITE))]
        for info in variants:
            self.assertFalse(poison.validate_capture_branch(board, "f3e5", "g4d1", info)["verified"])
        self.assertFalse(poison.validate_capture_branch(board, "f3e5", "g8f6", valid)["verified"])

    def test_proof_deadline_and_node_limit_fail_closed(self):
        board = legal_trap()
        for kwargs in ({"deadline_monotonic": time.monotonic() - 1}, {"max_nodes": 1}):
            result = poison.validate_capture_branch(board, "f3e5", "g4d1", mate_info(board), **kwargs)
            self.assertFalse(result["verified"])
            self.assertEqual(result["reason"], "proof_budget_exhausted")


if __name__ == "__main__":
    unittest.main(verbosity=2)
