"""Legal synthetic motif fixtures, not assertions about engine playing strength.

The mock classification chooses which existing line to explain. These tests
independently replay every move and check factual gates, perspective, references,
and preservation of the source game. No engine or application is started.
"""
import copy
import json
from pathlib import Path
import sys
import unittest

import chess

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from coach_strategy import analyze_strategy


FIXTURES = (
    ("fork", "8/3q1k2/8/8/8/5N2/8/6K1 w - - 0 1",
     "g1h1", "f3e5 f7e8 e5d7"),
    ("absolute_pin", "4k3/8/2n5/8/8/8/6P1/5BK1 w - - 0 1",
     "g2g3", "f1b5 e8d7 b5c6"),
    ("discovered_attack", "4q2k/8/8/8/8/8/P3B3/4R1K1 w - - 0 1",
     "a2a3", "e2d3 e8f8 e1e7"),
    ("rook_file", "6k1/p6p/8/8/8/8/P6P/R5K1 w - - 0 1",
     "a2a3", "a1e1 g8f7 e1e7"),
    ("passed_pawn", "6k1/p6p/8/2PP4/8/8/8/6K1 w - - 0 1",
     "g1h1", "d5d6 a7a6 d6d7"),
    ("outpost", "6k1/p6p/8/8/3P4/5N2/8/6K1 w - - 0 1",
     "g1h1", "f3e5 a7a6 e5d7"),
    ("king_activity", "6k1/p6p/8/8/8/4P3/P7/6K1 w - - 0 1",
     "a2a3", "g1f2 g8f7 f2e2"),
    ("development", "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2",
     "h2h3", "f1c4 g8f6 d2d3"),
    ("pawn_lever", "6k1/8/3p4/8/1PP5/8/P7/6K1 w - - 0 1",
     "a2a3", "c4c5 d6d5 c5c6"),
)


def mirror_move(uci):
    move = chess.Move.from_uci(uci)
    return chess.Move(chess.square_mirror(move.from_square), chess.square_mirror(move.to_square),
                      promotion=move.promotion).uci()


def frame(board):
    return {"fen": board.fen(en_passant="fen"), "ply": len(board.move_stack),
            "turn": "white" if board.turn else "black",
            "last_move": board.peek().uci() if board.move_stack else None,
            "board": {chess.square_name(square): piece.symbol() for square, piece in board.piece_map().items()}}


def branch(board, moves):
    replay = board.copy(stack=True)
    frames, sans = [frame(replay)], []
    for uci in moves:
        move = chess.Move.from_uci(uci)
        if move not in replay.legal_moves:
            raise AssertionError(f"illegal synthetic move: {uci} in {replay.fen()}")
        sans.append(replay.san(move))
        replay.push(move)
        frames.append(frame(replay))
    return {"uci": moves[0], "san": sans[0], "pv_uci": list(moves), "pv_san": sans,
            "pv_frames": frames, "bound": "exact", "depth": 8,
            "score_cp_white": 100, "mate_white": None, "winner": None}


def source_row(board, played, best, kind="mistake"):
    return {"ply": len(board.move_stack) + 1, "uci": played[0],
            "san": board.san(chess.Move.from_uci(played[0])),
            "color": "white" if board.turn else "black", "fen_before": board.fen(en_passant="fen"),
            "classification": kind, "played": branch(board, played), "best": branch(board, best)}


def fixture(value, mirrored=False):
    theme, fen, actual, pv = value
    board, played, best = chess.Board(fen), actual.split(), pv.split()
    if mirrored:
        board = board.mirror()
        played, best = [mirror_move(move) for move in played], [mirror_move(move) for move in best]
    if not board.is_valid():
        raise AssertionError(f"invalid synthetic board for {theme}: {board.status()}")
    return board, source_row(board, played, best)


class CoachStrategyTests(unittest.TestCase):
    def explain(self, board, row, perspective=None):
        saved = copy.deepcopy(row)
        fen, history = board.fen(en_passant="fen"), tuple(board.move_stack)
        result = analyze_strategy(board, row, perspective)
        self.assertEqual(row, saved)
        self.assertEqual(board.fen(en_passant="fen"), fen)
        self.assertEqual(tuple(board.move_stack), history)
        if result is None:
            return None
        self.assertIsInstance(result, dict)
        self.assertTrue(result["theme"])
        self.assertTrue(result["title"])
        self.assertTrue(result["summary"])
        self.assertLessEqual(len(result["summary"].split()), 40)
        self.assertLessEqual(result["summary"].count("."), 2)
        self.assertLessEqual(len(result["principle"].split()), 18)
        self.assertNotRegex(json.dumps(result, ensure_ascii=False), r"[가-힣]")
        self.assertNotIn("FORGED", json.dumps(result))
        expected_line = "played" if row["classification"] in ("best", "good") else "best"
        chosen = row[expected_line]
        sections = result["sections"]
        self.assertEqual([section["id"] for section in sections], ["idea", "plan", "counterplay"])
        for section in sections:
            self.assertTrue(section["title"])
            self.assertTrue(section["text"])
            self.assertLessEqual(len(section["text"].split()), 45)
        for note in result["step_notes"]:
            self.assertLessEqual(len(note["text"].split()), 35)
            self.assertLessEqual(note["text"].count("."), 2)
        for reference in [*sections, *result.get("evidence", []), *result["step_notes"]]:
            self.assertEqual(reference["line"], expected_line)
            self.assertIn(reference["ply_index"], range(1, len(chosen["pv_uci"]) + 1))
            self.assertIsInstance(reference["squares"], list)
            self.assertTrue(all(square in chess.SQUARE_NAMES for square in reference["squares"]))
            self.assertTrue(reference["text"])
        first_san = board.san(chess.Move.from_uci(chosen["pv_uci"][0]))
        self.assertIn(first_san, result["summary"])
        self.assertTrue(result["summary"].startswith("With " if expected_line == "played" else "Try "))
        return result

    def test_nine_motifs_and_color_mirrors_have_grounded_sections(self):
        for value in FIXTURES:
            for mirrored in (False, True):
                with self.subTest(theme=value[0], mirrored=mirrored):
                    board, row = fixture(value, mirrored)
                    for perspective in (None, "white", "black"):
                        result = self.explain(board, row, perspective)
                        self.assertIsNotNone(result)
                        self.assertEqual(result["theme"], value[0])

    def test_perspective_changes_words_without_changing_evidence(self):
        board, row = fixture(FIXTURES[0])
        results = [self.explain(board, row, color) for color in (None, "white", "black")]
        for result in results[1:]:
            self.assertEqual(result["theme"], results[0]["theme"])
            for key in ("sections", "evidence", "step_notes"):
                expected = [{k: item[k] for k in ("line", "ply_index", "squares")} for item in results[0].get(key, [])]
                actual = [{k: item[k] for k in ("line", "ply_index", "squares")} for item in result.get(key, [])]
                self.assertEqual(actual, expected)
        self.assertEqual(self.explain(board, row, chess.WHITE), results[1])
        self.assertEqual(self.explain(board, row, chess.BLACK), results[2])

    def test_fork_names_king_and_queen_and_plan_uses_actual_reply_and_followup(self):
        board, row = fixture(FIXTURES[0])
        result = self.explain(board, row)
        idea, plan, counterplay = result["sections"]
        self.assertIn("king", idea["text"].lower())
        self.assertIn("queen", idea["text"].lower())
        self.assertIn("Nxd7", plan["text"])
        self.assertIn("Ke8", counterplay["text"])
        self.assertEqual((plan["ply_index"], counterplay["ply_index"]), (3, 2))
        self.assertNotIn("wins a piece", result["summary"].lower())
        self.assertNotIn("guaranteed", json.dumps(result).lower())

    def test_positive_moves_explain_played_branch_without_requiring_best(self):
        for kind in ("good", "best"):
            board, critical = fixture(FIXTURES[0])
            row = source_row(board, critical["best"]["pv_uci"], critical["played"]["pv_uci"], kind)
            row["best"] = {"uci": "a1a8", "bound": "upper", "pv_uci": ["a1a8"]}
            result = self.explain(board, row)
            self.assertEqual(result["theme"], "fork")
            del row["best"]
            self.assertEqual(self.explain(board, row), result)

    def test_short_pv_never_invents_the_missing_reply_or_followup(self):
        board, row = fixture(FIXTURES[0])
        row["best"] = branch(board, row["best"]["pv_uci"][:1])
        result = self.explain(board, row)
        self.assertEqual(result["theme"], "fork")
        text = json.dumps(result)
        for unobserved_move in ("Ke8", "Nxd7", "f7e8", "e5d7"):
            self.assertNotIn(unobserved_move, text)

    def test_unknown_quiet_move_returns_none_without_generic_strategy_claim(self):
        board = chess.Board("6k1/p6p/8/8/8/3N4/8/K7 w - - 0 1")
        row = source_row(board, ["a1b1"], ["d3f2", "a7a6"])
        self.assertIsNone(self.explain(board, row))

    def test_pending_uncertain_and_untrusted_bounds_are_rejected(self):
        board, original = fixture(FIXTURES[0])
        for kind in ("pending", "uncertain", "unknown"):
            row = copy.deepcopy(original)
            row["classification"] = kind
            self.assertIsNone(self.explain(board, row))
        for name in ("played", "best"):
            for bound in (None, "upper", "lower", "lowerbound"):
                row = copy.deepcopy(original)
                row[name]["bound"] = bound
                with self.subTest(branch=name, bound=bound):
                    self.assertIsNone(self.explain(board, row))

    def test_wrong_actual_root_candidate_root_and_identical_alternative_are_rejected(self):
        board, original = fixture(FIXTURES[0])
        for change in ("actual", "played", "best", "same", "fen"):
            row = copy.deepcopy(original)
            if change == "actual":
                row["uci"] = "g1f1"
            elif change in ("played", "best"):
                row[change]["uci"] = "g1f1"
            elif change == "same":
                row["best"] = copy.deepcopy(row["played"])
            else:
                row["fen_before"] = chess.STARTING_FEN
            with self.subTest(change=change):
                self.assertIsNone(self.explain(board, row))

    def test_full_pv_is_validated_even_beyond_six_plies(self):
        board = chess.Board()
        board.push_uci("e2e4")
        board.push_uci("e7e5")
        row = source_row(board, ["h2h3"], "f1c4 g8f6 d2d3 b8c6 b1c3 f8c5 g1f3".split())
        self.assertIsNotNone(self.explain(board, row))
        row["best"]["pv_uci"].append("a1a8")
        self.assertIsNone(self.explain(board, row))

    def test_invalid_played_pv_rejects_otherwise_valid_alternative(self):
        board, row = fixture(FIXTURES[0])
        row["played"]["pv_uci"].append("a8a1")
        self.assertIsNone(self.explain(board, row))

    def test_malformed_missing_and_empty_branch_data_fail_closed(self):
        board, original = fixture(FIXTURES[0])
        for name in ("played", "best"):
            for replacement in (None, [], "not a line", {}, {"bound": "exact", "pv_uci": []},
                                {"bound": "exact", "uci": "f3e5", "pv_uci": "f3e5"},
                                {"bound": "exact", "uci": "f3e5", "pv_uci": [None]}):
                row = copy.deepcopy(original)
                row[name] = replacement
                with self.subTest(branch=name, replacement=replacement):
                    self.assertIsNone(self.explain(board, row))

    def test_frames_are_optional_but_supplied_wrong_or_truncated_frames_fail(self):
        board, original = fixture(FIXTURES[0])
        row = copy.deepcopy(original)
        for name in ("played", "best"):
            row[name].pop("pv_frames")
        self.assertIsNotNone(self.explain(board, row))
        for change in ("root", "middle", "tail", "short"):
            row = copy.deepcopy(original)
            if change == "short":
                row["best"]["pv_frames"].pop()
            else:
                index = {"root": 0, "middle": 1, "tail": 3}[change]
                row["best"]["pv_frames"][index]["fen"] = chess.STARTING_FEN
            with self.subTest(change=change):
                self.assertIsNone(self.explain(board, row))

    def test_stored_san_cannot_invent_a_tactical_claim(self):
        board, row = fixture(FIXTURES[0])
        expected = self.explain(board, row)
        row["san"] = "FORGED_ACTUAL_SAN"
        for name in ("played", "best"):
            row[name]["san"] = "FORGED_SAN"
            row[name]["pv_san"] = ["FORGED_PV_SAN"] * len(row[name]["pv_uci"])
        self.assertEqual(self.explain(board, row), expected)

    def test_pinned_rook_cannot_fork_queen_and_knight_off_pin_ray(self):
        for mirrored in (False, True):
            value = ("not_fork", "4r2k/8/8/8/q6n/8/4R3/4K3 w - - 0 1", "e1f1", "e2e4 h8g8")
            board, row = fixture(value, mirrored)
            after = board.copy(stack=True)
            after.push_uci(row["best"]["uci"])
            destination = chess.Move.from_uci(row["best"]["uci"]).to_square
            self.assertTrue(after.is_pinned(board.turn, destination))
            result = self.explain(board, row)
            if result:
                self.assertNotEqual(result["theme"], "fork")

    def test_relative_pin_to_queen_is_not_called_absolute_pin(self):
        for mirrored in (False, True):
            value = ("not_absolute", "4q2k/8/2n5/8/8/8/6P1/5BK1 w - - 0 1", "g2g3", "f1b5 h8g8")
            board, row = fixture(value, mirrored)
            result = self.explain(board, row)
            if result:
                self.assertNotEqual(result["theme"], "absolute_pin")

    def test_already_open_attack_is_not_a_discovered_attack(self):
        for mirrored in (False, True):
            value = ("not_discovery", "4q2k/8/8/8/8/8/P2B4/4R1K1 w - - 0 1", "a2a3", "d2e3 e8f8")
            board, row = fixture(value, mirrored)
            result = self.explain(board, row)
            if result:
                self.assertNotEqual(result["theme"], "discovered_attack")

    def test_own_pawn_blocks_open_rook_file_but_enemy_pawn_allows_semiopen_file(self):
        for mirrored in (False, True):
            for own in (True, False):
                fen = ("6k1/p6p/8/8/8/8/P3P2P/R5K1 w - - 0 1" if own else
                       "6k1/p6p/4p3/8/8/8/P6P/R5K1 w - - 0 1")
                board, row = fixture(("rook_file", fen, "a2a3", "a1e1 g8f7"), mirrored)
                result = self.explain(board, row)
                if own:
                    if result:
                        self.assertNotEqual(result["theme"], "rook_file")
                else:
                    self.assertIsNotNone(result)
                    self.assertEqual(result["theme"], "rook_file")
                    self.assertRegex(json.dumps(result).lower(), r"semi.?open")

    def test_adjacent_enemy_pawn_prevents_passed_pawn_claim(self):
        for mirrored in (False, True):
            value = ("not_passed", "6k1/p1p4p/8/2PP4/8/8/8/6K1 w - - 0 1", "g1h1", "d5d6 a7a6")
            board, row = fixture(value, mirrored)
            result = self.explain(board, row)
            if result:
                self.assertNotEqual(result["theme"], "passed_pawn")

    def test_enemy_pawn_can_advance_to_challenge_apparent_outpost(self):
        for mirrored in (False, True):
            value = ("not_outpost", "6k1/p4p1p/8/8/3P4/5N2/8/6K1 w - - 0 1", "g1h1", "f3e5 f7f6")
            board, row = fixture(value, mirrored)
            result = self.explain(board, row)
            if result:
                self.assertNotEqual(result["theme"], "outpost")

    def test_queens_on_board_prevent_queenless_king_activity_claim(self):
        for mirrored in (False, True):
            value = ("not_endgame", "1q4k1/p6p/8/8/8/4P3/P7/6K1 w - - 0 1", "a2a3", "g1f2 g8f7")
            board, row = fixture(value, mirrored)
            result = self.explain(board, row)
            if result:
                self.assertNotEqual(result["theme"], "king_activity")

    def test_pawn_contact_must_be_with_enemy_pawn(self):
        for mirrored in (False, True):
            value = ("not_lever", "6k1/8/3P4/8/1PP5/8/P7/6K1 w - - 0 1", "a2a3", "c4c5 g8f7")
            board, row = fixture(value, mirrored)
            result = self.explain(board, row)
            if result:
                self.assertNotEqual(result["theme"], "pawn_lever")

    def test_full_history_survives_analysis_with_repeated_position(self):
        board = chess.Board()
        for uci in "g1f3 g8f6 f3g1 f6g8 e2e4 e7e5".split():
            board.push_uci(uci)
        row = source_row(board, ["h2h3"], "f1c4 g8f6 d2d3".split())
        result = self.explain(board, row)
        self.assertEqual(result["theme"], "development")
        self.assertEqual(len(board.move_stack), 6)

    def test_pinned_rook_moving_along_ray_remains_pinned(self):
        value = ("absolute_pin", "4k3/4r3/8/8/8/8/P7/R5K1 w - - 0 1",
                 "a2a3", "a1e1 e7e6 e1e2")
        for mirrored in (False, True):
            board, row = fixture(value, mirrored)
            result = self.explain(board, row)
            self.assertEqual(result["theme"], "absolute_pin")
            self.assertIn("remains pinned", result["sections"][2]["text"])
            self.assertNotIn("removes this pin", result["sections"][2]["text"])
            self.assertIn("may", result["principle"])

    def test_capturing_discovered_attacker_is_not_escape_from_its_attack(self):
        value = ("discovered_attack", "4q2k/8/8/8/8/8/P3B3/4R1K1 w - - 0 1",
                 "a2a3", "e2d3 e8e1 g1h2")
        for mirrored in (False, True):
            board, row = fixture(value, mirrored)
            result = self.explain(board, row)
            self.assertEqual(result["theme"], "discovered_attack")
            self.assertIn("captures the rook behind the blocker", result["sections"][2]["text"])
            self.assertNotIn("moves the queen out", result["sections"][2]["text"])

    def test_opponent_can_capture_forking_piece(self):
        value = ("fork", "8/3q1k2/5p2/8/8/5N2/8/6K1 w - - 0 1",
                 "g1h1", "f3e5 f6e5 g1f2")
        for mirrored in (False, True):
            board, row = fixture(value, mirrored)
            result = self.explain(board, row)
            self.assertEqual(result["theme"], "fork")
            self.assertIn("captures the knight", result["sections"][2]["text"])
            self.assertNotIn("wins", result["summary"])

    def test_semiopen_pawn_behind_rook_is_not_called_ahead(self):
        value = ("rook_file", "6k1/p6p/R7/8/4p3/8/P6P/6K1 w - - 0 1",
                 "a2a3", "a6e6 g8f7 e6e7")
        for mirrored in (False, True):
            board, row = fixture(value, mirrored)
            result = self.explain(board, row)
            self.assertEqual(result["theme"], "rook_file")
            self.assertIn("enemy pawn still occupies", result["sections"][0]["text"])
            self.assertNotIn("ahead", result["sections"][0]["text"])

    def test_queenless_full_armies_are_not_called_endgame(self):
        value = ("not_ending", "rnb1kbnr/pppp1ppp/8/4p3/8/4P3/PPPP1PPP/RNB1KBNR w KQkq - 0 2",
                 "f1c4", "e1e2 g8f6 e2d3")
        for mirrored in (False, True):
            board, row = fixture(value, mirrored)
            result = self.explain(board, row)
            self.assertTrue(result is None or result["theme"] != "king_activity")

    def test_followup_new_support_answers_actual_knight_attack(self):
        for mirrored in (False, True):
            board, row = fixture(FIXTURES[7], mirrored)
            result = self.explain(board, row)
            text = result["sections"][1]["text"]
            square = "e5" if mirrored else "e4"
            self.assertIn("attacks the pawn on " + square, text)
            self.assertIn("adds pawn support", text)
            self.assertIn(square, result["sections"][1]["squares"])
            self.assertIn("adds pawn support", next(note["text"] for note in result["step_notes"] if note["ply_index"] == 3))

    def test_rook_rank_and_further_king_centralization_are_specific(self):
        board, row = fixture(FIXTURES[3])
        self.assertIn("seventh rank on e7", self.explain(board, row)["sections"][1]["text"])
        board, row = fixture(FIXTURES[3], True)
        self.assertIn("second rank on e2", self.explain(board, row)["sections"][1]["text"])
        board, row = fixture(FIXTURES[6])
        self.assertIn("another step closer to the center", self.explain(board, row)["sections"][1]["text"])

    def test_pinned_pawn_is_not_support_for_passed_pawn_or_outpost(self):
        values = (
            ("not_passed", "2r3k1/p6p/8/2PP4/8/8/8/2K5 w - - 0 1", "c1b1", "d5d6 a7a6", "passed_pawn"),
            ("not_outpost", "3r2k1/p6p/8/8/3P4/5N2/8/3K4 w - - 0 1", "d1c1", "f3e5 a7a6", "outpost"),
        )
        for label, fen, actual, pv, forbidden in values:
            for mirrored in (False, True):
                board, row = fixture((label, fen, actual, pv), mirrored)
                result = self.explain(board, row)
                self.assertTrue(result is None or result["theme"] != forbidden)

    def test_pawn_reply_creates_passed_pawn_and_later_push_is_explained(self):
        for mirrored in (False, True):
            board, row = fixture(FIXTURES[8], mirrored)
            result = self.explain(board, row)
            self.assertIn("sidesteps the capture but leaves", result["sections"][2]["text"])
            self.assertIn("-pawn passed", result["sections"][2]["text"])
            self.assertIn("advancing the passed pawn", result["sections"][1]["text"])

    def test_discovered_attack_followup_rook_rank_does_not_invent_pawn_targets(self):
        for mirrored in (False, True):
            board, row = fixture(FIXTURES[2], mirrored)
            result = self.explain(board, row)
            self.assertIn("rook onto the " + ("second" if mirrored else "seventh") + " rank", result["sections"][1]["text"])
            self.assertNotIn("attacking the pawn", result["sections"][1]["text"])

    def test_enemy_back_rank_is_not_an_undeveloped_home_square(self):
        value = ("not_development", "1N4k1/p6p/8/8/8/8/7P/6K1 w - - 0 1",
                 "h2h3", "b8c6 g8f7 c6e5")
        for mirrored in (False, True):
            board, row = fixture(value, mirrored)
            result = self.explain(board, row)
            self.assertTrue(result is None or result["theme"] != "development")

    def test_immediate_mate_or_automatic_draw_preserves_terminal_coaching(self):
        values = (
            ("mate", "7k/1R6/8/8/8/8/7P/R5K1 w - - 0 1", "h2h3", "a1a8"),
            ("draw", "6k1/p6p/8/8/8/8/P6P/R5K1 w - - 149 80", "a2a3", "a1e1"),
        )
        for value in values:
            for mirrored in (False, True):
                board, row = fixture(value, mirrored)
                end = board.copy(stack=True)
                end.push_uci(row["best"]["uci"])
                self.assertTrue(end.is_game_over(claim_draw=False))
                self.assertIsNone(self.explain(board, row))
                positive = source_row(board, row["best"]["pv_uci"], row["played"]["pv_uci"], "best")
                self.assertIsNone(self.explain(board, positive))

    def test_future_terminal_mate_or_draw_gets_no_general_step_note(self):
        values = (
            ("mate", "7k/1R6/8/8/8/8/7P/R5K1 w - - 0 1", "h2h3", "a1a6 h8g8 a6a8"),
            ("draw", "6k1/p6p/8/8/8/8/P6P/R5K1 w - - 147 80", "a2a3", "a1e1 g8f7 e1e7"),
        )
        for value in values:
            for mirrored in (False, True):
                board, row = fixture(value, mirrored)
                result = self.explain(board, row)
                self.assertIsNotNone(result)
                end = board.copy(stack=True)
                for move in row["best"]["pv_uci"]:
                    end.push_uci(move)
                self.assertTrue(end.is_game_over(claim_draw=False))
                self.assertTrue(all(note["ply_index"] < 3 for note in result["step_notes"]))
                plan = result["sections"][1]
                self.assertEqual(plan["ply_index"], 3)
                self.assertIn("ends this line", plan["text"])
                self.assertNotIn("depends on", plan["text"])
                if value[0] == "mate":
                    self.assertIn("checkmate for " + ("Black" if mirrored else "White"), plan["text"])
                    from coach_explanations import describe_move
                    coach = describe_move(board, row)
                    self.assertIn("checkmate", coach["continuation"]["steps"][2]["text"])
                else:
                    self.assertIn("draw by the seventy-five-move rule", plan["text"])

    def test_terminal_opponent_reply_explains_ending_not_counterplay(self):
        value = ("development", "rnbqkbnr/pppp1ppp/8/4p3/6P1/5P2/PPPPP2P/RNBQKBNR w KQkq - 0 3",
                 "a2a3", "b1c3 d8h4")
        for mirrored in (False, True):
            board, row = fixture(value, mirrored)
            result = self.explain(board, row)
            counter = result["sections"][2]
            self.assertEqual(counter["ply_index"], 2)
            self.assertIn("ends this line in checkmate for " + ("White" if mirrored else "Black"), counter["text"])
            self.assertNotIn("answer", counter["text"])
            self.assertNotIn("conditional", counter["text"])
            plan = result["sections"][1]
            self.assertEqual(plan["ply_index"], 2)
            self.assertIn("ends this line in checkmate", plan["text"])
            self.assertNotIn("next move", plan["text"])
            self.assertTrue(all(note["ply_index"] < 2 for note in result["step_notes"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
