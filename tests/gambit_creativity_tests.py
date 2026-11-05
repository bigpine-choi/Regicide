"""Controlled native packets test creativity contracts, not playing strength."""
import copy
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

import chess
import chess.engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import boss_play
import gambit_creativity as policy


def replay(history):
    board = chess.Board()
    for uci in history.split():
        board.push_uci(uci)
    return board


DANISH = "e2e4 e7e5 d2d4 e5d4 c2c3 d4c3 b1c3 b8c6"


def packet(board, first, *, reply="g8f6", cp=20, depth=4, **changes):
    return {"depth": depth, "score": chess.engine.PovScore(chess.engine.Cp(cp), board.turn),
            "pv": [chess.Move.from_uci(first), chess.Move.from_uci(reply)], **changes}


class CreativityTests(unittest.TestCase):
    def setUp(self):
        enabled = patch.object(policy, "ENABLED", True)
        enabled.start()
        self.addCleanup(enabled.stop)
        self.board = replay(DANISH)
        self.seed = "creative-unit"
        self.plan = boss_play.plan(self.board, 1000, self.seed)
        self.move = chess.Move.from_uci("a2a3")
        self.base = packet(self.board, self.move.uci())
        self.calls, self.closed = [], []

    def run_choice(self, *, uci="f1c4", motif="piece_coordination", cp=20, changes=None,
                   final=None, returned=None, delay=0, error=None, budget=500,
                   base_packets=None, exclusions=(), proposal_override=None, packet_key=None):
        def search(amount, **kwargs):
            self.calls.append((amount, kwargs))
            if delay:
                time.sleep(delay)
            if error:
                raise error
            common = packet(self.board, uci, cp=cp, **(changes or {}))
            return chess.engine.BestMove(chess.Move.from_uci(returned or uci), None), final or common, {
                common["depth"] if packet_key is None else packet_key: common}
        proposal = {"uci": uci, "motif": motif, "proposal_points": 9,
                    "proposed_motifs": {motif: 9}}
        with patch.object(policy, "proposals", return_value=proposal_override if proposal_override is not None else [proposal]):
            return policy.choose(self.board, self.move, self.base,
                {4: self.base} if base_packets is None else base_packets, search,
                lambda: self.closed.append(True), seed=self.seed, budget_ms=budget,
                search_plan=self.plan, excluded_uci=exclusions)

    def test_completed_equal_quality_move_retains_real_reply_motif_and_history(self):
        saved = self.board.fen(en_passant="fen"), list(self.board.move_stack), copy.deepcopy(self.plan)
        result, discarded = self.run_choice()
        self.assertFalse(discarded)
        self.assertTrue(result["applied"])
        self.assertEqual(result["selected_uci"], "f1c4")
        self.assertEqual(result["loss_cp"], 0)
        evidence = result["evidence"]
        self.assertTrue(evidence["retained_after_reply"])
        self.assertGreater(evidence["retained_motifs"]["piece_coordination"], 0)
        self.assertTrue(evidence["operations_evidence"]["settled"])
        self.assertEqual(evidence["operations_evidence"]["pv_prefix_uci"], ["f1c4", "g8f6"])
        self.assertEqual(evidence["candidate_compared"]["depth"], result["baseline"]["depth"])
        self.assertEqual(len(self.calls), 1)
        amount, options = self.calls[0]
        self.assertLess(amount, 500)
        self.assertEqual(options, {"root_move": chess.Move.from_uci("f1c4"),
                                   "publish": False, "fresh": True, "depth": 4})
        self.assertEqual((self.board.fen(en_passant="fen"), self.board.move_stack, self.plan), saved)

    def test_even_one_centipawn_loss_cannot_buy_a_creative_motif(self):
        result, _ = self.run_choice(cp=19)
        self.assertFalse(result["applied"])
        self.assertEqual(result["selected_uci"], self.move.uci())
        self.assertEqual(result["attempts"][0]["root_loss_cp"], 1)
        self.assertFalse(result["attempts"][0]["paired_search_complete"])

    def test_incomplete_bound_wrong_pov_mate_and_illegal_pv_are_rejected(self):
        cases = [{"depth": 3}, {"lowerbound": True}, {"upperbound": True},
                 {"score": chess.engine.PovScore(chess.engine.Cp(20), not self.board.turn)},
                 {"score": chess.engine.PovScore(chess.engine.Mate(2), self.board.turn)},
                 {"pv": [chess.Move.from_uci("f1c4")]},
                 {"pv": [chess.Move.from_uci("f1c4"), chess.Move.from_uci("g8g5")]}]
        for changes in cases:
            with self.subTest(changes=changes):
                result, discarded = self.run_choice(changes=changes)
                self.assertFalse(result["applied"])
                self.assertFalse(discarded)
                self.assertEqual(result["selected_uci"], self.move.uci())

    def test_returned_wrong_root_and_conflicting_final_score_are_rejected(self):
        result, _ = self.run_choice(returned="c1e3")
        self.assertFalse(result["applied"])
        result, _ = self.run_choice(final=packet(self.board, "f1c4", cp=30))
        self.assertEqual(result["attempts"][0]["reason"], "candidate_score_conflict")
        result, _ = self.run_choice(final=packet(self.board, "f1c4", cp=19, depth=5))
        self.assertEqual(result["attempts"][0]["reason"], "outside_zero_loss_floor")

    def test_packet_dictionary_key_cannot_disguise_a_different_completed_depth(self):
        result, discarded = self.run_choice(changes={"depth": 5}, packet_key=4)
        self.assertFalse(result["applied"])
        self.assertFalse(discarded)
        self.assertEqual(result["attempts"][0]["reason"], "candidate_unreliable_or_incomplete")
        self.assertEqual(result["selected_uci"], self.move.uci())

    def test_baseline_needs_its_own_matching_completed_packet(self):
        for packets in ({}, {4: packet(self.board, "a2a3", cp=19)}):
            result, _ = self.run_choice(base_packets=packets)
            self.assertEqual(result["reason"], "baseline_packet_conflict")
        self.assertFalse(self.calls)

    def test_current_context_cannot_be_reused_after_mutation(self):
        original = copy.deepcopy(self.plan)
        for field, value in (("fen", chess.STARTING_FEN), ("context_id", "stale"),
                             ("goal", "foreign"), ("active", False)):
            self.plan = copy.deepcopy(original)
            self.plan["operations_context"][field] = value
            result, _ = self.run_choice()
            self.assertEqual(result["reason"], "context_changed")
        self.plan = {"eligible": True, "mode": "initiative"}
        self.assertEqual(self.run_choice()[0]["reason"], "context_changed")
        self.assertFalse(self.calls)

    def test_knowledge_and_authored_contexts_are_independently_revalidated(self):
        board = replay("e2e4 e7e5 g1f3 b8c6 f1c4 f8c5 b2b4 c5b4")
        plan = boss_play.plan(board, 1000, self.seed)
        self.assertIsNotNone(policy._context(board, plan, self.seed))
        self.assertIn("knowledge_context", plan)
        plan["knowledge_context"]["family_id"] = "foreign"
        self.assertIsNone(policy._context(board, plan, self.seed))
        source = next(row for row in policy.boss_openings.repertoire() if row["line_id"] == "danish")
        before = chess.Board()
        for uci in source["moves"][:-1]:
            before.push_uci(uci)
        receipt = {"policy": "regicide-boss-play-v10", "ply": len(source["moves"]),
            "line_id": "danish", "line_complete": True, "history_uci": source["moves"][:-1],
            "before_fen": before.fen(en_passant="fen"), "seed": self.seed,
            "candidate_uci": source["moves"][-1], "selected_uci": source["moves"][-1]}
        self.plan["initiative_context"] = policy.attack.context(self.board, [receipt])
        self.assertIsNotNone(policy._context(self.board, self.plan, self.seed))
        self.plan["initiative_context"]["source_uci"] = "a2a3"
        self.assertIsNone(policy._context(self.board, self.plan, self.seed))

    def test_current_repetition_check_endgame_and_decisive_score_skip_search(self):
        self.board = replay(DANISH + " g1f3 g8f6 f3g1 f6g8")
        result, _ = self.run_choice()
        self.assertEqual(result["reason"], "forced_terminal_or_repeated_position")
        self.board = replay("e2e4 d7d5 f1b5")
        self.move = chess.Move.from_uci("b8c6")
        result, _ = self.run_choice()
        self.assertEqual(result["reason"], "forced_terminal_or_repeated_position")
        self.board = replay(DANISH)
        self.move = chess.Move.from_uci("a2a3")
        with patch.object(policy.dynamic_play, "_phase", return_value=7):
            self.assertEqual(self.run_choice()[0]["reason"], "simplified_position")
        for cp in (300, -300):
            self.base = packet(self.board, self.move.uci(), cp=cp)
            self.assertEqual(self.run_choice()[0]["reason"], "forcing_or_decisive_baseline")
        self.assertFalse(self.calls)

    def test_defensive_transition_and_new_king_pressure_preserve_baseline(self):
        with patch.object(policy, "_context", return_value="defensive_transition"):
            result, discarded = self.run_choice()
        self.assertEqual(result["reason"], "defensive_transition_preserved")
        self.assertFalse(discarded)
        self.assertFalse(self.calls)
        # Qh4 attacks f2 on the actual legal reply, despite the controlled
        # search score claiming equality. A new quiet plan may not add risk.
        result, discarded = self.run_choice(changes={"reply": "d8h4"})
        self.assertFalse(result["applied"])
        self.assertFalse(discarded)
        self.assertEqual(result["attempts"][0]["reason"], "king_danger_increased")
        self.assertEqual(result["attempts"][0]["operations_evidence"]["king_danger_reduction"], -1)

    def test_matching_context_still_requires_the_player_who_owns_the_gambit(self):
        foreign = copy.deepcopy(self.plan["operations_context"])
        foreign["owner"] = "black"
        self.plan["operations_context"] = foreign
        with patch.object(policy.operations, "context", return_value=foreign):
            self.assertIsNone(policy._context(self.board, self.plan, self.seed))
        board = replay("e2e4 e7e5 g1f3 b8c6 f1c4 f8c5 b2b4 c5b4")
        plan = boss_play.plan(board, 1000, self.seed)
        plan.pop("operations_context", None)
        plan["knowledge_context"]["owner"] = "black"
        with patch.object(policy.gambit_knowledge, "context", return_value=plan["knowledge_context"]):
            self.assertIsNone(policy._context(board, plan, self.seed))
        black = replay("d2d4 g8f6 c2c4 e7e5 d4e5 f6g4 g1f3")
        self.assertIsNotNone(policy._context(black, boss_play.plan(black, 1000, self.seed), self.seed))

    def test_context_source_failure_preserves_baseline_and_discards_search_session(self):
        with patch.object(policy.operations, "context", side_effect=OSError("controlled")):
            result, discarded = self.run_choice()
        self.assertTrue(discarded)
        self.assertFalse(result["applied"])
        self.assertEqual(result["reason"], "creativity_context_failed")
        self.assertEqual(result["selected_uci"], self.move.uci())
        self.assertFalse(self.calls)

    def test_twofold_repetition_later_in_candidate_pv_is_not_creativity(self):
        pv = [chess.Move.from_uci(u) for u in "g1f3 g8f6 f3g1 f6g8".split()]
        result, _ = self.run_choice(uci="g1f3", changes={"pv": pv})
        self.assertFalse(result["applied"])
        self.assertEqual(result["attempts"][0]["reason"], "repeated_candidate_continuation")

    def test_real_undefended_queen_offer_is_rejected_before_native_search(self):
        result, _ = self.run_choice(uci="d1d6", motif="king_pressure")
        self.assertFalse(result["applied"])
        self.assertEqual(result["attempts"][0]["reason"], "new_piece_donation")
        self.assertFalse(self.calls)

    def test_candidate_needs_settled_actual_exchange_and_retained_motif(self):
        with patch.object(policy, "_motifs", return_value={}):
            result, _ = self.run_choice()
        self.assertEqual(result["attempts"][0]["reason"], "motif_not_retained_after_reply")
        self.board = replay("d2d4 d7d5 e2e4 d5e4 b1c3 g8f6 f2f3 e4f3 g1f3 c8g4")
        self.plan = boss_play.plan(self.board, 1000, self.seed)
        self.move = chess.Move.from_uci("c1e3")
        self.base = packet(self.board, "c1e3", reply="b8c6")
        result, _ = self.run_choice(uci="h2h3", motif="pawn_break", changes={"reply": "g4f3"})
        self.assertFalse(result["applied"])
        self.assertEqual(result["attempts"][0]["reason"], "unsettled_candidate")

    def test_different_seed_changes_proposals_without_touching_board_or_strength_floor(self):
        before = self.board.fen(), list(self.board.move_stack)
        orders = [policy.proposals(self.board, self.move, str(seed)) for seed in range(16)]
        self.assertGreater(len({rows[0]["uci"] for rows in orders if rows}), 1)
        self.assertGreater(len({tuple(sorted(row["uci"] for row in rows)) for rows in orders}), 1)
        for seed, rows in enumerate(orders):
            self.assertEqual(rows, policy.proposals(self.board, self.move, str(seed)))
            self.assertLessEqual(len(rows), policy.MAX_SCREENED)
            self.assertEqual(len(rows), len({row["uci"] for row in rows}))
            for row in rows:
                move = chess.Move.from_uci(row["uci"])
                self.assertIn(move, self.board.legal_moves)
                self.assertFalse(self.board.is_capture(move) or self.board.gives_check(move))
        self.assertEqual((self.board.fen(), self.board.move_stack), before)
        self.assertEqual(policy.LOSS_CAP_CP, 0)

    def test_excluded_moves_and_invalid_exclusion_arguments_are_not_probed(self):
        excluded = [move.uci() for move in self.board.legal_moves]
        self.assertEqual(policy.proposals(self.board, self.move, self.seed, excluded), [])
        for invalid in (None, 42, "f1c4", [None], {"x": "f1c4"}):
            result, _ = self.run_choice(exclusions=invalid)
            self.assertEqual(result["reason"], "invalid_exclusions")
        self.assertFalse(self.calls)

    def test_only_one_safe_native_probe_even_with_multiple_candidates(self):
        rows = [{"uci": u, "motif": "piece_coordination", "proposal_points": 9,
                 "proposed_motifs": {"piece_coordination": 9}} for u in ("f1c4", "c1e3")]
        result, _ = self.run_choice(cp=0, proposal_override=rows)
        self.assertFalse(result["applied"])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual({move.uci() for move in self.calls[0][1]["root_moves"]}, {"f1c4", "c1e3"})

    def test_second_and_third_native_winners_keep_their_own_evidence(self):
        rows = [{"uci": "f1c4", "motif": "king_pressure", "proposal_points": 5,
                 "proposed_motifs": {"king_pressure": 5}},
                {"uci": "c1e3", "motif": "piece_coordination", "proposal_points": 8,
                 "proposed_motifs": {"piece_coordination": 8}},
                {"uci": "c1f4", "motif": "piece_coordination", "proposal_points": 10,
                 "proposed_motifs": {"piece_coordination": 10}}]
        for winner in ("c1e3", "c1f4"):
            with self.subTest(winner=winner):
                self.calls.clear()
                result, discarded = self.run_choice(uci=winner, proposal_override=rows)
                self.assertFalse(discarded)
                self.assertTrue(result["applied"])
                self.assertEqual(result["selected_uci"], winner)
                self.assertEqual(result["evidence"]["motif"], "piece_coordination")
                self.assertEqual(result["evidence"]["candidate_compared"]["uci"], winner)
                self.assertTrue(result["evidence"]["material_screen"]["admitted"])
                self.assertEqual(len(self.calls), 1)
                self.assertEqual({m.uci() for m in self.calls[0][1]["root_moves"]}, {r["uci"] for r in rows})
                for attempt in result["attempts"]:
                    if attempt["uci"] != winner:
                        self.assertNotIn("candidate_compared", attempt)
                        self.assertNotIn("candidate_final", attempt)
                        self.assertFalse(attempt["admitted"])

    def test_outside_pool_or_failed_winner_cannot_select_an_unscored_loser(self):
        rows = [{"uci": u, "motif": "piece_coordination", "proposal_points": 9,
                 "proposed_motifs": {"piece_coordination": 9}} for u in ("f1c4", "c1e3")]
        result, discarded = self.run_choice(returned="a2a4", proposal_override=rows)
        self.assertFalse(discarded)
        self.assertFalse(result["applied"])
        self.assertEqual(result["reason"], "candidate_outside_allowed_pool")
        self.assertEqual(result["selected_uci"], self.move.uci())
        result, discarded = self.run_choice(uci="c1e3", cp=19, proposal_override=rows)
        self.assertFalse(result["applied"])
        self.assertEqual(result["selected_uci"], self.move.uci())
        self.assertNotIn("candidate_compared", result["attempts"][0])
        self.assertEqual(result["attempts"][1]["root_loss_cp"], 1)
        self.assertFalse(result["attempts"][1]["paired_search_complete"])

    def test_material_rejected_moves_never_enter_the_native_pool(self):
        rows = [{"uci": "d1d6", "motif": "king_pressure", "proposal_points": 20,
                 "proposed_motifs": {"king_pressure": 20}},
                {"uci": "c1e3", "motif": "piece_coordination", "proposal_points": 9,
                 "proposed_motifs": {"piece_coordination": 9}}]
        result, _ = self.run_choice(uci="c1e3", proposal_override=rows)
        self.assertTrue(result["applied"])
        self.assertEqual(result["candidate_pool"], ["c1e3"])
        self.assertEqual(self.calls[0][1]["root_move"].uci(), "c1e3")
        self.assertEqual(result["attempts"][0]["reason"], "new_piece_donation")
        self.assertNotIn("candidate_compared", result["attempts"][0])

    def test_pool_screens_share_the_deadline_with_the_only_search(self):
        rows = [{"uci": u, "motif": "piece_coordination", "proposal_points": 9,
                 "proposed_motifs": {"piece_coordination": 9}} for u in ("f1c4", "c1e3")]
        deadlines = []
        def slow_screen(_board, _move, deadline):
            deadlines.append(deadline)
            time.sleep(.06)
            return {"admitted": True, "reason": "controlled", "captures": []}
        with patch.object(policy.attack, "_material_screen", side_effect=slow_screen):
            result, discarded = self.run_choice(proposal_override=rows, budget=100)
        self.assertEqual(len(set(deadlines)), 1)
        self.assertFalse(self.calls)
        self.assertTrue(discarded)
        self.assertFalse(result["applied"])
        self.assertEqual(result["reason"], "creativity_probe_timeout")

    def run_pair(self, *, candidate="f1c4", motif="piece_coordination", root_loss=5,
                 base_child_cp=-50, candidate_child_cp=-51, corrupt_role=None, corruption=None,
                 child_pv=None, root_delay=0, candidate_delay=0, budget=500):
        self.calls.clear()
        original_history = [m.uci() for m in self.board.move_stack]
        child_pv = child_pv or {}
        def continuation(position):
            first = next((chess.Move.from_uci(u) for u in ("g8f6", "b1c3")
                          if chess.Move.from_uci(u) in position.legal_moves), next(iter(position.legal_moves)))
            after = position.copy(stack=True)
            after.push(first)
            second = next(iter(after.legal_moves))
            return [first, second]
        def root_packet(move, cp):
            child = self.board.copy(stack=True)
            child.push(move)
            return {"depth": 4, "score": chess.engine.PovScore(chess.engine.Cp(cp), self.board.turn),
                    "pv": [move, continuation(child)[0]]}
        self.base = root_packet(self.move, 20)
        def search(amount, **kwargs):
            self.calls.append((amount, kwargs))
            if "position" not in kwargs:
                if root_delay:
                    time.sleep(root_delay)
                move = chess.Move.from_uci(candidate)
                value = root_packet(move, 20 - root_loss)
                return chess.engine.BestMove(move, None), value, {4: value}
            position = kwargs["position"]
            role = "baseline" if position.peek() == self.move else "candidate"
            self.assertEqual([m.uci() for m in position.move_stack], original_history + [position.peek().uci()])
            self.assertEqual(kwargs["depth"], 3)
            self.assertTrue(kwargs["fresh"])
            self.assertFalse(kwargs["publish"])
            self.assertNotIn("root_move", kwargs)
            self.assertNotIn("root_moves", kwargs)
            if role == "candidate" and candidate_delay:
                time.sleep(candidate_delay)
            pv = ([chess.Move.from_uci(u) for u in child_pv[role].split()] if role in child_pv
                  else continuation(position))
            value = {"depth": 3, "score": chess.engine.PovScore(chess.engine.Cp(
                base_child_cp if role == "baseline" else candidate_child_cp), position.turn), "pv": pv}
            final = copy.deepcopy(value)
            returned, packets = pv[0], {3: value}
            if role == corrupt_role:
                if corruption == "error":
                    raise RuntimeError("controlled child failure")
                elif corruption == "missing":
                    packets = {}
                elif corruption == "wrong_depth":
                    value["depth"] = final["depth"] = 2
                elif corruption == "wrong_pov":
                    value["score"] = final["score"] = chess.engine.PovScore(chess.engine.Cp(-50), not position.turn)
                elif corruption == "bound":
                    value["lowerbound"] = final["lowerbound"] = True
                elif corruption == "mate":
                    value["score"] = final["score"] = chess.engine.PovScore(chess.engine.Mate(2), position.turn)
                elif corruption == "wrong_return":
                    returned = next(move for move in position.legal_moves if move != pv[0])
                elif corruption == "different_final_pv":
                    after = position.copy(stack=True)
                    after.push(pv[0])
                    final["pv"] = [pv[0], next(move for move in after.legal_moves if move != pv[1])]
                elif corruption == "lost_history":
                    position.clear_stack()
            return chess.engine.BestMove(returned, None), final, packets
        row = {"uci": candidate, "motif": motif, "proposal_points": 9,
               "proposed_motifs": {motif: 9}}
        with patch.object(policy, "proposals", return_value=[row]):
            return policy.choose(self.board, self.move, self.base, {4: self.base}, search,
                lambda: self.closed.append(True), seed=self.seed, budget_ms=budget, search_plan=self.plan)

    def test_paired_children_reverse_pov_and_preserve_original_root_loss_for_both_colors(self):
        for black in (False, True):
            with self.subTest(black=black):
                if black:
                    self.board = replay("d2d4 g8f6 c2c4 e7e5 d4e5 f6g4 g1f3")
                    self.move = chess.Move.from_uci("g7g6")
                    self.plan = boss_play.plan(self.board, 1000, self.seed)
                saved = self.board.fen(), list(self.board.move_stack)
                result, discarded = self.run_pair(candidate="b8c6" if black else "f1c4")
                self.assertFalse(discarded)
                self.assertTrue(result["applied"])
                e = result["evidence"]
                self.assertEqual(e["comparison_kind"], "paired_unrestricted_children")
                self.assertEqual(e["root_loss_cp"], 5)
                self.assertEqual(e["paired_loss_cp"], 0)
                self.assertEqual(e["candidate_compared"]["cp"], 15)
                self.assertEqual(e["baseline_compared"]["cp"], 50)
                self.assertEqual(e["selection_compared"]["cp"], 51)
                self.assertEqual(e["selection_compared"]["cp_white"], -51 if black else 51)
                self.assertEqual(e["selection_compared"]["depth_basis"], "fixed_root_ply_plus_completed_child_depth")
                self.assertEqual(e["child_depth"], 3)
                self.assertEqual(len(self.calls), 3)
                self.assertEqual((self.board.fen(), self.board.move_stack), saved)

    def test_root_band_only_authorizes_work_and_pair_still_requires_zero_loss(self):
        result, _ = self.run_pair(root_loss=11)
        self.assertFalse(result["applied"])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(result["attempts"][0]["reason"], "outside_zero_loss_floor")
        result, _ = self.run_pair(root_loss=10, candidate_child_cp=-49)
        self.assertFalse(result["applied"])
        self.assertEqual(result["attempts"][0]["paired_loss_cp"], 1)
        self.assertEqual(result["attempts"][0]["reason"], "outside_paired_zero_loss_floor")
        result, _ = self.run_pair(root_loss=0)
        self.assertTrue(result["applied"])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(result["comparison_kind"], "native_same_depth_creativity")
        self.assertNotIn("paired_loss_cp", result)

    def test_either_child_must_finish_matching_exact_depth_pov_move_and_pv(self):
        for role in ("baseline", "candidate"):
            for corruption in ("missing", "wrong_depth", "wrong_pov", "bound", "mate",
                               "wrong_return", "different_final_pv", "lost_history"):
                with self.subTest(role=role, corruption=corruption):
                    result, discarded = self.run_pair(corrupt_role=role, corruption=corruption)
                    self.assertFalse(result["applied"])
                    self.assertFalse(discarded)
                    self.assertEqual(result["selected_uci"], self.move.uci())

    def test_actual_child_reply_must_keep_the_motif_and_both_kings_safe(self):
        result, _ = self.run_pair(motif="king_pressure", child_pv={"candidate": "d7d5 c4d5"})
        self.assertFalse(result["applied"])
        self.assertEqual(result["attempts"][0]["reason"], "motif_not_retained_after_reply")
        for role in ("baseline", "candidate"):
            result, _ = self.run_pair(child_pv={role: "d8h4 g1f3"})
            self.assertFalse(result["applied"])
            self.assertIn("king_danger_increased", result["attempts"][0]["reason"])

    def test_actual_child_exchanges_must_settle_for_baseline_and_candidate(self):
        result, _ = self.run_pair(child_pv={"baseline": "f8a3"})
        self.assertFalse(result["applied"])
        self.assertEqual(result["attempts"][0]["reason"], "paired_baseline_unsettled_candidate")
        self.board = replay("d2d4 d7d5 e2e4 d5e4 b1c3 g8f6 f2f3 e4f3 g1f3 c8g4")
        self.move = chess.Move.from_uci("c1e3")
        self.plan = boss_play.plan(self.board, 1000, self.seed)
        result, _ = self.run_pair(candidate="h2h3", motif="pawn_break",
                                 child_pv={"baseline": "b8c6", "candidate": "g4f3"})
        self.assertFalse(result["applied"])
        self.assertEqual(result["attempts"][0]["reason"], "unsettled_candidate")

    def test_child_failure_retains_root_evidence_and_never_selects_half_a_pair(self):
        result, discarded = self.run_pair(corrupt_role="candidate", corruption="error")
        self.assertTrue(discarded)
        self.assertFalse(result["applied"])
        self.assertEqual(result["reason"], "creativity_probe_failed")
        attempt = result["attempts"][0]
        self.assertEqual(attempt["root_loss_cp"], 5)
        self.assertEqual(attempt["candidate_compared"]["cp"], 15)
        self.assertIsNotNone(attempt["paired_child_baseline"]["final"])
        self.assertFalse(attempt["paired_search_complete"])

    def test_repeated_baseline_child_is_preserved_and_time_limits_cover_both_children(self):
        result, _ = self.run_pair(child_pv={"baseline": "g8f6 g1f3 f6g8 f3g1"})
        self.assertFalse(result["applied"])
        self.assertEqual(result["attempts"][0]["reason"], "paired_baseline_repeated_candidate_continuation")
        result, discarded = self.run_pair(root_delay=.16, budget=200)
        self.assertFalse(result["applied"])
        self.assertFalse(discarded)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(result["attempts"][0]["reason"], "insufficient_paired_recheck_budget")
        result, discarded = self.run_pair(candidate_delay=.25, budget=200)
        self.assertFalse(result["applied"])
        self.assertTrue(discarded)
        self.assertEqual(result["reason"], "creativity_probe_timeout")

    def test_short_budget_disabled_failed_search_and_timeout_preserve_baseline(self):
        for budget in (0, -1, 39, True, float("nan"), float("inf")):
            result, _ = self.run_choice(budget=budget)
            self.assertFalse(result["applied"])
        with patch.object(policy, "ENABLED", False):
            self.assertEqual(self.run_choice()[0]["reason"], "creativity_disabled")
        self.assertFalse(self.calls)
        result, discarded = self.run_choice(error=RuntimeError("controlled"))
        self.assertTrue(discarded)
        self.assertEqual(result["reason"], "creativity_probe_failed")
        result, discarded = self.run_choice(delay=.25, budget=100)
        self.assertTrue(discarded)
        self.assertEqual(result["reason"], "creativity_probe_timeout")
        self.assertTrue(self.closed)
        self.assertEqual(result["selected_uci"], self.move.uci())


if __name__ == "__main__":
    unittest.main()
