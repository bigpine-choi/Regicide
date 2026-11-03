"""Candidate reuse contract: real native receipt schema, no engine execution."""

import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import chess
import chess.engine
import boss_play
from sparring_referee_pool import build_pool


def line(board, uci, cp=30, depth=6, pv=None):
    move = chess.Move.from_uci(uci)
    info = {"depth": depth, "score": chess.engine.PovScore(chess.engine.Cp(cp), board.turn),
            "pv": [chess.Move.from_uci(item) for item in (pv or [uci])]}
    return boss_play.score_line(board, info, move)


def probe(board, uci, cp=30, depth=6, baseline_cp=100):
    return {"uci": uci, "admitted": True, "naturalness": {"admitted": True, "style_points": 1},
            "candidate_compared": line(board, uci, cp, depth),
            "candidate_final": line(board, uci, cp, depth), "compared_depth": depth,
            "reference_delta_cp": baseline_cp - cp, "loss_cp": max(0, baseline_cp - cp),
            "reason": "verified_position_candidate", "error": None}


def receipt(board=None, baseline="e2e4", proposal="d2d4"):
    board = board or chess.Board()
    return {"policy": "regicide-sparring-play-v6", "before_fen": board.fen(en_passant="fen"),
            "history_uci": [move.uci() for move in board.move_stack],
            "color": "white" if board.turn else "black", "ply": len(board.move_stack) + 1,
            "baseline_uci": baseline, "selected_uci": proposal, "baseline": line(board, baseline, 100),
            "actual_reference_depth": 6, "verification_depth": 6, "compared_depth": 6,
            "probes": [probe(board, proposal), probe(board, "g1f3"), probe(board, "b1c3"), probe(board, "e2e3")]}


class CandidatePoolTests(unittest.TestCase):
    def test_native_score_line_schema_reused_in_stable_order(self):
        board, decision = chess.Board(), receipt()
        self.assertNotIn("bound", decision["baseline"])
        pool = build_pool(board, decision)
        self.assertEqual([(row["uci"], row["origin"]) for row in pool],
                         [("e2e4", "baseline"), ("d2d4", "proposal"), ("g1f3", "verified_probe"), ("b1c3", "verified_probe")])
        self.assertEqual(pool[2]["native_evidence"]["candidate_final"]["bound"], "exact")
        self.assertIsNone(pool[2]["native_evidence"]["candidate_final"]["mate"])

    def test_original_choices_survive_missing_quality_but_probes_do_not_expand(self):
        decision = receipt()
        for key in ("baseline", "actual_reference_depth", "verification_depth", "compared_depth"):
            decision.pop(key)
        pool = build_pool(chess.Board(), decision)
        self.assertEqual([row["uci"] for row in pool], ["e2e4", "d2d4"])
        self.assertTrue(all(row["native_evidence"]["validation"] == "host_receipt_legal_choice_only" for row in pool))

    def test_duplicate_original_and_probe_moves_never_consume_extra_slots(self):
        decision = receipt(proposal="e2e4")
        decision["probes"] = [probe(chess.Board(), "e2e4"), probe(chess.Board(), "g1f3"),
                              probe(chess.Board(), "g1f3"), probe(chess.Board(), "b1c3"), probe(chess.Board(), "e2e3")]
        self.assertEqual([row["uci"] for row in build_pool(chess.Board(), decision)], ["e2e4", "g1f3", "b1c3", "e2e3"])

    def test_mismatched_context_or_illegal_original_choices_return_empty_pool(self):
        for change in ({"before_fen": "old"}, {"history_uci": ["e2e4"]}, {"history_uci": None},
                       {"history_uci": [True]}, {"color": "black"}, {"ply": True}, {"ply": 2},
                       {"baseline_uci": "e2e5"}, {"selected_uci": "e2e5"}, {"selected_uci": "0000"}):
            decision = receipt()
            decision.update(change)
            self.assertEqual(build_pool(chess.Board(), decision), [], change)

    def test_history_must_match_even_if_fen_field_is_forged_to_current_position(self):
        board = chess.Board()
        board.push_uci("e2e4")
        decision = receipt(board, "e7e5", "d7d5")
        decision["history_uci"] = []
        self.assertEqual(build_pool(board, decision), [])

    def test_bad_admission_naturalness_error_or_move_is_dropped(self):
        changes = [{"admitted": False}, {"admitted": 1}, {"naturalness": {"admitted": 1}},
                   {"naturalness": {"admitted": False}}, {"naturalness": None}, {"error": "timeout"},
                   {"uci": "e2e5"}, {"uci": "0000"}]
        for change in changes:
            decision = receipt()
            candidate = probe(chess.Board(), "g1f3")
            candidate.update(change)
            decision["probes"] = [candidate]
            self.assertEqual(len(build_pool(chess.Board(), decision)), 2, change)

    def test_finite_exact_primary_score_and_root_perspective_are_required(self):
        changes = [{"cp": True}, {"cp": 30.0}, {"cp": float("nan")}, {"cp": "30"},
                   {"cp_white": -30}, {"pov": "black"}, {"mate": 0}, {"mate": 3},
                   {"bound": "lower"}, {"bound": "upper"}, {"bound": None},
                   {"score_bound": "lower"}, {"score_bound": "upper"},
                   {"lowerbound": True}, {"upperbound": True}, {"multipv": 2}, {"multipv": True},
                   {"uci": "b1c3"}, {"fen": "wrong"}]
        for change in changes:
            decision = receipt()
            candidate = probe(chess.Board(), "g1f3")
            candidate["candidate_compared"].update(change)
            decision["probes"] = [candidate]
            self.assertEqual(len(build_pool(chess.Board(), decision)), 2, change)

    def test_final_packet_is_checked_independently_and_cannot_be_partial(self):
        for changed in ({"cp": True}, {"cp_white": -30}, {"pov": "black"}, {"depth": 5},
                        {"bound": "upper"}, {"mate": 1}, {"pv_uci": ["g1f3", "e2e4"]}):
            decision = receipt()
            candidate = probe(chess.Board(), "g1f3")
            candidate["candidate_final"].update(changed)
            decision["probes"] = [candidate]
            self.assertEqual(len(build_pool(chess.Board(), decision)), 2, changed)

    def test_full_pv_legality_and_first_root_identity_are_checked(self):
        for pv in ([], ["b1c3"], ["g1f3", "e2e4"], ["g1f3", "e7e5", "f3f8"], ["g1f3"] * 129):
            decision = receipt()
            candidate = probe(chess.Board(), "g1f3")
            candidate["candidate_compared"]["pv_uci"] = pv
            decision["probes"] = [candidate]
            self.assertEqual(len(build_pool(chess.Board(), decision)), 2, pv[:3])
        decision = receipt()
        candidate = probe(chess.Board(), "g1f3")
        candidate["candidate_compared"]["pv_uci"] = ["g1f3", "e7e5", "e2e4"]
        candidate["candidate_final"]["pv_uci"] = ["g1f3", "d7d5"]
        decision["probes"] = [candidate]
        self.assertEqual(len(build_pool(chess.Board(), decision)), 3)

    def test_stronger_baseline_depth_and_same_depth_consistency_are_preserved(self):
        changes = ["shallow", "compared_tag", "strong_reference", "same_depth_cp_conflict", "invalid_depth_tag"]
        for kind in changes:
            decision = receipt()
            candidate = probe(chess.Board(), "g1f3")
            if kind == "shallow":
                candidate["candidate_compared"]["depth"] = 3
            elif kind == "compared_tag":
                candidate["compared_depth"] = 5
            elif kind == "strong_reference":
                decision["baseline"]["depth"] = 8
                decision.update(actual_reference_depth=8, verification_depth=8, compared_depth=8)
            elif kind == "invalid_depth_tag":
                decision["verification_depth"] = True
            else:
                candidate["candidate_final"].update(cp=31, cp_white=31)
            decision["probes"] = [candidate]
            self.assertEqual(len(build_pool(chess.Board(), decision)), 2, kind)
        decision = receipt()
        candidate = probe(chess.Board(), "g1f3")
        candidate["candidate_final"].update(depth=7, cp=40, cp_white=40)
        decision["probes"] = [candidate]
        self.assertEqual(len(build_pool(chess.Board(), decision)), 3)

    def test_negative_reference_conflicts_and_inconsistent_delta_metadata_are_rejected(self):
        for cp, delta in ((126, -26), (30, -20), (30, True)):
            decision = receipt()
            candidate = probe(chess.Board(), "g1f3", cp=cp)
            candidate["reference_delta_cp"] = delta
            decision["probes"] = [candidate]
            self.assertEqual(len(build_pool(chess.Board(), decision)), 2)
        decision = receipt()
        decision["probes"] = [probe(chess.Board(), "g1f3", cp=125)]
        self.assertEqual(len(build_pool(chess.Board(), decision)), 3, "existing -25 tolerance is inclusive")

    def test_optional_raw_diagnostics_cannot_contradict_final_exact_packet(self):
        for raw in ({"cp": 30, "depth": 6, "mate": 1}, {"cp": 30, "depth": 6, "lowerbound": True},
                    {"cp": 31, "depth": 6}, {"cp": 30, "depth": 7},
                    {"cp": 30, "depth": 6, "bound": "lower"},
                    {"cp": 30, "depth": 6, "score_bound": "upper"}):
            decision = receipt()
            candidate = probe(chess.Board(), "g1f3")
            candidate["candidate_final_raw"] = raw
            decision["probes"] = [candidate]
            self.assertEqual(len(build_pool(chess.Board(), decision)), 2, raw)

    def test_black_cp_white_sign_is_verified(self):
        board = chess.Board()
        board.push_uci("e2e4")
        decision = receipt(board, "e7e5", "d7d5")
        decision["probes"] = [probe(board, "g8f6")]
        self.assertEqual(len(build_pool(board, decision)), 3)
        decision["probes"][0]["candidate_final"]["cp_white"] = 30
        self.assertEqual(len(build_pool(board, decision)), 2)

    def test_special_legal_roots_castling_en_passant_and_underpromotion(self):
        positions = [("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1", "a1a2", "a1a3", "e1g1"),
                     ("4k3/8/8/3pP3/8/8/8/4K3 w - d6 0 1", "e1d1", "e1f1", "e5d6"),
                     ("4k3/P7/8/8/8/8/8/4K3 w - - 0 1", "e1d1", "e1f1", "a7a8n")]
        for fen, baseline, proposal, extra in positions:
            board = chess.Board(fen)
            decision = receipt(board, baseline, proposal)
            decision["probes"] = [probe(board, extra)]
            self.assertEqual(build_pool(board, decision)[-1]["uci"], extra)

    def test_no_mutation_no_search_and_returned_evidence_is_independent(self):
        board, decision = chess.Board(), receipt()
        original = copy.deepcopy(decision)
        fen, history = board.fen(en_passant="fen"), list(board.move_stack)
        with patch("chess.engine.SimpleEngine.popen_uci", side_effect=AssertionError("must not search")):
            pool = build_pool(board, decision)
        self.assertEqual(decision, original)
        self.assertEqual((board.fen(en_passant="fen"), board.move_stack), (fen, history))
        pool[0]["native_evidence"]["history_uci"].append("e2e4")
        pool[2]["native_evidence"]["naturalness"]["admitted"] = False
        self.assertEqual(decision, original)

    def test_size_limits_keep_original_priority(self):
        for limit in (1, 2, 3, 4):
            self.assertEqual(len(build_pool(chess.Board(), receipt(), limit)), limit)
        for limit in (0, -1, 5, True, 1.0):
            with self.assertRaises(ValueError):
                build_pool(chess.Board(), receipt(), limit)


if __name__ == "__main__":
    unittest.main()
