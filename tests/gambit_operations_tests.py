"""Operational planning after actual sacrifices, including off-book positions."""
import copy
import json
from pathlib import Path
import sys
import unittest
import tempfile
from unittest.mock import patch

import chess
import chess.engine

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import boss_play
import gambit_operations as operations
import gambit_repertoire_play as verifier
import gambit_knowledge


def replay(moves):
    board = chess.Board()
    for uci in moves:
        board.push_uci(uci)
    return board


class OperationsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = json.loads(operations.CURRICULUM.read_text(encoding="utf-8"))["cases"]

    def setUp(self):
        self.case = next(row for row in self.cases if row["family_id"] == "evans")
        self.board = replay(self.case["moves"])

    def test_all_curriculum_positions_need_no_invented_engine_decision(self):
        for case in self.cases:
            with self.subTest(case=case["case_id"]):
                board = replay(case["moves"])
                before = board.fen(), list(board.move_stack)
                ctx = operations.context(board)
                self.assertTrue(ctx["active"])
                self.assertTrue(ctx["accepted"])
                self.assertEqual(ctx["family_id"], case["family_id"])
                self.assertGreater(ctx["material_debt_cp"], 0)
                self.assertIn(ctx["goal"], operations.GOALS)
                self.assertEqual((board.fen(), board.move_stack), before)

    def test_undo_before_acceptance_and_historyless_fen_have_no_operational_credit(self):
        board = replay(self.case["moves"][:self.case["acceptance"]["capture_ply"] - 1])
        self.assertFalse(operations.context(board)["active"])
        self.assertFalse(operations.context(chess.Board(self.board.fen()))["active"])
        changed = self.board.copy(stack=True)
        changed.halfmove_clock += 1
        self.assertEqual(operations.context(changed)["reason"], "altered_board")

    def test_context_continues_after_twenty_ply_and_catalog_end(self):
        board = replay(self.cases[0]["moves"])
        for _ in range(16):
            selected = None
            for move in list(board.legal_moves):
                if board.is_capture(move) or board.gives_check(move):
                    continue
                board.push(move)
                if not board.is_repetition(2) and not board.is_game_over(claim_draw=True):
                    selected = move
                    break
                board.pop()
            if selected is None:
                self.fail("No quiet branch for the controlled history fixture")
        ctx = operations.context(board)
        self.assertGreater(ctx["elapsed_since_acceptance_plies"], 20)
        self.assertTrue(ctx["accepted"])
        self.assertTrue(ctx["active"] or ctx["reason"] == "opponent_to_move")

    def test_window_and_forced_game_keep_full_strength(self):
        late = next(row for row in self.cases if len(row["moves"]) > row["acceptance"]["capture_ply"])
        with patch.object(operations, "MAX_AFTER_ACCEPTANCE_PLIES", 0):
            self.assertEqual(operations.context(replay(late["moves"]))["reason"], "window_finished")
        self.assertEqual(boss_play.phase(self.board, forced=True, budget_ms=1000), "full_strength")
        self.assertFalse(boss_play.plan(self.board, 1000, "test", forced=True)["eligible"])

    def test_defense_and_consolidation_are_not_automatically_attack(self):
        checked = chess.Board("6k1/5ppp/8/8/8/8/5PqP/6K1 w - - 0 30")
        self.assertTrue(checked.is_check())
        self.assertEqual(operations.goal(checked, chess.WHITE), "defensive_transition")
        quiet = chess.Board("4k3/8/8/8/8/8/4P3/4K3 w - - 0 1")
        self.assertEqual(operations.goal(quiet, chess.WHITE), "consolidation")

    def test_proposals_are_legal_deterministic_and_state_bound(self):
        rows = operations.proposals(self.board, "same")
        self.assertTrue(rows)
        self.assertEqual(rows, operations.proposals(self.board, "same"))
        self.assertEqual(len({row["uci"] for row in rows}), len(rows))
        for row in rows:
            self.assertIn(chess.Move.from_uci(row["uci"]), self.board.legal_moves)
            self.assertTrue(operations.valid_proposal(self.board, row))
            for key, value in [("goal", "forged"), ("context_id", "old"), ("fen", "old"),
                               ("history_uci", []), ("curriculum_sha256", "wrong")]:
                self.assertFalse(operations.valid_proposal(self.board, {**row, key: value}))

    def test_plan_and_memo_record_operational_provenance(self):
        plan = boss_play.plan(self.board, 1000, "plan")
        self.assertEqual(plan["candidate_source"], "post_sacrifice_operations")
        self.assertEqual(plan["mode"], "initiative")
        self.assertEqual(plan["operations_context"]["family_id"], "evans")
        self.assertLessEqual(len(plan["knowledge_candidates"]), 4)
        with patch.object(boss_play.gambit_practice_memory, "memory_identity", return_value={"memory_id": "A"}):
            a = boss_play.memo_key(self.board, 1000, "bin", "eval")
        with patch.object(boss_play.gambit_practice_memory, "memory_identity", return_value={"memory_id": "B"}):
            b = boss_play.memo_key(self.board, 1000, "bin", "eval")
        self.assertNotEqual(a, b)

    def test_reply_measurement_does_not_relabel_material_before_a_recapture(self):
        move = next(iter(self.board.legal_moves))
        after = self.board.copy(stack=True)
        after.push(move)
        reply = next(iter(after.legal_moves))
        pv = [move.uci(), reply.uci()]
        result = operations.continuation_evidence(self.board, pv, "material_recovery")
        self.assertIsNotNone(result)
        after.push(reply)
        self.assertEqual(result["material_change_cp"],
            operations.material_balance(after, self.board.turn) - operations.material_balance(self.board, self.board.turn))
        self.assertIsNone(operations.continuation_evidence(self.board, pv[:1], "material_recovery"))
        self.assertIsNone(operations.continuation_evidence(self.board, [move.uci(), "a1a8"], "development"))

    def test_stale_operation_context_cannot_authorize_native_probes(self):
        plan = boss_play.plan(self.board, 1000, "stale")
        plan["operations_context"]["context_id"] = "old"
        move = next(iter(self.board.legal_moves))
        after = self.board.copy(stack=True)
        after.push(move)
        reply = next(iter(after.legal_moves))
        info = {"depth": 6, "score": chess.engine.PovScore(chess.engine.Cp(0), self.board.turn), "pv": [move, reply]}
        result, discard = verifier.choose(self.board, plan, move, info, {6: info}, "stale",
            lambda *_a, **_k: self.fail("stale search"), lambda: None)
        self.assertEqual(result["reason"], "operations_context_changed")
        self.assertEqual(result["selected_uci"], move.uci())
        self.assertFalse(discard)

    def test_malformed_optional_curriculum_does_not_block_gameplay(self):
        valid = json.loads(operations.CURRICULUM.read_text(encoding="utf-8"))
        for malformed in [{}, "wrong", [], {"cases": ["bad"]}, {**valid, "curriculum_sha256": "wrong"}]:
            with self.subTest(kind=type(malformed).__name__), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "invalid.json"
                path.write_text(json.dumps(malformed), encoding="utf-8")
                with patch.object(operations, "CURRICULUM", path):
                    self.assertFalse(operations.metadata()["available"])
                    self.assertFalse(operations.context(self.board)["active"])
                    self.assertTrue(boss_play.plan(self.board, 1000, "fallback")["eligible"])

    def controlled_choice(self, plan, candidate_cp):
        base = chess.Move.from_uci("h2h3")
        def packet(move, cp):
            after = self.board.copy(stack=True)
            after.push(move)
            reply = next(iter(after.legal_moves))
            return {"depth": 6, "score": chess.engine.PovScore(chess.engine.Cp(cp), self.board.turn),
                    "pv": [move, reply]}
        info = packet(base, 0)
        calls = []
        def search(amount, **kwargs):
            calls.append(kwargs)
            move = kwargs["root_move"]
            candidate = packet(move, candidate_cp)
            return chess.engine.BestMove(move, None), candidate, {6: candidate}
        value, discard = verifier.choose(self.board, plan, base, info, {6: info}, "safety", search, lambda: None)
        return value, discard, calls

    def test_defensive_goal_enforces_fifteen_cp_instead_of_attack_allowance(self):
        with patch.object(operations, "goal", return_value="defensive_transition"):
            plan = boss_play.plan(self.board, 1000, "safety")
            plan["knowledge_candidates"] = gambit_knowledge.proposals(self.board, "safety")[:1]
            value, discard, calls = self.controlled_choice(plan, -20)
        self.assertTrue(calls)
        self.assertFalse(discard)
        self.assertEqual(value["loss_cap_cp"], 15)
        self.assertFalse(value["applied"])
        self.assertEqual(value["attempts"][0]["reason"], "outside_quality_floor")

    def test_an_unchanged_plan_after_the_opponent_reply_cannot_force_a_switch(self):
        plan = boss_play.plan(self.board, 1000, "safety")
        plan["knowledge_candidates"] = gambit_knowledge.proposals(self.board, "safety")[:1]
        with patch.object(operations, "continuation_evidence", return_value={"points": 0}):
            value, discard, calls = self.controlled_choice(plan, 0)
        self.assertTrue(calls)
        self.assertFalse(discard)
        self.assertFalse(value["applied"])
        self.assertEqual(value["attempts"][0]["reason"], "no_reply_supported_operations_gain")

    def test_offline_memory_score_never_substitutes_for_current_native_evaluation(self):
        plan = boss_play.plan(self.board, 1000, "safety")
        plan["knowledge_candidates"] = [{"uci": "c2c3", "source": "native_practice_memory",
            "offline_evidence": {"offline_owner_cp": 99999}, "record_id": "controlled-memory"}]
        with patch.object(verifier.gambit_practice_memory, "valid_proposal", return_value=True):
            value, discard, calls = self.controlled_choice(plan, -300)
        self.assertTrue(calls)
        self.assertFalse(discard)
        self.assertFalse(value["applied"])
        self.assertEqual(value["selected_uci"], "h2h3")

    def test_unpracticed_geometry_cannot_buy_a_plan_with_native_evaluation_loss(self):
        plan = boss_play.plan(self.board, 1000, "safety")
        choices = [row for row in operations.proposals(self.board, "safety") if row["uci"] != "h2h3"]
        plan["knowledge_candidates"] = choices[:1]
        with patch.object(operations, "continuation_evidence", return_value={"points": 100}):
            value, discard, calls = self.controlled_choice(plan, -1)
        self.assertTrue(calls)
        self.assertFalse(discard)
        self.assertFalse(value["applied"])
        self.assertEqual(value["attempts"][0]["loss_cap_cp"], 0)
        self.assertEqual(value["attempts"][0]["reason"], "outside_quality_floor")

    def test_reproduced_kings_nc3_concession_is_rejected(self):
        board = replay("e2e4 e7e5 f2f4 e5f4 d1g4 g7g5 g1f3 h7h6".split())
        base = chess.Move.from_uci("f1b5")
        candidate = chess.Move.from_uci("b1c3")
        plan = boss_play.plan(board, 1000, "kings-regression")
        row = next(row for row in operations.proposals(board, "kings-regression", limit=8)
                   if row["uci"] == "b1c3")
        plan["knowledge_candidates"] = [row]
        info = {"depth": 5, "score": chess.engine.PovScore(chess.engine.Cp(-120), board.turn),
                "pv": [chess.Move.from_uci(uci) for uci in "f1b5 c7c6 b5d3 g8f6 g4f5".split()]}
        checked = {"depth": 5, "score": chess.engine.PovScore(chess.engine.Cp(-141), board.turn),
                   "pv": [chess.Move.from_uci(uci) for uci in "b1c3 d7d5 g4h5 g8f6 f1b5 b8c6".split()]}
        def search(*_a, **_k):
            return chess.engine.BestMove(candidate, None), checked, {5: checked}
        result, discard = verifier.choose(board, plan, base, info, {5: info}, "kings-regression", search, lambda: None)
        self.assertFalse(discard)
        self.assertEqual(result["selected_uci"], "f1b5")
        self.assertFalse(result["applied"])
        self.assertEqual(result["attempts"][0]["reason"], "outside_quality_floor")
        self.assertEqual(result["attempts"][0]["loss_cap_cp"], 0)


if __name__ == "__main__":
    unittest.main()
