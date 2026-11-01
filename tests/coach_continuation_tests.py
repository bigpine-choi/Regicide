"""Six-ply coaching uses legal stored lines, not invented search or game grades.

All positions and classifications are synthetic. These checks establish factual
wording, source isolation, and replay compatibility, not move strength.
"""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

import chess

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from coach_explanations import COACH_VERSION, describe_move
from coaching_session import CoachingSession, _line
from coaching_tests import Factory, FakeEngine, SHORT, packet, wait_for


EXCHANGE_FEN = "3q2k1/8/3b4/8/8/2P2N2/7P/3Q2K1 w - - 0 1"
EXCHANGE_PLAYED = "d1d3 d6h2 f3h2 d8d3 g1f2 g8f7".split()
EXCHANGE_BEST = "d1d4 d6h2 f3h2 d8d4 c3d4 g8f7".split()


def mirror_move(uci):
    move = chess.Move.from_uci(uci)
    return chess.Move(chess.square_mirror(move.from_square), chess.square_mirror(move.to_square),
                      promotion=move.promotion).uci()


def source_row(board, played, best, kind="blunder"):
    branches = {name: _line(board, packet(value, color=board.turn, pv=moves), "white")
                for name, value, moves in (("played", -150, played), ("best", 150, best))}
    if any(branch is None for branch in branches.values()):
        raise AssertionError("invalid synthetic line")
    return {"ply": len(board.move_stack) + 1, "uci": played[0], "san": "FORGED_SAN",
            "color": "wrong", "fen_before": board.fen(en_passant="fen"),
            "classification": kind, **branches}


class CoachContinuationTests(unittest.TestCase):
    def explain(self, board, row):
        previous = copy.deepcopy(row)
        fen, moves = board.fen(en_passant="fen"), tuple(board.move_stack)
        coach = describe_move(board, row)
        self.assertEqual(row, previous)
        self.assertEqual(board.fen(en_passant="fen"), fen)
        self.assertEqual(tuple(board.move_stack), moves)
        if coach is None:
            return None
        self.assertNotIn("FORGED", json.dumps(coach))
        for view in (coach, *coach["perspectives"].values()):
            continuation = view["continuation"]
            if continuation is None:
                continue
            count = continuation["available_plies"]
            self.assertIn(count, range(1, 7))
            self.assertEqual((continuation["line"], continuation["target_plies"]), ("best", 6))
            self.assertEqual(len(continuation["steps"]), count)
            self.assertEqual(continuation["complete"], count == 6 or continuation["terminal"])
            self.assertIn(view["teaching"]["alternative"]["reason"], continuation["intro"])
            self.assertIn(view["teaching"]["principle"], continuation["summary"])
            replay = board.copy(stack=True)
            for index, step in enumerate(continuation["steps"], 1):
                move = chess.Move.from_uci(step["uci"])
                self.assertIn(move, replay.legal_moves)
                self.assertEqual(step["ply_index"], index)
                self.assertEqual(step["san"], replay.san(move))
                self.assertEqual(step["color"], "white" if replay.turn else "black")
                self.assertEqual(step["uci"], row["best"]["pv_uci"][index - 1])
                self.assertLessEqual(len(step["text"].split()), 35)
                self.assertLessEqual(step["text"].count("."), 2)
                self.assertTrue(step["squares"])
                self.assertTrue(all(square in chess.SQUARE_NAMES for square in step["squares"]))
                replay.push(move)
                self.assertEqual(row["best"]["pv_frames"][index]["fen"], replay.fen(en_passant="fen"))
            self.assertEqual(continuation["terminal"], replay.is_game_over(claim_draw=False))
        return coach

    def exchange(self, mirrored=False):
        board = chess.Board(EXCHANGE_FEN)
        played, best = EXCHANGE_PLAYED, EXCHANGE_BEST
        if mirrored:
            board = board.mirror()
            played, best = [mirror_move(uci) for uci in played], [mirror_move(uci) for uci in best]
        return board, source_row(board, played, best)

    def test_six_ply_exchange_both_colors_has_steps_same_horizon_and_perspectives(self):
        for mirrored in (False, True):
            with self.subTest(mirrored=mirrored):
                board, row = self.exchange(mirrored)
                coach = self.explain(board, row)
                line = coach["continuation"]
                self.assertEqual((line["available_plies"], line["complete"], line["terminal"]), (6, True, False))
                self.assertIn("9 points better", line["summary"])
                self.assertIn("At 6 plies", line["summary"])
                self.assertIn("recapture", line["steps"][4]["text"])
                self.assertIn("queen", line["steps"][4]["text"])
                neutral = [{key: value for key, value in step.items() if key != "text"} for step in line["steps"]]
                for perspective, view in coach["perspectives"].items():
                    steps = view["continuation"]["steps"]
                    self.assertEqual([{key: value for key, value in step.items() if key != "text"} for step in steps], neutral)
                    self.assertIn("You recapture" if perspective == ("black" if mirrored else "white") else
                                  ("Black recaptures" if mirrored else "White recaptures"), steps[4]["text"])

    def test_only_first_six_plies_of_longer_pv_are_explained(self):
        board = chess.Board()
        played = "d2d4 d7d5 c2c4 e7e6 b1c3 g8f6".split()
        best = "e2e4 e7e5 g1f3 b8c6 f1c4 g8f6 d2d3 f8c5".split()
        coach = self.explain(board, source_row(board, played, best))
        self.assertEqual([step["uci"] for step in coach["continuation"]["steps"]], best[:6])
        self.assertIn("same material balance", coach["continuation"]["summary"])

    def test_short_line_is_not_padded_or_called_complete(self):
        board = chess.Board()
        coach = self.explain(board, source_row(board, ["e2e4"], ["d2d4", "d7d5"]))
        line = coach["continuation"]
        self.assertEqual((line["available_plies"], line["complete"], line["terminal"]), (2, False, False))
        self.assertIn("Only 2 plies", line["intro"])
        self.assertIn("No equal-length material comparison", line["summary"])

    def test_unequal_horizon_never_compares_material_using_distant_capture(self):
        board, row = self.exchange()
        row["played"] = _line(board, packet(-150, pv=EXCHANGE_PLAYED[:1]), "white")
        coach = self.explain(board, row)
        self.assertNotIn("better than", coach["continuation"]["summary"])
        self.assertIn("No equal-length material comparison", coach["continuation"]["summary"])

    def test_immediate_mate_ends_short_line_and_is_not_padded(self):
        board = chess.Board("7k/5K2/6Q1/8/8/8/8/8 w - - 0 1")
        coach = self.explain(board, source_row(board, ["g6g5", "h8h7"], ["g6g7"], "missed_mate"))
        line = coach["continuation"]
        self.assertEqual((line["available_plies"], line["complete"], line["terminal"]), (1, True, True))
        self.assertIn("ends the game", line["intro"])
        self.assertIn("checkmate for White", line["summary"])

    def test_longer_mating_pv_is_not_proof_of_forced_mate(self):
        board = chess.Board()
        board.push_uci("f2f3")
        coach = self.explain(board, source_row(board, ["a7a6"], ["e7e5", "g2g4", "d8h4"], "mistake"))
        line = coach["continuation"]
        self.assertEqual((line["available_plies"], line["terminal"]), (3, True))
        self.assertIn("doesn't establish a forced mate", line["summary"])

    def test_stalemate_and_insufficient_material_are_terminal_draws(self):
        fixtures = [
            ("7k/5K2/8/6Q1/8/8/8/8 w - - 0 1", ["g5e5"], ["g5g6"], "stalemate"),
            ("7k/8/8/8/8/8/Bb6/K7 w - - 0 1", ["a1b1"], ["a1b2"], "insufficient mating material"),
        ]
        for fen, played, best, reason in fixtures:
            with self.subTest(reason=reason):
                line = self.explain(chess.Board(fen), source_row(chess.Board(fen), played, best))["continuation"]
                self.assertTrue(line["complete"])
                self.assertTrue(line["terminal"])
                self.assertIn("draw by " + reason, line["summary"])

    def test_full_history_fivefold_and_claimable_draw_are_distinguished(self):
        board = chess.Board()
        cycle = "g1f3 g8f6 f3g1 f6g8".split()
        for move in cycle * 3 + cycle[:3]:
            board.push_uci(move)
        self.assertTrue(board.can_claim_threefold_repetition())
        row = source_row(board, ["f6e4"], ["f6g8"])
        line = self.explain(board, row)["continuation"]
        self.assertTrue(line["terminal"])
        self.assertIn("fivefold repetition", line["summary"])
        no_history = chess.Board(board.fen(en_passant="fen"))
        line = self.explain(no_history, source_row(no_history, ["f6e4"], ["f6g8"]))["continuation"]
        self.assertFalse(line["terminal"])
        self.assertFalse(line["complete"])

    def test_early_played_mate_is_compared_without_equal_horizon_material_claim(self):
        board = chess.Board()
        for move in ("f2f3", "e7e5"):
            board.push_uci(move)
        best = "g2g3 b8c6 g1h3 g8f6 h3f2 f8c5".split()
        line = self.explain(board, source_row(board, ["g2g4", "d8h4"], best, "allows_mate"))["continuation"]
        self.assertIn("checkmate for Black after 2 plies", line["summary"])
        self.assertIn("hasn't ended after 6 plies", line["summary"])
        self.assertNotIn("No equal-length", line["summary"])
        self.assertNotIn("safe", line["summary"])

    def test_surviving_queen_is_concrete_and_short_quiet_moves_do_not_repeat_advice(self):
        board = chess.Board("3q2k1/8/8/8/8/4P3/8/3Q2K1 w - - 0 1")
        played = "d1d4 d8g5 g1f2 g5f5 f2g3 f5g6".split()
        best = "d1d8 g8f7 e3e4 f7e6 d8d5 e6f6".split()
        coach = self.explain(board, source_row(board, played, best))
        line = coach["perspectives"]["white"]["continuation"]
        self.assertIn("your queen survives here while Black's queen is gone", line["summary"])
        self.assertIn("played line still has both queens", line["summary"])
        self.assertNotIn(coach["teaching"]["principle"], line["intro"])
        self.assertNotIn("Now check", " ".join(step["text"] for step in line["steps"]))
        self.assertLessEqual(len(line["summary"].split()), 40)

    def test_special_moves_are_grounded_in_exact_board_frames(self):
        fixtures = [
            ("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1", ["a1a2"], ["e1g1", "e8c8"], "castle", "f1"),
            ("7k/8/8/3pP3/8/8/8/K7 w - d6 0 1", ["e5e6"], ["e5d6", "h8g7"], "en passant", "d5"),
            ("k2r4/4PN2/8/8/8/8/8/7K w - - 0 1", ["e7e8r"], ["e7d8q", "a8b7"], "promote", "d8"),
        ]
        for fen, played, best, observation, square in fixtures:
            for mirrored in (False, True):
                with self.subTest(observation=observation, mirrored=mirrored):
                    board = chess.Board(fen)
                    if mirrored:
                        board = board.mirror()
                        played_line, best_line = [mirror_move(move) for move in played], [mirror_move(move) for move in best]
                        expected_square = chess.square_name(chess.square_mirror(chess.parse_square(square)))
                    else:
                        played_line, best_line, expected_square = played, best, square
                    line = self.explain(board, source_row(board, played_line, best_line))["continuation"]
                    self.assertIn(observation, line["steps"][0]["text"])
                    self.assertIn(expected_square, line["steps"][0]["squares"])
                    if observation == "promote":
                        self.assertIn("queen", line["steps"][0]["text"])

    def test_discovered_check_does_not_name_moved_piece_as_checker(self):
        board = chess.Board("k7/8/8/8/8/8/N7/R6K w - - 0 1")
        line = self.explain(board, source_row(board, ["h1g1"], ["a2c3", "a8b7"]))["continuation"]
        self.assertIn("That move gives check", line["steps"][0]["text"])
        self.assertNotIn("knight gives check", line["steps"][0]["text"])

    def test_countercapture_is_a_capture_not_escaping_attack(self):
        board = chess.Board("3q2k1/8/8/8/8/4P3/8/3Q2K1 w - - 0 1")
        line = self.explain(board, source_row(board, ["g1f1"], ["d1d4", "d8d4", "e3d4"]))["continuation"]
        self.assertIn("captures White's queen", line["steps"][1]["text"])
        self.assertNotIn("escape", line["steps"][1]["text"])
        self.assertIn("recaptures Black's queen", line["steps"][2]["text"])

    def test_pinned_defender_is_not_named_as_protection(self):
        board = chess.Board("4r3/7k/8/8/8/8/4N3/3QK3 w - - 0 1")
        line = self.explain(board, source_row(board, ["d1c2"], ["d1d4"]))["continuation"]
        self.assertTrue(board.is_pinned(chess.WHITE, chess.E2))
        self.assertNotIn("protected", line["steps"][0]["text"])
        self.assertNotIn("knight", line["steps"][0]["text"])

    def test_automatic_seventyfive_move_draw_ends_line(self):
        board = chess.Board("6k1/8/8/8/8/8/1R6/6K1 w - - 149 80")
        line = self.explain(board, source_row(board, ["b2b3"], ["b2b4"]))["continuation"]
        self.assertTrue(line["terminal"])
        self.assertTrue(line["complete"])
        self.assertIn("seventy-five-move rule", line["summary"])

    def test_noncritical_inexact_same_move_and_invalid_lines_never_offer_walkthrough(self):
        board, original = self.exchange()
        variations = []
        for kind in ("good", "best", "uncertain", "pending"):
            row = copy.deepcopy(original)
            row["classification"] = kind
            variations.append((kind, row))
        for branch in ("best", "played"):
            for bound in ("lower", "upper", None):
                row = copy.deepcopy(original)
                row[branch]["bound"] = bound
                variations.append((branch + str(bound), row))
        row = copy.deepcopy(original)
        row["best"] = copy.deepcopy(row["played"])
        variations.append(("same move", row))
        for branch in ("best", "played"):
            row = copy.deepcopy(original)
            row[branch]["pv_uci"].append("e1e8")
            variations.append((branch + " illegal tail", row))
            row = copy.deepcopy(original)
            row[branch]["uci"] = "a2a3"
            variations.append((branch + " wrong root", row))
        for label, row in variations:
            with self.subTest(label=label):
                coach = self.explain(board, row)
                if coach is not None:
                    self.assertIsNone(coach["continuation"])
                    self.assertTrue(all(view["continuation"] is None for view in coach["perspectives"].values()))

    def test_missing_or_corrupted_frames_and_root_fen_are_rejected(self):
        board, original = self.exchange()
        for kind in ("missing", "short", "root", "middle", "row"):
            row = copy.deepcopy(original)
            if kind == "missing":
                del row["best"]["pv_frames"]
            elif kind == "short":
                row["best"]["pv_frames"] = row["best"]["pv_frames"][:2]
            elif kind == "row":
                row["fen_before"] = chess.STARTING_FEN
            else:
                row["best"]["pv_frames"][0 if kind == "root" else 3]["fen"] = chess.STARTING_FEN
            with self.subTest(kind=kind):
                self.assertIsNone(self.explain(board, row)["continuation"])

    def test_stored_san_does_not_control_steps_and_retry_hint_stays_solution_free(self):
        board, row = self.exchange()
        row["best"]["san"] = "FORGED_BEST_SAN"
        row["best"]["pv_san"] = ["FORGED_PV_SAN"] * 6
        coach = self.explain(board, row)
        self.assertEqual(coach["continuation"]["steps"][0]["san"], "Qd4")
        for view in (coach, *coach["perspectives"].values()):
            for solution in ("Qd4", "d1d4", "d4"):
                self.assertNotIn(solution, view["hint"])

    def test_legacy_cache_regenerates_walkthrough_without_search_or_archive_rewrite(self):
        with tempfile.TemporaryDirectory(prefix="regicide-continuation-") as folder:
            root = Path(folder)
            binary = root / "fake.exe"
            binary.write_bytes(b"not executed")
            factory = Factory()
            best = "d2d4 d7d5 c2c4 e7e6 b1c3 g8f6".split()
            played = "e2e4 e7e5 g1f3 b8c6 f1c4 g8f6".split()
            factory.peers.append(FakeEngine([packet(150, pv=best), packet(-150, pv=played)]))
            session = CoachingSession(binary, "Synthetic", root / "games", factory)
            try:
                session.start(SHORT, "white", 500, session.revision)
                state = wait_for(lambda: session.snapshot() if session.snapshot()["status"] == "completed" else None)
                self.assertEqual(state["moves"][0]["coach"]["continuation"]["available_plies"], 6)
                path = next((root / "games/coaching").glob("*.json"))
                raw = json.loads(path.read_text(encoding="utf-8"))
                raw["report"]["coach"]["version"] = 4
                raw["report"]["moves"][0]["coach"] = {"continuation": {"steps": [{"uci": "e2e5", "text": "Invented fork"}]}}
                path.write_text(json.dumps(raw), encoding="utf-8")
                before = path.read_bytes()
                cached = session.start(SHORT, "white", 500, session.revision)
                self.assertTrue(cached["cached"])
                self.assertEqual(cached["coach"]["version"], COACH_VERSION)
                self.assertEqual(cached["moves"], state["moves"])
                self.assertEqual(factory.calls, 1)
                self.assertEqual(path.read_bytes(), before)
            finally:
                session.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
