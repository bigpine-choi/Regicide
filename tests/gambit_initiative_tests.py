"""Verified gambit context and bounded reply-supported native preference."""
import copy
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

import chess
import chess.engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import boss_openings
import gambit_initiative as policy

SEED = "a" * 32
DANISH = "e2e4 e7e5 d2d4 e5d4 c2c3 d4c3 b1c3 b8c6".split()


def board_after(moves=DANISH):
    board = chess.Board()
    for uci in moves:
        board.push_uci(uci)
    return board


def signature(line_id="danish", version="regicide-boss-play-v5"):
    line = next(row for row in boss_openings.repertoire() if row["line_id"] == line_id)
    before = board_after(line["moves"][:-1])
    return {"policy": version, "ply": len(line["moves"]), "line_id": line_id,
            "line_complete": True, "history_uci": line["moves"][:-1],
            "before_fen": before.fen(en_passant="fen"), "seed": SEED,
            "candidate_uci": line["moves"][-1], "selected_uci": line["moves"][-1]}


def preparation(line_id="danish", ply=1, version="regicide-boss-play-v5"):
    line = next(row for row in boss_openings.repertoire() if row["line_id"] == line_id)
    before = board_after(line["moves"][:ply - 1])
    return {"policy": version, "ply": ply, "line_id": line_id,
            "line_complete": False, "history_uci": line["moves"][:ply - 1],
            "before_fen": before.fen(en_passant="fen"), "seed": SEED,
            "candidate_uci": line["moves"][ply - 1], "selected_uci": line["moves"][ply - 1]}


def packet(board, first, reply="g8f6", cp=20, depth=4, **extra):
    return {"depth": depth, "score": chess.engine.PovScore(chess.engine.Cp(cp), board.turn),
            "pv": [chess.Move.from_uci(first), chess.Move.from_uci(reply)], **extra}


class ContextTests(unittest.TestCase):
    def test_actual_signature_acceptance_and_net_debt_are_separate(self):
        board = board_after()
        ctx = policy.context(board, [signature()])
        self.assertTrue(ctx["active"])
        self.assertEqual((ctx["line_id"], ctx["signature_ply"]), ("danish", 5))
        self.assertEqual((ctx["material_offered_cp"], ctx["material_accepted_cp"], ctx["material_debt_cp"]),
                         (100, 100, 100))
        self.assertEqual(ctx["signature_capture"]["uci"], "d4c3")
        self.assertEqual(board.move_stack, board_after().move_stack)

    def test_forged_stale_legacy_and_wrong_side_receipts_do_not_activate(self):
        board = board_after()
        for change in ({"policy": "regicide-boss-play-v3"}, {"policy": "regicide-boss-play-v4"},
                       {"policy": "foreign"},
                       {"line_complete": False}, {"selected_uci": "g1f3"},
                       {"candidate_uci": "g1f3"}, {"history_uci": []}, {"ply": 7},
                       {"before_fen": chess.STARTING_FEN}, {"line_id": "not-a-gambit"},
                       {"line_id": ["danish"]}, {"line_id": {"id": "danish"}}, {"seed": None}):
            with self.subTest(change=change):
                self.assertFalse(policy.context(board, [{**signature(), **change}])["active"])
        self.assertEqual(policy.context(board, [signature(version="regicide-boss-play-v3")])["reason"],
                         "legacy_policy")
        undone = board.copy(stack=True)
        for _ in range(4):
            undone.pop()
        self.assertFalse(policy.context(undone, [signature()])["active"])
        self.assertEqual(policy.context(board_after(DANISH[:-1]), [signature()])["reason"], "opponent_to_move")
        self.assertFalse(policy.context(chess.Board(board.fen()), [signature()])["active"])

    def test_black_signature_uses_black_perspective(self):
        moves = "d2d4 e7e5 d4e5".split()
        ctx = policy.context(board_after(moves), [signature("englund")])
        self.assertTrue(ctx["active"])
        self.assertEqual((ctx["turn"], ctx["material_debt_cp"], ctx["material_accepted_cp"]),
                         ("black", 100, 100))

    def test_check_repetition_and_window_end_disable_optional_style(self):
        repeated = board_after(DANISH + "g1f3 g8f6 f3g1 f6g8".split())
        self.assertEqual(policy.context(repeated, [signature()])["reason"], "repetition")
        window = board_after()
        for _ in range(18):
            move = next(m for m in window.legal_moves if not window.gives_check(m))
            window.push(move)
        ctx = policy.context(window, [signature()])
        self.assertEqual(ctx["reason"], "window_finished")

    def test_preparation_survives_offbook_without_claiming_a_gambit_offer(self):
        board = board_after("e2e4 c7c6".split())
        ctx = policy.context(board, [preparation()])
        self.assertTrue(ctx["active"])
        self.assertEqual((ctx["stage"], ctx["entry_ply"], ctx["source_ply"]), ("preparation", 1, 1))
        self.assertEqual(ctx["source_uci"], "e2e4")
        self.assertEqual(ctx["source_fen"], board_after(["e2e4"]).fen(en_passant="fen"))
        self.assertEqual(ctx["source_before_fen"], chess.STARTING_FEN)
        self.assertFalse(ctx["signature_complete"])
        for key in ("signature_ply", "signature_uci", "signature_fen"):
            self.assertIsNone(ctx[key])
        self.assertEqual((ctx["material_offered_cp"], ctx["material_accepted_cp"], ctx["accepted"]), (0, 0, False))
        self.assertEqual(ctx["reason"], "authored_preparation_offbook")
        debt = policy.context(board_after("e2e4 c7c6 d2d4 d7d5 g1f3 d5e4".split()), [preparation()])
        self.assertEqual((debt["material_debt_cp"], debt["material_offered_cp"], debt["material_accepted_cp"]), (100, 0, 0))

    def test_preparation_does_not_replace_a_matching_book_or_valid_signature(self):
        ctx = policy.context(board_after("e2e4 e7e5".split()), [preparation()])
        self.assertFalse(ctx["active"])
        self.assertEqual(ctx["reason"], "preparation_in_repertoire")
        offered = policy.context(board_after(), [preparation(), signature()])
        self.assertEqual((offered["stage"], offered["signature_ply"], offered["entry_ply"]), ("offered", 5, 5))
        self.assertTrue(offered["signature_complete"])

    def test_preparation_receipts_require_actual_own_selection_and_surviving_prefix(self):
        board = board_after("e2e4 c7c6".split())
        for change in ({"selected_uci": "d2d4"}, {"candidate_uci": "d2d4"},
                       {"history_uci": ["a2a3"]}, {"before_fen": "wrong"}, {"line_complete": True},
                       {"seed": None}, {"policy": "regicide-boss-play-v3"},
                       {"policy": "regicide-boss-play-v4"}, {"ply": 0}, {"ply": True}):
            with self.subTest(change=change):
                self.assertFalse(policy.context(board, [{**preparation(), **change}])["active"])
        self.assertFalse(policy.context(board_after("e2e4 e7e5".split()), [preparation(ply=3)])["active"])
        self.assertFalse(policy.context(board, [preparation("evans", ply=5)])["active"])
        black = policy.context(board_after("e2e4 e7e5 b1c3".split()), [preparation("stafford", ply=2)])
        self.assertTrue(black["active"])
        self.assertEqual((black["owner"], black["stage"]), ("black", "preparation"))


class ChoiceTests(unittest.TestCase):
    def setUp(self):
        self.board = board_after()
        self.ctx = policy.context(self.board, [signature()])
        self.base_move = chess.Move.from_uci("a2a3")
        self.base = packet(self.board, "a2a3")
        self.calls = []
        self.closed = []

    def run_choice(self, *, final=None, common=None, context=None, seed=SEED,
                   proposals=("f1c4",), failure=None, budget=500, delay=0):
        if final is None:
            final = packet(self.board, "f1c4", cp=15)
        if common is None:
            common = final
        def search(amount, **options):
            self.calls.append((amount, options))
            if delay:
                time.sleep(delay)
            if failure:
                raise failure
            return chess.engine.BestMove(options["root_move"], None), final, {4: common}
        with patch.object(policy, "_proposals", return_value=[chess.Move.from_uci(u) for u in proposals]):
            return policy.choose(self.board, self.base_move, self.base, {4: self.base}, search,
                                 lambda: self.closed.append(True), seed=seed, budget_ms=budget,
                                 context=self.ctx if context is None else context)

    def test_native_same_depth_preference_retains_reply_supported_development(self):
        result, discard = self.run_choice()
        self.assertTrue(result["applied"])
        self.assertFalse(discard)
        self.assertEqual((result["selected_uci"], result["loss_cp"]), ("f1c4", 5))
        self.assertEqual(result["evidence"]["candidate_compared"]["depth"], 4)
        self.assertEqual(result["evidence"]["style"]["pv_prefix_uci"], ["f1c4", "g8f6"])
        self.assertGreater(result["evidence"]["style"]["points"], result["baseline_evidence"]["points"])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][1], {"root_move": chess.Move.from_uci("f1c4"),
                                          "publish": False, "fresh": True, "depth": 4})
        self.assertLessEqual(self.calls[0][0], 500)
        self.assertEqual(self.board.fen(), board_after().fen())

    def test_bounds_mates_wrong_pov_depth_and_both_quality_floors_are_rejected(self):
        good = packet(self.board, "f1c4", cp=15)
        variants = [dict(good, lowerbound=True), dict(good, upperbound=True),
                    dict(good, depth=3), dict(good, score=chess.engine.PovScore(chess.engine.Cp(15), chess.BLACK)),
                    dict(good, score=chess.engine.PovScore(chess.engine.Mate(2), chess.WHITE)),
                    packet(self.board, "f1c4", cp=-16),
                    packet(self.board, "f1c4", cp=-16, depth=5),
                    {**good, "pv": [chess.Move.from_uci("f1c4")]},
                    {**good, "pv": [chess.Move.from_uci("f1c5"), chess.Move.from_uci("g8f6")]}]
        for candidate in variants:
            with self.subTest(candidate=candidate):
                result, _ = self.run_choice(final=candidate, common=good)
                self.assertFalse(result["applied"])
                self.assertEqual(result["selected_uci"], "a2a3")
        result, _ = self.run_choice(final=good, common=packet(self.board, "f1c4", cp=-16))
        self.assertFalse(result["applied"])

    def test_fresh_piece_offer_is_rejected_even_if_fake_score_claims_quality(self):
        result, discard = self.run_choice(proposals=("f1a6",))
        self.assertFalse(result["applied"])
        self.assertFalse(discard)
        self.assertEqual(result["attempts"][0]["reason"], "new_piece_donation")
        self.assertEqual(self.calls, [])
        self.assertLess(result["attempts"][0]["material_screen"]["captures"][0]["net_material_cp"], -100)

    def test_black_native_scores_and_development_use_the_current_side(self):
        self.board = board_after("d2d4 e7e5 d4e5".split())
        self.ctx = policy.context(self.board, [signature("englund")])
        self.base_move = chess.Move.from_uci("a7a6")
        self.base = packet(self.board, "a7a6", reply="g1f3")
        candidate = packet(self.board, "b8c6", reply="g1f3", cp=15)
        result, discard = self.run_choice(final=candidate, common=candidate, proposals=("b8c6",))
        self.assertFalse(discard)
        self.assertTrue(result["applied"])
        self.assertEqual(result["selected_uci"], "b8c6")
        self.assertEqual(result["evidence"]["candidate_final"]["cp_white"], -15)

    def test_a_complete_equal_exchange_is_not_mislabeled_a_donation(self):
        board = chess.Board("7k/8/8/8/8/1r6/B7/1R5K w - - 0 1")
        move = chess.Move.from_uci("h1g1")
        self.assertIn(move, board.legal_moves)
        result = policy._material_screen(board, move, time.monotonic() + 1)
        self.assertTrue(result["admitted"])
        # Black may capture the rook, then the bishop recaptures the rook.
        capture = next(r for r in result["captures"] if r["reply_uci"] == "b3b1")
        self.assertEqual(capture["net_material_cp"], 0)
        self.assertTrue(capture["recapture_exchange"]["complete"])

    def test_stale_context_seed_and_conversion_keep_baseline_without_search(self):
        for ctx in ({**self.ctx, "fen": chess.STARTING_FEN}, {**self.ctx, "history_uci": []},
                    {**self.ctx, "turn": "black"}, {**self.ctx, "active": False},
                    {**self.ctx, "line_id": ["danish"]}, {**self.ctx, "line_id": {"id": "danish"}}):
            result, _ = self.run_choice(context=ctx)
            self.assertFalse(result["applied"])
        result, _ = self.run_choice(seed="different")
        self.assertFalse(result["applied"])
        self.assertEqual(self.calls, [])
        self.base = packet(self.board, "a2a3", cp=300)
        result, _ = self.run_choice()
        self.assertEqual(result["reason"], "forcing_or_conversion_baseline")
        self.assertEqual(self.calls, [])

    def test_timeout_and_callback_failure_abandon_any_alternative(self):
        result, discard = self.run_choice(failure=RuntimeError("cancelled"))
        self.assertEqual((result["selected_uci"], discard), ("a2a3", True))
        self.assertEqual(result["reason"], "initiative_probe_failed")
        result, discard = self.run_choice(delay=.25, budget=100)
        self.assertEqual((result["selected_uci"], discard), ("a2a3", True))
        self.assertEqual(result["reason"], "initiative_probe_timeout")
        self.assertTrue(self.closed)

    def test_short_budget_and_maximum_probe_count(self):
        result, discard = self.run_choice(budget=49)
        self.assertEqual((result["reason"], discard), ("short_budget", False))
        self.assertEqual(self.calls, [])
        generated = policy._proposals(self.board, self.base_move, SEED)
        self.assertLessEqual(len(generated), 2)
        self.assertEqual(generated, policy._proposals(self.board, self.base_move, SEED))
        self.assertTrue(all(move in self.board.legal_moves and move != self.base_move for move in generated))

    def test_preparation_uses_the_same_quality_screen_and_does_not_inflate_signature(self):
        self.board = board_after("e2e4 c7c6".split())
        self.ctx = policy.context(self.board, [preparation()])
        self.base_move = chess.Move.from_uci("a2a3")
        self.base = packet(self.board, "a2a3")
        result, discard = self.run_choice()
        self.assertTrue(result["applied"])
        self.assertFalse(discard)
        self.assertEqual(result["context"]["stage"], "preparation")
        self.assertIsNone(result["context"]["signature_ply"])
        self.assertEqual(result["context"]["material_accepted_cp"], 0)
        for change in ({"signature_ply": 1}, {"signature_complete": True}, {"stage": "offered"},
                       {"source_fen": chess.STARTING_FEN}, {"source_before_fen": "wrong"},
                       {"entry_ply": True}, {"material_accepted_cp": 100}, {"material_offered_cp": 100}):
            invalid, _ = self.run_choice(context={**self.ctx, **change})
            self.assertFalse(invalid["applied"], change)
        rejected, _ = self.run_choice(final=packet(self.board, "f1c4", cp=-16))
        self.assertFalse(rejected["applied"])


if __name__ == "__main__":
    unittest.main()
