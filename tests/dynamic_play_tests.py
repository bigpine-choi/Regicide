"""Deterministic policy/geometry checks; no native engine or user game runs."""

import copy
from pathlib import Path
import sys
import unittest
from unittest import mock

import chess
import chess.engine

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import dynamic_play as policy
import opening_variety as opening


def middlegame():
    board = chess.Board()
    for san in "e4 e5 Nf3 Nc6 Bb5 a6 Ba4 Nf6 O-O Be7 Re1 b5 Bb3 d6 c3 O-O".split():
        board.push_san(san)
    return board


def packet(board, uci, cp=20, depth=4):
    return {"depth": depth, "score": chess.engine.PovScore(chess.engine.Cp(cp), board.turn),
            "pv": [chess.Move.from_uci(move) for move in uci.split()]}


def proof(board, old, new, cp=20, loss=0, depth=4):
    baseline, candidate = packet(board, old, cp, depth), packet(board, new, cp - loss, depth)
    move = candidate["pv"][0]
    return baseline, policy.assess(board, baseline["pv"][0], baseline, move.uci(),
                                   {depth: candidate}, candidate, move)


class DynamicPlanTests(unittest.TestCase):
    def test_opening_order_pool_and_budget_are_exactly_preserved(self):
        board = chess.Board()
        original = opening.plan(board, 1000, "a" * 32)
        selected = policy.plan(board, 1000, "a" * 32, opening_plan=original)
        for key in ("candidate_order", "candidates", "extra_budget_ms", "proposal_limit", "early_exploration"):
            self.assertEqual(selected[key], original[key])
        self.assertEqual(len(selected["candidates"]), 20)
        self.assertEqual(selected["tolerance_cp"], 25)

    def test_middlegame_has_full_legal_pool_and_four_bounded_proposals(self):
        board = middlegame()
        chosen = policy.plan(board, 1000, "a" * 32)
        self.assertTrue(chosen["eligible"])
        self.assertEqual(set(chosen["candidates"]), {move.uci() for move in board.legal_moves})
        self.assertEqual(chosen["extra_budget_ms"], 500)
        self.assertEqual(chosen["tolerance_cp"], 15)
        proposals = policy.proposals(chosen, chess.Move.from_uci("d2d4"))
        self.assertEqual(len(proposals), 4)
        self.assertNotIn("d2d4", proposals)
        self.assertEqual(chosen, policy.plan(board, 1000, "a" * 32))
        self.assertNotEqual(chosen["candidate_order"], policy.plan(board, 1000, "b" * 32)["candidate_order"])

    def test_time_limits_and_phase_boundaries(self):
        for budget, ply, expected in ((100, 0, 0), (1000, 0, 1000), (5000, 0, 2000),
                                     (100, 16, 0), (500, 16, 250), (1000, 16, 500),
                                     (5000, 16, 750), (1000, 79, 500), (5000, 80, 0)):
            self.assertEqual(policy.extra_budget(budget, ply=ply), expected)
        self.assertEqual(policy.plan(middlegame(), 100, "a")["reason"], "short_budget")
        self.assertEqual(policy.plan(middlegame(), 1000, "a", forced=True)["reason"], "user_forced_game")

    def test_custom_roots_check_repetition_and_simplified_positions_are_excluded(self):
        board = middlegame()
        with mock.patch.object(board, "is_check", return_value=True):
            self.assertEqual(policy.plan(board, 1000, "a")["reason"], "in_check")
        with mock.patch.object(opening, "has_repetition", return_value=True):
            self.assertEqual(policy.plan(board, 1000, "a")["reason"], "repetition")
        with mock.patch.object(policy, "_phase", return_value=7):
            self.assertEqual(policy.plan(board, 1000, "a")["reason"], "simplified_position")
        with mock.patch.object(policy, "_nonpawns", return_value=4):
            self.assertEqual(policy.plan(board, 1000, "a")["reason"], "simplified_position")
        with mock.patch.object(board, "root", return_value=chess.Board("7k/8/8/8/8/8/8/K7 w - - 0 1")):
            self.assertEqual(policy.plan(board, 1000, "a")["reason"], "custom_root")

    def test_planning_preserves_full_history_and_memo_depends_on_state(self):
        board = middlegame()
        before = (board.fen(), list(board.move_stack))
        policy.plan(board, 1000, "a")
        self.assertEqual((board.fen(), list(board.move_stack)), before)
        key = policy.memo_key(board, 1000, "binary", "evaluation", seed="a")
        for alternative in (policy.memo_key(board, 5000, "binary", "evaluation", seed="a"),
                            policy.memo_key(board, 1000, "other", "evaluation", seed="a"),
                            policy.memo_key(board, 1000, "binary", "other", seed="a"),
                            policy.memo_key(board, 1000, "binary", "evaluation", seed="b"),
                            policy.memo_key(chess.Board(board.fen()), 1000, "binary", "evaluation", seed="a")):
            self.assertNotEqual(key, alternative)


class DynamicGeometryTests(unittest.TestCase):
    def test_quiet_central_break_opens_real_lines_retained_after_reply(self):
        board = middlegame()
        before = (board.fen(), list(board.move_stack))
        quiet = policy._style(board, ["h2h3", "c6a5"])
        central = policy._style(board, ["d2d4", "e5d4"])
        self.assertGreater(central["points"], quiet["points"])
        self.assertEqual(central["retained_open_lines"], 2)
        self.assertEqual(central["components"]["central_break"], 12)
        self.assertEqual(quiet["components"]["central_break"], 0)
        self.assertEqual((board.fen(), list(board.move_stack)), before)

    def test_one_queen_is_not_coordinated_pressure(self):
        alone = chess.Board("6k1/6pp/8/7Q/8/8/8/6K1 w - - 0 1")
        together = chess.Board("6k1/6pp/8/5N1Q/8/8/8/6K1 w - - 0 1")
        self.assertTrue(alone.is_valid() and together.is_valid())
        a, b = policy._features(alone, chess.WHITE), policy._features(together, chess.WHITE)
        self.assertEqual(a["king_attackers"], 1)
        self.assertEqual(a["coordinated_pressure"], 0)
        self.assertEqual(b["king_attackers"], 2)
        self.assertGreater(b["coordinated_pressure"], 0)

    def test_color_reflection_and_real_reply_requirement(self):
        board = middlegame()
        original = [chess.Move.from_uci(uci) for uci in ("d2d4", "e5d4")]
        mirrored = [chess.Move(chess.square_mirror(move.from_square), chess.square_mirror(move.to_square),
                               promotion=move.promotion) for move in original]
        self.assertEqual(policy._style(board, original)["components"],
                         policy._style(board.mirror(), mirrored)["components"])
        self.assertIsNone(policy._style(board, original[:1]))
        self.assertIsNone(policy._style(board, ["d2d4", "e5e4"]))

    def test_no_check_capture_or_material_loss_feature_exists(self):
        style = policy._style(middlegame(), ["d2d4", "e5d4"])
        self.assertEqual(set(style["components"]),
                         {"activity", "development", "rook_files", "king_pressure", "central_break"})
        self.assertEqual(style["units"], "style points; not centipawns")


class DynamicSelectionTests(unittest.TestCase):
    def setUp(self):
        self.board = middlegame()
        self.old = "h2h3 c6a5"
        self.new = "d2d4 e5d4"
        self.plan = policy.plan(self.board, 1000, "a")
        # Fixed probe availability makes selection tests independent of ordering.
        self.plan["candidate_order"] = ["d2d4", "h2h3"]

    def result(self, baseline, probes, seed="a"):
        return policy.receipt(self.board, self.plan, baseline["pv"][0], baseline, probes, seed)

    def test_style_preference_stays_within_quality_gate_and_retains_baseline(self):
        baseline, probe = proof(self.board, self.old, self.new, loss=5)
        self.assertTrue(probe["admitted"])
        results = [self.result(baseline, [probe], seed=str(seed)) for seed in range(128)]
        selected = [row["selected_uci"] for row in results]
        self.assertGreater(selected.count("d2d4"), selected.count("h2h3"))
        self.assertIn("h2h3", selected)
        for result in results:
            self.assertIn("h2h3", result["selection_pool"])
            self.assertEqual(result["selection_weights"], {"h2h3": 3, "d2d4": 15})
            self.assertLessEqual(result["loss_cp"], 15)

    def test_stronger_style_never_waives_centipawn_tolerance(self):
        for loss, expected in ((15, True), (16, False), (100, False)):
            baseline, probe = proof(self.board, self.old, self.new, loss=loss)
            self.assertEqual(probe["admitted"], expected)
            if not expected:
                self.assertEqual(self.result(baseline, [probe])["selection_pool"], ["h2h3"])
        opening_board = chess.Board()
        for loss, expected in ((25, True), (26, False)):
            _baseline, probe = proof(opening_board, "g1f3 d7d5", "e2e4 e7e5", loss=loss)
            self.assertEqual(probe["admitted"], expected)

    def test_lower_style_is_excluded_even_when_equal_engine_score(self):
        baseline, probe = proof(self.board, self.new, self.old)
        result = self.result(baseline, [probe])
        self.assertEqual(result["selected_uci"], "d2d4")
        self.assertEqual(result["style_exclusions"]["h2h3"], "less_dynamic_after_reply")

    def test_mate_bound_wrong_pov_illegal_and_partial_scores_fail_closed(self):
        baseline = packet(self.board, self.old)
        move = chess.Move.from_uci("d2d4")
        for info in ({"depth": 4}, packet(self.board, "d2d4 e5e4"),
                     {**packet(self.board, self.new), "lowerbound": True},
                     {**packet(self.board, self.new), "score": chess.engine.PovScore(chess.engine.Cp(20), not self.board.turn)},
                     {**packet(self.board, self.new), "score": chess.engine.PovScore(chess.engine.Mate(3), self.board.turn)}):
            probe = policy.assess(self.board, baseline["pv"][0], baseline, "d2d4", {4: info}, info, move)
            self.assertFalse(probe["admitted"])
            self.assertEqual(self.result(baseline, [probe])["selected_uci"], "h2h3")

    def test_same_depth_and_deeper_warning_and_conflicts(self):
        baseline = packet(self.board, self.old)
        move = chess.Move.from_uci("d2d4")
        shallow = packet(self.board, self.new, 20, 3)
        probe = policy.assess(self.board, baseline["pv"][0], baseline, "d2d4", {3: shallow}, shallow, move)
        self.assertEqual(probe["reason"], "no_common_depth")
        same = packet(self.board, self.new, 20, 4)
        deeper = packet(self.board, self.new, -20, 5)
        probe = policy.assess(self.board, baseline["pv"][0], baseline, "d2d4", {4: same, 5: deeper}, deeper, move)
        self.assertEqual(probe["reason"], "deeper_candidate_warning")
        contradictory = packet(self.board, self.new, 36, 4)
        probe = policy.assess(self.board, baseline["pv"][0], baseline, "d2d4", {4: contradictory}, contradictory, move)
        self.assertEqual(probe["reason"], "candidate_score_conflict")

    def test_any_candidate_failure_returns_baseline_and_skips_style_choice(self):
        baseline, good = proof(self.board, self.old, self.new)
        bad = copy.deepcopy(good)
        bad.update(admitted=False, reason="candidate_failed", error="TimeoutError")
        self.assertEqual(self.result(baseline, [good, bad])["selected_uci"], "h2h3")

    def test_conversion_mate_terminal_and_short_pv_guard(self):
        baseline = packet(self.board, self.old, 300)
        self.assertEqual(policy.baseline_guard(self.board, baseline["pv"][0], baseline), "conversion_priority")
        self.assertEqual(self.result(baseline, [])["reason"], "conversion_priority")
        one = packet(self.board, "h2h3")
        self.assertEqual(policy.baseline_guard(self.board, one["pv"][0], one), "insufficient_reply_evidence")
        mate = {**baseline, "score": chess.engine.PovScore(chess.engine.Mate(2), self.board.turn)}
        self.assertEqual(policy.baseline_guard(self.board, baseline["pv"][0], mate), "baseline_unreliable")
        draw = middlegame()
        packet_draw = packet(draw, "b1a3 c6b8 a3b1 b8c6 b1a3 c6b8 a3b1 b8c6", 0)
        self.assertEqual(policy.baseline_guard(draw, packet_draw["pv"][0], packet_draw), "forcing_line_preserved")

    def test_opening_receipt_is_reference_evidence_not_the_final_choice(self):
        board = chess.Board()
        planned = policy.plan(board, 1000, "a")
        planned["candidate_order"] = ["e2e4", "g1f3"]
        baseline, probe = proof(board, "g1f3 d7d5", "e2e4 e7e5")
        result = policy.receipt(board, planned, baseline["pv"][0], baseline, [probe], "a")
        self.assertEqual(result["policy"], policy.POLICY)
        self.assertEqual(result["opening_receipt"]["policy"], opening.POLICY)
        self.assertEqual(result["tolerance_cp"], 25)
        self.assertEqual(result["selected_uci"], result["opening_receipt"]["selected_uci"])
        self.assertEqual(result["selection_rule"], "opening_variety_preserved")

    def test_opening_selection_matches_v3_at_both_colors_and_phase_edges(self):
        full = middlegame()
        for ply in (0, 1, 8, 15):
            board = chess.Board()
            for move in full.move_stack[:ply]:
                board.push(move)
            legal = sorted(board.legal_moves, key=lambda move: move.uci())[:8]
            old = opening.plan(board, 1000, "a" * 32)
            old.update(eligible=True, reason="compare", candidates=[move.uci() for move in legal],
                       candidate_order=[move.uci() for move in legal], candidate_uci=legal[0].uci())
            planned = policy.plan(board, 1000, "a" * 32, opening_plan=old)
            def line(move):
                after = board.copy(stack=True)
                after.push(move)
                reply = sorted(after.legal_moves, key=lambda item: item.uci())[0]
                return packet(board, move.uci() + " " + reply.uci())
            baseline = line(legal[0])
            probes = []
            for uci in opening.proposals(old, legal[0]):
                move = chess.Move.from_uci(uci)
                candidate = line(move)
                a = opening.assess(board, legal[0], baseline, uci, {4: candidate}, candidate, move)
                b = policy.assess(board, legal[0], baseline, uci, {4: candidate}, candidate, move)
                self.assertEqual({key: b[key] for key in a}, a)
                probes.append(b)
            for seed in range(128):
                seed = f"{seed:032x}"
                expected = opening.receipt(board, old, legal[0], baseline, probes, seed)
                actual = policy.receipt(board, planned, legal[0], baseline, probes, seed)
                for key in ("selected_uci", "selection_pool", "selection_weights", "reason", "loss_cp",
                            "compared_depth", "candidate_compared", "proposals", "extra_budget_ms"):
                    self.assertEqual(actual[key], expected[key], (ply, seed, key))
                self.assertEqual(actual["style_evidence"], {})

    def test_opening_keeps_a_valid_fianchetto_despite_lower_two_ply_style(self):
        board = chess.Board()
        board.push_uci("e2e4")
        baseline, probe = proof(board, "b8c6 d2d4", "g7g6 d2d4")
        self.assertLess(policy._style(board, ["g7g6", "d2d4"])["points"],
                        policy._style(board, ["b8c6", "d2d4"])["points"])
        old = opening.plan(board, 1000, "a" * 32)
        old.update(candidate_order=["g7g6", "b8c6"], candidates=["g7g6", "b8c6"], candidate_uci="g7g6")
        planned = policy.plan(board, 1000, "a" * 32, opening_plan=old)
        selected = []
        for seed in range(64):
            expected = opening.receipt(board, old, baseline["pv"][0], baseline, [probe], str(seed))
            actual = policy.receipt(board, planned, baseline["pv"][0], baseline, [probe], str(seed))
            self.assertEqual(actual["selection_weights"], expected["selection_weights"])
            self.assertEqual(actual["selection_pool"], ["b8c6", "g7g6"])
            self.assertEqual(actual["selected_uci"], expected["selected_uci"])
            selected.append(actual["selected_uci"])
        self.assertIn("g7g6", selected)
        self.assertIn("b8c6", selected)

    def test_opening_probe_guard_preserves_v3_instead_of_middlegame_style_gates(self):
        board = chess.Board()
        one = packet(board, "g1f3", 300)
        self.assertIsNone(policy.baseline_guard(board, one["pv"][0], one))
        self.assertEqual(policy.score_line(board, one), opening.score_line(board, one))
        bound = {**one, "lowerbound": True}
        self.assertEqual(policy.baseline_guard(board, one["pv"][0], bound), "baseline_unreliable")

    def test_headers_require_current_policy_and_valid_seed(self):
        value = {"enabled": True, "policy": policy.POLICY, "seed": "a" * 32}
        self.assertEqual(policy.pgn_headers(value), {"DynamicPlayPolicy": policy.POLICY, "DynamicPlaySeed": "a" * 32})
        for bad in ({**value, "enabled": False}, {**value, "seed": "user text"}, {**value, "policy": "unknown"}, None):
            self.assertEqual(policy.pgn_headers(bad), {})


if __name__ == "__main__":
    unittest.main()
