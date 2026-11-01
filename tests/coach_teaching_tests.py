"""Verified teaching effects, not engine move-quality or strength assertions.

These public synthetic positions supply controlled classifications and best
candidates. Every line is legally replayed; no personal games or engine process
are needed to test why/principle wording and fail-closed behavior.
"""
import copy
import json
from pathlib import Path
import sys
import unittest

import chess

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from coach_explanations import describe_move


FIXTURES = (
    ("meet_piece_threat", "6k1/8/8/3p4/2B5/8/3P4/6K1 w - - 0 1",
     ["d2d3", "d5c4", "d3c4"], ["c4d3", "g8f7"], "mistake", "bishop", "capture"),
    ("choose_defended_square", "3q2k1/8/3b4/8/8/2P2N2/7P/3Q2K1 w - - 0 1",
     ["d1d3", "d6h2", "f3h2", "d8d3"], ["d1d4", "d6b8"], "blunder", "c-pawn", "recapture"),
    ("develop_with_threat", "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2",
     ["h2h3", "b8c6"], ["g1f3", "b8c6"], "inaccuracy", "knight", "development"),
    ("mate_first", "7k/5K2/6Q1/8/8/8/8/8 w - - 0 1",
     ["g6g5", "h8h7"], ["g6g7"], "missed_mate", "checkmate", "mate"),
    ("meet_mating_threat", "rnbqkbnr/pppp1ppp/8/4p3/8/5P2/PPPPP1PP/RNBQKBNR w KQkq - 0 2",
     ["g2g4", "d8h4"], ["g2g3", "b8c6"], "allows_mate", "mate in one", "mating threats"),
    ("count_recaptures", "3q2k1/8/8/8/8/4P3/8/3Q2K1 w - - 0 1",
     ["d1d4", "d8d4", "e3d4"], ["d1d8", "g8f7"], "blunder", "queen", "recaptures"),
)


def mirror_uci(uci):
    move = chess.Move.from_uci(uci)
    return chess.Move(chess.square_mirror(move.from_square), chess.square_mirror(move.to_square),
                      promotion=move.promotion).uci()


def source_row(played, best, kind="mistake"):
    return {"ply": 1, "uci": played[0], "san": "FORGED_ACTUAL_SAN", "color": "wrong",
            "classification": kind,
            "played": {"uci": played[0], "san": "FORGED_PLAYED_SAN", "pv_uci": list(played),
                       "pv_san": ["FORGED_PV_SAN"], "bound": "exact"},
            "best": {"uci": best[0], "san": "FORGED_BEST_SAN", "pv_uci": list(best),
                     "pv_san": ["FORGED_PV_SAN"], "bound": "exact"}}


class CoachTeachingTests(unittest.TestCase):
    def check_source(self, board, source):
        original = copy.deepcopy(source)
        original_fen, original_moves = board.fen(), tuple(board.move_stack)
        packet = describe_move(board, source)
        self.assertEqual(source, original)
        self.assertEqual(board.fen(), original_fen)
        self.assertEqual(tuple(board.move_stack), original_moves)
        if packet is not None:
            self.assertNotIn("FORGED_", json.dumps(packet))
            self.assertNotRegex(json.dumps(packet, ensure_ascii=False), r"[가-힣]")
            self.assertEqual(set(packet["perspectives"]), {"white", "black"})
            for view in (packet, *packet["perspectives"].values()):
                self.assertLessEqual(len(view["dialogue"].split()), 40)
                self.assertLessEqual(len(view["hint"].split()), 16)
                lesson = view["teaching"]
                if lesson:
                    alternative = lesson["alternative"]
                    self.assertEqual((lesson["line"], lesson["ply_index"]), ("best", 1))
                    self.assertTrue(lesson["concept"])
                    self.assertTrue(lesson["principle"])
                    self.assertIn(alternative["reason"], view["dialogue"])
                    self.assertIn(lesson["principle"], view["dialogue"])
                    self.assertEqual(alternative["uci"], source["best"]["pv_uci"][0])
                    self.assertEqual(alternative["san"], board.san(chess.Move.from_uci(alternative["uci"])))
                    self.assertIn(alternative["san"], alternative["reason"])
                    for solution in (alternative["uci"], alternative["san"], alternative["uci"][2:4]):
                        self.assertNotIn(solution, view["hint"])
        return packet

    def assert_no_teaching(self, board, source):
        packet = self.check_source(board, source)
        if packet is not None:
            for view in (packet, *packet["perspectives"].values()):
                self.assertIsNone(view["teaching"])
        return packet

    def test_six_verified_motifs_and_mirrors_have_specific_why_and_principle(self):
        for concept, fen, played, best, kind, observation, principle in FIXTURES:
            for mirrored in (False, True):
                with self.subTest(concept=concept, mirrored=mirrored):
                    board = chess.Board(fen)
                    actual_pv, best_pv = played, best
                    if mirrored:
                        board = board.mirror()
                        actual_pv, best_pv = [mirror_uci(move) for move in played], [mirror_uci(move) for move in best]
                    self.assertTrue(board.is_valid())
                    for pv in (actual_pv, best_pv):
                        probe = board.copy(stack=True)
                        for uci in pv:
                            move = chess.Move.from_uci(uci)
                            self.assertIn(move, probe.legal_moves)
                            probe.push(move)
                    packet = self.check_source(board, source_row(actual_pv, best_pv, kind))
                    lesson = packet["teaching"]
                    self.assertEqual(lesson["concept"], concept)
                    self.assertEqual(lesson["grounding"], "verified_move_effect")
                    self.assertIn(observation, lesson["alternative"]["reason"])
                    self.assertIn(principle, lesson["principle"])
                    for view in packet["perspectives"].values():
                        self.assertEqual(view["teaching"]["concept"], concept)
                        self.assertEqual(view["teaching"]["principle"], lesson["principle"])

    def test_mover_perspective_changes_ownership_without_changing_the_advice(self):
        _, fen, played, best, kind, _, _ = FIXTURES[1]
        for board, actual_pv, best_pv, own, other, name in (
            (chess.Board(fen), played, best, "white", "black", "White"),
            (chess.Board(fen).mirror(), list(map(mirror_uci, played)), list(map(mirror_uci, best)), "black", "white", "Black"),
        ):
            packet = self.check_source(board, source_row(actual_pv, best_pv, kind))
            self.assertIn("your queen", packet["perspectives"][own]["teaching"]["alternative"]["reason"])
            self.assertIn(name + "'s queen", packet["perspectives"][other]["teaching"]["alternative"]["reason"])
            self.assertNotIn("your queen", packet["perspectives"][other]["teaching"]["alternative"]["reason"])

    def test_positive_uncertain_and_pending_rows_do_not_invent_a_better_move_lesson(self):
        for kind in ("best", "good", "uncertain", "pending"):
            with self.subTest(kind=kind):
                packet = self.assert_no_teaching(chess.Board(), source_row(["a2a3"], ["e2e4"], kind))
                self.assertEqual(packet is None, kind == "pending")

    def test_bound_scores_are_not_promoted_into_confirmed_teaching(self):
        for branch in ("best", "played"):
            for bound in ("lower", "upper", "lowerbound", "upperbound", "unknown"):
                with self.subTest(branch=branch, bound=bound):
                    source = source_row(["a2a3"], ["g1f3"])
                    source[branch]["bound"] = bound
                    packet = self.assert_no_teaching(chess.Board(), source)
                    self.assertEqual(packet["mood"], "think")

    def test_same_actual_and_best_is_not_an_alternative_even_with_different_pv(self):
        for best in (["e2e4"], ["e2e4", "e7e5"]):
            self.assert_no_teaching(chess.Board(), source_row(["e2e4", "c7c5"], best, "blunder"))

    def test_invalid_or_wrong_root_pvs_cannot_ground_a_lesson(self):
        bad_lines = ([], [None], "e2e4", ["e2e5"], ["g1f3", "e7e6", "f3f5"])
        for branch in ("best", "played"):
            for bad in bad_lines:
                with self.subTest(branch=branch, bad=bad):
                    source = source_row(["a2a3"], ["g1f3"])
                    source[branch]["pv_uci"] = bad
                    self.assert_no_teaching(chess.Board(), source)
        source = source_row(["a2a3"], ["g1f3"])
        source["played"]["pv_uci"] = ["h2h3"]
        self.assert_no_teaching(chess.Board(), source)

    def test_mismatched_best_uci_does_not_explain_another_legal_move(self):
        source = source_row(["a2a3"], ["g1f3"])
        source["best"]["uci"] = "b1c3"
        self.assert_no_teaching(chess.Board(), source)

    def test_stored_fake_san_and_color_cannot_change_replayed_facts(self):
        source = source_row(["h2h3"], ["g1f3"])
        board = chess.Board()
        packet = self.check_source(board, source)
        self.assertEqual(packet["teaching"]["alternative"]["san"], "Nf3")
        self.assertIn("White's knight", packet["teaching"]["alternative"]["reason"])
        source["best"]["san"] = "Qh4#"
        source["played"]["san"] = "Qh4#"
        source["san"] = "Qh4#"
        self.assertEqual(self.check_source(board, source), packet)

    def test_history_stack_and_input_report_remain_unchanged(self):
        board = chess.Board()
        for uci in ("g1f3", "g8f6", "f3g1", "f6g8", "e2e4", "e7e5"):
            board.push_uci(uci)
        source = source_row(["h2h3", "b8c6"], ["g1f3", "b8c6"])
        source["best"]["pv_frames"] = [{"fen": "untrusted illustrative frame"}]
        packet = self.check_source(board, source)
        self.assertEqual(packet, self.check_source(board, source))
        self.assertEqual(len(board.move_stack), 6)

    def test_later_pv_mate_is_not_a_verified_mate_in_one_or_forced_finish(self):
        board = chess.Board()
        board.push_uci("f2f3")
        for position, played, best in ((board, ["a7a6"], ["e7e5", "g2g4", "d8h4"]),
                                       (board.mirror(), ["a2a3"], ["e2e4", "g7g5", "d1h5"])):
            packet = self.check_source(position, source_row(played, best, "missed_mate"))
            self.assertNotEqual(packet["teaching"]["concept"], "mate_first")
            self.assertEqual(packet["teaching"]["grounding"], "search_preference")
            for view in (packet, *packet["perspectives"].values()):
                self.assertNotRegex(view["dialogue"], r"checkmate right now|forced mate|mate in one|inevitable|unavoidable")

    def test_pinned_defender_cannot_justify_a_protected_square(self):
        initial = chess.Board("4r2k/8/8/8/8/8/4N3/3QK3 w - - 0 1")
        for board, actual, best in ((initial, "d1d3", "d1d4"), (initial.mirror(), "d8d6", "d8d5")):
            self.assertIn(chess.Move.from_uci(actual), board.legal_moves)
            self.assertIn(chess.Move.from_uci(best), board.legal_moves)
            packet = self.check_source(board, source_row([actual], [best]))
            self.assertNotEqual(packet["teaching"]["concept"], "choose_defended_square")
            self.assertNotIn("protection from the knight", packet["dialogue"])

    def test_escape_requires_leaving_that_attack_not_just_changing_square(self):
        initial = chess.Board("2r4k/8/8/8/2R5/8/P7/7K w - - 0 1")
        for board, played, best in ((initial, ["a2a3", "c8c4"], ["c4c5"]),
                                    (initial.mirror(), ["a7a6", "c1c5"], ["c5c4"])):
            packet = self.check_source(board, source_row(played, best, "blunder"))
            self.assertNotEqual(packet["teaching"]["concept"], "meet_piece_threat")
            self.assertNotRegex(packet["dialogue"], r"out of that|away from that|safe")

    def test_promoted_piece_name_is_used_for_protection(self):
        initial = chess.Board("k2r4/4PN2/8/8/8/8/8/7K w - - 0 1")
        for board, actual, best in ((initial, "e7e8r", "e7d8q"), (initial.mirror(), "e2e1r", "e2d1q")):
            packet = self.check_source(board, source_row([actual], [best]))
            self.assertEqual(packet["teaching"]["concept"], "choose_defended_square")
            self.assertIn("queen protection", packet["teaching"]["alternative"]["reason"])
            self.assertNotIn("pawn protection", packet["dialogue"])

    def test_blocking_check_takes_priority_over_describing_development(self):
        initial = chess.Board("k3r3/8/8/8/8/8/P7/4KB2 w - - 0 1")
        for board, actual, best in ((initial, "e1d1", "f1e2"), (initial.mirror(), "e8d8", "f8e7")):
            self.assertTrue(board.is_valid())
            self.assertTrue(board.is_check())
            self.assertIn(chess.Move.from_uci(actual), board.legal_moves)
            self.assertIn(chess.Move.from_uci(best), board.legal_moves)
            packet = self.check_source(board, source_row([actual], [best]))
            self.assertEqual(packet["teaching"]["concept"], "answer_check")
            self.assertIn("blocks the check", packet["teaching"]["alternative"]["reason"])
            self.assertNotIn("develop", packet["teaching"]["alternative"]["reason"])
            mover = board.turn
            board.push_uci(best)
            self.assertFalse(board.is_attacked_by(not mover, board.king(mover)))

    def test_avoiding_an_attacked_destination_requires_an_unattacked_original_square(self):
        fixtures = (
            ("6k1/8/8/4n3/8/2P5/8/3Q2K1 w - - 0 1", ["c3c4", "e5c4"], ["d1a4", "g8f7"], "e5", True),
            ("2r3k1/8/8/8/8/2P5/8/3Q2K1 w - - 0 1", ["c3c4", "c8c4"], ["d1a4", "g8f7"], "c8", False),
        )
        for fen, played, best, attacker, avoids in fixtures:
            for mirrored in (False, True):
                with self.subTest(avoids=avoids, mirrored=mirrored):
                    board = chess.Board(fen)
                    actual_pv, best_pv = list(played), list(best)
                    original, attack_square = chess.C3, chess.parse_square(attacker)
                    if mirrored:
                        board = board.mirror()
                        actual_pv, best_pv = list(map(mirror_uci, played)), list(map(mirror_uci, best))
                        original, attack_square = chess.square_mirror(original), chess.square_mirror(attack_square)
                    self.assertTrue(board.is_valid())
                    for pv in (actual_pv, best_pv):
                        position = board.copy(stack=True)
                        for uci in pv:
                            self.assertIn(chess.Move.from_uci(uci), position.legal_moves)
                            position.push_uci(uci)
                    position = board.copy(stack=True)
                    position.push_uci(best_pv[0])
                    self.assertEqual(original not in position.attacks(attack_square), avoids)
                    packet = self.check_source(board, source_row(actual_pv, best_pv, "mistake"))
                    lesson = packet["teaching"]
                    if avoids:
                        self.assertEqual(lesson["concept"], "avoid_moving_into_attack")
                        self.assertEqual(lesson["grounding"], "verified_move_effect")
                        self.assertIn("keeps", lesson["alternative"]["reason"])
                        self.assertIn(chess.square_name(original), lesson["alternative"]["reason"])
                        self.assertIn("out of that knight's attack", lesson["alternative"]["reason"])
                        self.assertIn("Pawns can't retreat", lesson["principle"])
                    else:
                        self.assertNotEqual(lesson["concept"], "avoid_moving_into_attack")
                        self.assertNotIn("out of that", lesson["alternative"]["reason"])
                        self.assertNotIn("Pawns can't retreat", lesson["principle"])

    def test_non_developing_move_explains_a_verified_threat_and_replies(self):
        initial = chess.Board("6k1/8/8/4n3/8/2P5/P7/3Q2K1 w - - 0 1")
        for board, actual, best, target, attacker_view, victim_view, victim_name in (
            (initial, "a2a3", "d1h5", "e5", "white", "black", "Black"),
            (initial.mirror(), "a7a6", "d8h4", "e4", "black", "white", "White"),
        ):
            self.assertTrue(board.is_valid())
            self.assertIn(chess.Move.from_uci(actual), board.legal_moves)
            self.assertIn(chess.Move.from_uci(best), board.legal_moves)
            position = board.copy(stack=True)
            position.push_uci(best)
            self.assertIn(chess.parse_square(target), position.attacks(chess.Move.from_uci(best).to_square))
            packet = self.check_source(board, source_row([actual], [best], "mistake"))
            lesson = packet["teaching"]
            self.assertEqual(lesson["concept"], "calculate_threats")
            self.assertEqual(lesson["grounding"], "verified_move_effect")
            self.assertIn(f"attacks {victim_name}'s knight on {target}", lesson["alternative"]["reason"])
            self.assertIn("opponent can answer", lesson["principle"])
            self.assertNotIn("develop", lesson["alternative"]["reason"])
            self.assertIn(f"{victim_name}'s knight", packet["perspectives"][attacker_view]["teaching"]["alternative"]["reason"])
            self.assertIn("your knight", packet["perspectives"][victim_view]["teaching"]["alternative"]["reason"])


if __name__ == "__main__":
    unittest.main()
