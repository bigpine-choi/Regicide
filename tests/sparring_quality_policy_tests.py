"""Safety-floor and evidence regressions for the final sparring quality gate."""
import copy
import json
from pathlib import Path
import sys
import unittest

import chess
import chess.engine

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import sparring_quality as quality


def position(ply=0):
    if not ply:
        return chess.Board()
    saved = json.loads((ROOT / "docs/matches/2026-11-03-sparring-validation/1700-white/game.json").read_text(encoding="utf-8"))
    board = chess.Board(saved["initial_fen"])
    for move in saved["uci_moves"][:ply]:
        board.push_uci(move)
    assert len(board.move_stack) == ply and not board.is_game_over(claim_draw=True)
    return board


def packet(board, own, cp=None, mate=None):
    sign = 1 if own else -1
    return {"fen": board.fen(en_passant="fen"), "depth": 12, "bound": "exact",
            "cp": cp * sign if cp is not None else None, "cp_own": cp,
            "mate": mate * sign if mate is not None else None, "mate_own": mate,
            "terminal": False, "winner": ("white" if mate * sign > 0 else "black") if mate else None}


def assessment(board, move, root=0, child=0, *, root_mate=None, child_mate=None):
    after = board.copy(stack=True)
    after.push(move)
    return {"status": "ready", "fen": board.fen(en_passant="fen"),
            "history_uci": [m.uci() for m in board.move_stack], "selected_uci": move.uci(),
            "root": packet(board, board.turn, None if root_mate else root, root_mate),
            "child": packet(after, board.turn, None if child_mate else child, child_mate)}


def evaluate(board, root, child):
    # This helper isolates score-floor behavior; donation rejection has its own
    # actual-position regressions instead of an arbitrary first legal move.
    move = next(move for move in board.legal_moves if quality.pending_exchange.naturalness(board, move)["admitted"])
    return quality.verdict(board, move, assessment(board, move, root, child))


def native_reply(board, move, *, depth=5):
    info = {"depth": depth, "score": chess.engine.PovScore(chess.engine.Cp(30), board.turn), "pv": [move]}
    return chess.engine.PlayResult(move, None), info, {depth: info}


class QualityPolicyTests(unittest.TestCase):
    def test_close_phase_scores_use_original_side_for_both_colors(self):
        for ply in (28, 29):
            board = position(ply)
            with self.subTest(ply=ply):
                self.assertTrue(evaluate(board, 0, -100)["acceptable"])
                self.assertFalse(evaluate(board, 0, -101)["acceptable"])
                move = next(iter(board.legal_moves))
                evidence = assessment(board, move, 0, -100)
                evidence["child"]["cp_own"] = 100
                self.assertFalse(quality.verdict(board, move, evidence)["verified"])

    def test_early_winning_advantage_can_return_to_close_band(self):
        result = evaluate(position(28), 500, 20)
        self.assertTrue(result["acceptable"])
        self.assertTrue(result["inside_close_band"])
        self.assertEqual(result["loss_cp"], 480)

    def test_early_losing_position_preserves_resistance(self):
        board = position(28)
        self.assertTrue(evaluate(board, -300, -360)["acceptable"])
        result = evaluate(board, -300, -361)
        self.assertFalse(result["acceptable"])
        self.assertEqual(result["floor_cp"], -340)

    def test_move16_starts_anti_reversal_guard_for_both_colors(self):
        for before, after in ((28, 30), (29, 31)):
            self.assertTrue(evaluate(position(before), 50, -50)["acceptable"])
            self.assertFalse(evaluate(position(after), 50, -50)["acceptable"])

    def test_ramp_allowance_shrinks_at_later_moves(self):
        self.assertTrue(evaluate(position(30), 300, 260)["acceptable"])
        self.assertFalse(evaluate(position(84), 300, 260)["acceptable"])
        self.assertGreater(evaluate(position(84), 300, 260)["floor_cp"],
                           evaluate(position(30), 300, 260)["floor_cp"])

    def test_ramp_noise_margin_cannot_cross_from_holdable_to_negative_for_either_color(self):
        for ply in (30, 31, 96, 97):
            for root in (0, 5, 20):
                with self.subTest(ply=ply, root=root):
                    value = evaluate(position(ply), root, -1)
                    self.assertTrue(value["verified"])
                    self.assertFalse(value["acceptable"])
                    self.assertGreaterEqual(value["effective_floor_cp"], 0)
                    self.assertTrue(evaluate(position(ply), root, root)["acceptable"])

    def test_fifteenth_move_can_balance_but_sixteenth_cannot_give_away_advantage_or_mate(self):
        for early, later in ((28, 30), (29, 31)):
            self.assertTrue(evaluate(position(early), 500, 30)["acceptable"])
            self.assertFalse(evaluate(position(later), 500, 30)["acceptable"])
            board = position(later)
            move = next(iter(board.legal_moves))
            value = quality.verdict(board, move, assessment(board, move, root_mate=4, child=500))
            self.assertEqual(value["reason"], "forced_win_lost")
            self.assertFalse(value["acceptable"])

    def test_repair_cannot_use_root_drift_to_undo_ramp_nonnegative_floor(self):
        for ply in (30, 31):
            board = position(ply)
            incumbent = next(iter(board.legal_moves))
            def search(amount, *, root_moves, **kwargs):
                return native_reply(board, root_moves[0])
            def assess(move, amount, cancelled):
                return (assessment(board, move, 5, -10) if move == incumbent
                        else assessment(board, move, -5, -1))
            replacement, receipt, discarded = quality.verify(
                board, incumbent, search, lambda: None, assess)
            self.assertFalse(discarded)
            self.assertIsNone(replacement)
            self.assertFalse(receipt["final_verdict"]["acceptable"])
            checked = [row["verdict"] for row in receipt["attempts"] if row.get("verdict")]
            self.assertTrue(checked)
            self.assertTrue(all(not row["acceptable"] and row["effective_floor_cp"] == 0
                                for row in checked))

    def test_move50_keeps_fixed_loss_cap_for_both_colors(self):
        for ply in (98, 99):
            with self.subTest(ply=ply):
                self.assertTrue(evaluate(position(ply), 200, 140)["acceptable"])
                value = evaluate(position(ply), 200, 139)
                self.assertFalse(value["acceptable"])
                self.assertEqual(value["floor_cp"], 160)
                self.assertEqual(value["phase"], "finish")

    def test_wrong_position_history_selection_and_status_are_unverified(self):
        board = position(40)
        move = next(iter(board.legal_moves))
        correct = assessment(board, move)
        for key, wrong in (("fen", chess.STARTING_FEN), ("history_uci", []),
                           ("selected_uci", "a1a1"), ("status", "pending")):
            value = copy.deepcopy(correct)
            value[key] = wrong
            with self.subTest(key=key):
                self.assertFalse(quality.verdict(board, move, value)["verified"])

    def test_bounds_shallow_depth_and_malformed_scores_are_unverified(self):
        board = position(40)
        move = next(iter(board.legal_moves))
        correct = assessment(board, move)
        for side in ("root", "child"):
            for key, wrong in (("bound", "lower"), ("depth", 9), ("depth", True),
                               ("cp", float("nan")), ("cp", True), ("cp", 100001),
                               ("mate", 2), ("winner", "white"), ("fen", "stale")):
                value = copy.deepcopy(correct)
                value[side][key] = wrong
                with self.subTest(side=side, key=key, wrong=wrong):
                    self.assertFalse(quality.verdict(board, move, value)["verified"])

    def test_mate_perspective_and_winner_must_agree_for_both_colors(self):
        for ply in (28, 29):
            board = position(ply)
            move = next(iter(board.legal_moves))
            value = assessment(board, move, child_mate=3)
            result = quality.verdict(board, move, value)
            self.assertTrue(result["acceptable"])
            self.assertEqual(result["reason"], "winning_mate_verified")
            value["child"]["winner"] = "black" if board.turn else "white"
            self.assertFalse(quality.verdict(board, move, value)["verified"])

    def test_losing_mate_and_lost_forced_win_are_rejected(self):
        board = position(40)
        move = next(iter(board.legal_moves))
        lost = quality.verdict(board, move, assessment(board, move, child_mate=-4))
        self.assertFalse(lost["acceptable"])
        self.assertEqual(lost["reason"], "losing_mate_detected")
        missed = quality.verdict(board, move, assessment(board, move, root_mate=5, child=300))
        self.assertFalse(missed["acceptable"])
        self.assertEqual(missed["reason"], "forced_win_lost")
        escaped = quality.verdict(board, move, assessment(board, move, root_mate=-5, child=-300))
        self.assertTrue(escaped["acceptable"])

    def test_terminal_mate_requires_real_rule_outcome_for_both_colors(self):
        initial = chess.Board("7k/8/5KQ1/8/8/8/8/8 w - - 0 1")
        for black in (False, True):
            board = initial.mirror() if black else initial.copy(stack=True)
            move = chess.Move.from_uci("g3g2" if black else "g6g7")
            child = board.copy(stack=True)
            child.push(move)
            self.assertTrue(child.is_checkmate())
            evidence = assessment(board, move)
            evidence["child"] = {"fen": child.fen(en_passant="fen"), "terminal": True,
                                 "winner": "black" if black else "white", "outcome": child.result()}
            self.assertTrue(quality.verdict(board, move, evidence)["acceptable"])
            evidence["child"]["outcome"] = "1/2-1/2"
            self.assertFalse(quality.verdict(board, move, evidence)["verified"])

    def test_terminal_draw_requires_real_outcome_and_zero_own_score(self):
        board = chess.Board("7k/5K2/8/6Q1/8/8/8/8 w - - 0 1")
        move = chess.Move.from_uci("g5g6")
        child = board.copy(stack=True)
        child.push(move)
        self.assertTrue(child.is_stalemate())
        evidence = assessment(board, move)
        evidence["child"] = {"fen": child.fen(en_passant="fen"), "terminal": True,
                             "winner": None, "outcome": "1/2-1/2", "cp_own": 0}
        self.assertTrue(quality.verdict(board, move, evidence)["acceptable"])
        evidence["child"]["cp_own"] = 100
        self.assertFalse(quality.verdict(board, move, evidence)["verified"])

    def test_repeated_root_drift_cannot_relax_initial_floor(self):
        board = position(98)
        incumbent = next(iter(board.legal_moves))
        def search(amount, *, root_moves, **kwargs):
            return native_reply(board, root_moves[0])
        def assess(move, amount, cancelled):
            return assessment(board, move, 300, -200) if move == incumbent else assessment(board, move, 230, 200)
        replacement, receipt, discarded = quality.verify(board, incumbent, search, lambda: None, assess)
        self.assertIsNotNone(replacement)
        self.assertEqual(receipt["status"], "mitigated")
        self.assertFalse(receipt["final_verdict"]["acceptable"])
        self.assertFalse(discarded)
        self.assertEqual(sum(attempt["kind"] == "global" for attempt in receipt["attempts"]), 4)
        self.assertLessEqual(sum(attempt["kind"] == "minor_origin" for attempt in receipt["attempts"]), 2)
        for attempt in receipt["attempts"]:
            self.assertEqual(attempt["verdict"]["floor_cp"], 260)
            self.assertFalse(attempt["verdict"]["acceptable"])

    def test_large_reference_root_disagreement_blocks_native_replacement(self):
        board = position()
        incumbent = next(iter(board.legal_moves))
        def search(amount, *, root_moves, **kwargs):
            return native_reply(board, root_moves[0])
        def assess(move, amount, cancelled):
            return assessment(board, move, 0, -300) if move == incumbent else assessment(board, move, 150, 140)
        replacement, receipt, _ = quality.verify(board, incumbent, search, lambda: None, assess)
        self.assertIsNone(replacement)
        self.assertTrue(receipt["attempts"])
        self.assertTrue(all(attempt["reason"] == "reference_root_disagreement" for attempt in receipt["attempts"]))

    def test_later_shallow_candidates_need_completed_promotion_before_acceptance(self):
        board = position(40)
        incumbent = next(iter(board.legal_moves))
        calls = []
        def search(amount, *, root_moves, **kwargs):
            return native_reply(board, root_moves[0], depth=3)
        def assess(move, amount, cancelled):
            calls.append(move)
            return assessment(board, move, 0, -300 if move == incumbent else 0)
        from unittest import mock
        with mock.patch.object(quality, "MAX_MINOR_PROBES", 0):
            replacement, receipt, _ = quality.verify(board, incumbent, search, lambda: None, assess)
        self.assertIsNone(replacement)
        self.assertEqual(len(calls), 5)
        self.assertEqual(calls[0], incumbent)
        self.assertEqual(len(receipt["attempts"]), 4)
        self.assertTrue(all(attempt["reason"] == "incomplete_native_promotion" for attempt in receipt["attempts"]))


if __name__ == "__main__":
    unittest.main()
