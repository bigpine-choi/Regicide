"""Native evidence gates for sourced and learned gambit continuations."""
import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import chess
import chess.engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import boss_play
import gambit_knowledge
import gambit_repertoire_play as policy


def packet(board, uci, cp=0, depth=6, **changes):
    move = chess.Move.from_uci(uci)
    after = board.copy(stack=True)
    after.push(move)
    reply = next(iter(after.legal_moves))
    return {"score": chess.engine.PovScore(chess.engine.Cp(cp), board.turn),
            "depth": depth, "pv": [move, reply], **changes}


class RepertoirePlayTests(unittest.TestCase):
    def setUp(self):
        self.board = chess.Board()
        for uci in "e2e4 e7e5 g1f3 b8c6 f1c4 f8c5 b2b4 c5b4".split():
            self.board.push_uci(uci)
        self.seed = "integration"
        # Exercise the sourced-book verifier in isolation; post-sacrifice
        # operational plans have their own integration and safety tests.
        with patch.object(boss_play.gambit_operations, "context", return_value={"active": False}):
            self.plan = boss_play.plan(self.board, 1000, self.seed)
        self.assertEqual(self.plan["candidate_source"], "gambit_knowledge")
        self.base_move = chess.Move.from_uci("h2h3")
        self.base = packet(self.board, "h2h3", 0)
        self.proposed = self.plan["knowledge_candidates"][0]["uci"]
        self.calls, self.closed = [], []

    def run_choice(self, cp=0, changes=None, final=None, error=None):
        def search(amount, **kwargs):
            self.calls.append((amount, kwargs))
            if error:
                raise error
            move = kwargs["root_move"]
            info = packet(self.board, move.uci(), cp, **(changes or {}))
            return chess.engine.BestMove(move, None), final or info, {info["depth"]: info}
        return policy.choose(self.board, self.plan, self.base_move, self.base, {6: self.base},
                             self.seed, search, lambda: self.closed.append(True))

    def test_catalog_proposal_requires_fresh_native_common_depth_and_records_source(self):
        original = self.board.fen(), list(self.board.move_stack)
        value, discard = self.run_choice(-20)
        self.assertFalse(discard)
        self.assertTrue(value["applied"])
        self.assertEqual(value["selected_uci"], self.proposed)
        self.assertEqual(value["loss_cp"], 20)
        self.assertEqual(value["evidence"]["source"]["source_license"], "CC0-1.0")
        self.assertEqual(self.calls[0][1]["depth"], 6)
        self.assertTrue(self.calls[0][1]["fresh"])
        self.assertFalse(self.calls[0][1]["publish"])
        self.assertEqual((self.board.fen(), self.board.move_stack), original)

    def test_loss_floor_applies_at_common_and_final_depth(self):
        for common, final in [(-36, None), (0, -100)]:
            with self.subTest(common=common, final=final):
                info = packet(self.board, self.proposed, final, depth=7) if final is not None else None
                value, _ = self.run_choice(common, final=info)
                self.assertFalse(value["applied"])
                self.assertEqual(value["selected_uci"], "h2h3")
                self.assertEqual(value["attempts"][0]["reason"], "outside_quality_floor")

    def test_bounds_wrong_pov_depth_mate_and_missing_reply_are_not_evidence(self):
        for change in [{"lowerbound": True}, {"upperbound": True}, {"depth": 5},
                       {"score": chess.engine.PovScore(chess.engine.Cp(0), not self.board.turn)},
                       {"score": chess.engine.PovScore(chess.engine.Mate(3), self.board.turn)},
                       {"pv": [chess.Move.from_uci(self.proposed)]}]:
            with self.subTest(change=change):
                value, _ = self.run_choice(changes=change)
                self.assertFalse(value["applied"])
                self.assertEqual(value["selected_uci"], "h2h3")

    def test_catalog_or_position_mismatch_cannot_become_a_proposal(self):
        self.plan = copy.deepcopy(self.plan)
        self.plan["knowledge_candidates"][0]["catalog_sha256"] = "old"
        value, _ = self.run_choice()
        self.assertEqual(value["reason"], "knowledge_context_changed")
        self.assertFalse(self.calls)

    def test_material_screen_blocks_new_piece_donation_before_search(self):
        with patch.object(policy.attack, "_material_screen", return_value={"admitted": False,
                          "reason": "new_piece_donation", "captures": []}):
            value, _ = self.run_choice()
        self.assertFalse(value["applied"])
        self.assertEqual(value["attempts"][0]["reason"], "new_piece_donation")
        self.assertFalse(self.calls)

    def test_native_baseline_already_in_knowledge_uses_no_extra_search(self):
        base = chess.Move.from_uci(self.proposed)
        info = packet(self.board, self.proposed)
        value, discard = policy.choose(self.board, self.plan, base, info, {6: info}, self.seed,
                                       lambda *_a, **_k: self.fail("unneeded search"), lambda: None)
        self.assertEqual(value["reason"], "knowledge_move_is_baseline")
        self.assertEqual(value["selected_uci"], self.proposed)
        self.assertFalse(discard)

    def test_model_scores_do_not_change_native_quality_floor(self):
        self.plan["knowledge_ranking"] = {"active": True, "ranking": [{"uci": self.proposed, "score": 99999}]}
        value, _ = self.run_choice(-500)
        self.assertFalse(value["applied"])
        self.assertEqual(value["selected_uci"], "h2h3")

    def test_short_budget_and_failed_search_preserve_legal_baseline(self):
        value, discard = self.run_choice(error=RuntimeError("controlled"))
        self.assertFalse(value["applied"])
        self.assertTrue(discard)
        self.plan["extra_budget_ms"] = 1
        value, discard = self.run_choice()
        self.assertEqual(value["reason"], "short_budget")
        self.assertFalse(discard)

    def test_existing_authored_offer_remains_primary_and_keeps_its_risk_contract(self):
        board = chess.Board()
        board.push_uci("d2d4")
        value = boss_play.plan(board, 1000, "authored")
        self.assertEqual(value["mode"], "opening")
        self.assertEqual(value["candidate_uci"], "e7e5")
        self.assertTrue(value["line_complete"])
        self.assertNotIn("knowledge_candidates", value)

    def test_actual_family_context_does_not_depend_on_a_different_representative_line(self):
        self.board = chess.Board()
        for uci in "e2e4 e7e5 f2f4 e5f4 g1f3 g7g5".split():
            self.board.push_uci(uci)
        decisions = [{"policy": boss_play.POLICY, "ply": 3, "line_id": "kings",
                      "selected_uci": "f2f4", "candidate_uci": "f2f4", "line_complete": True}]
        with patch.object(boss_play.gambit_operations, "context", return_value={"active": False}):
            self.plan = boss_play.plan(self.board, 1000, self.seed, decisions=decisions)
        self.proposed = self.plan["knowledge_candidates"][0]["uci"]
        self.base_move = chess.Move.from_uci("h2h3")
        self.base = packet(self.board, "h2h3")
        self.assertTrue(all(gambit_knowledge.valid_proposal(self.board, row)
                            for row in self.plan["knowledge_candidates"]))
        value, _ = self.run_choice()
        self.assertNotEqual(value["reason"], "knowledge_context_changed")
        self.assertTrue(self.calls)

    def test_memo_cannot_reuse_choices_from_a_different_learned_model(self):
        with patch.object(boss_play.gambit_learning, "model_identity", return_value={"model_id": "first"}):
            first = boss_play.memo_key(self.board, 1000, "binary", "evaluation", seed=self.seed)
        with patch.object(boss_play.gambit_learning, "model_identity", return_value={"model_id": "second"}):
            second = boss_play.memo_key(self.board, 1000, "binary", "evaluation", seed=self.seed)
        self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
