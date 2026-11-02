"""Finite boss opening seeds: legality, history isolation, and exit boundaries."""

import csv
import io
from pathlib import Path
import sys
import unittest

import chess
import chess.pgn

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import boss_openings as openings


def played(*moves):
    board = chess.Board()
    for uci in moves:
        board.push_uci(uci)
    return board


class BossOpeningTests(unittest.TestCase):
    def test_each_short_seed_replays_legally_and_matches_pinned_named_catalogue(self):
        directory = Path(__file__).resolve().parents[1] / "data/openings"
        catalog = set()
        for path in sorted(directory.glob("*.tsv")):
            with path.open(encoding="utf-8") as stream:
                for row in csv.DictReader(stream, delimiter="\t"):
                    catalog.add((row["name"], row["pgn"]))
        names_and_moves = {}
        for line in openings.repertoire():
            board = chess.Board()
            for uci in line["moves"]:
                self.assertIn(chess.Move.from_uci(uci), board.legal_moves, line["line_id"])
                board.push_uci(uci)
            self.assertLessEqual(len(line["moves"]), 8)
            self.assertEqual(board.turn, line["color"] != "white")
            names_and_moves[line["name"]] = tuple(line["moves"])
        for name, moves in names_and_moves.items():
            matching = [pgn for row_name, pgn in catalog if row_name == name]
            self.assertTrue(matching, name)
            self.assertTrue(any(tuple(move.uci() for move in
                                     chess.pgn.read_game(io.StringIO(pgn)).mainline_moves()) == moves
                                for pgn in matching), name)

    def test_signature_moves_and_no_scripted_continuation_after_each_gambit(self):
        for line in openings.repertoire():
            with self.subTest(line=line["line_id"]):
                board = played(*line["moves"][:-1])
                self.assertIn({"uci": line["moves"][-1], "name": line["name"],
                               "line_id": line["line_id"], "signature": True},
                              openings.proposals(board, "final"))
                board.push_uci(line["moves"][-1])
                # On the next turn of this gambit's owner, every legal reply
                # must lead back to normal search, including accurate defenses.
                for reply in list(board.legal_moves):
                    board.push(reply)
                    self.assertEqual(openings.proposals(board, "final"), [], line["line_id"])
                    board.pop()

    def test_only_intended_side_is_proposed_not_opponents_acceptance(self):
        board = played("e2e4", "e7e5", "d2d4")
        self.assertEqual(openings.proposals(board, "danish"), [])
        board = played("e2e4", "c7c5", "d2d4")
        self.assertEqual(openings.proposals(board, "morra"), [])
        board = played("d2d4")
        self.assertEqual({item["uci"] for item in openings.proposals(board, "black")},
                         {"e7e5", "g8f6", "d7d5"})
        board = played("e2e4", "e7e5", "g1f3")
        self.assertEqual({item["uci"] for item in openings.proposals(board, "black")},
                         {"g8f6", "f7f5", "d7d5", "b8c6"})
        flags = {item["uci"]: item["signature"] for item in openings.proposals(board, "black")}
        self.assertEqual(flags, {"g8f6": False, "f7f5": True, "d7d5": True, "b8c6": False})

    def test_opponent_deviations_return_to_engine(self):
        cases = [("e2e4", "e7e6"), ("d2d4", "d7d5", "g1f3"),
                 ("e2e4", "e7e5", "g1f3", "g8f6", "d2d4")]
        for moves in cases:
            self.assertEqual(openings.proposals(played(*moves), "deviation"), [])

    def test_custom_fen_endgame_lost_history_and_repetition_are_excluded(self):
        regular = played("d2d4")
        cases = [chess.Board(regular.fen()), chess.Board(chess960=True),
                 chess.Board("7k/8/8/8/8/8/8/KQ6 w - - 0 1"),
                 played("g1f3", "g8f6", "f3g1", "f6g8")]
        for board in cases:
            self.assertEqual(openings.proposals(board, "excluded"), [])

    def test_altered_counters_or_current_position_fail_history_replay(self):
        board = played("d2d4")
        board.halfmove_clock += 1
        self.assertEqual(openings.proposals(board, "altered"), [])
        board = played("d2d4")
        board.set_piece_at(chess.D4, None)
        self.assertEqual(openings.proposals(board, "altered"), [])
        null = chess.Board()
        null.push(chess.Move.null())
        self.assertEqual(openings.proposals(null, "null"), [])

    def test_order_is_deterministic_diverse_and_unique_without_board_mutation(self):
        for board in [chess.Board(), played("d2d4"), played("e2e4", "e7e5")]:
            before = board.fen(en_passant="fen"), tuple(board.move_stack)
            expected_moves = {item["uci"] for item in openings.proposals(board, "0")}
            first = set()
            for seed in range(512):
                choices = openings.proposals(board, str(seed))
                self.assertEqual(choices, openings.proposals(board.copy(stack=True), str(seed)))
                self.assertEqual({item["uci"] for item in choices}, expected_moves)
                self.assertEqual(len(choices), len(expected_moves))
                first.add(choices[0]["uci"])
            self.assertEqual(first, expected_moves)
            self.assertEqual((board.fen(en_passant="fen"), tuple(board.move_stack)), before)
        self.assertEqual({item["uci"] for item in openings.proposals(chess.Board(), "root")},
                         {"e2e4", "d2d4"})

    def test_tactical_candidates_have_unique_ids_and_explicit_signature_prefix(self):
        lines = openings.repertoire()
        self.assertEqual(len(lines), 19)
        self.assertEqual(len({line["line_id"] for line in lines}), len(lines))
        self.assertEqual(sum(line["color"] == "white" for line in lines), 9)
        for line in lines:
            self.assertEqual(line["prefix_to_signature"], line["moves"])

    def test_every_committed_line_keeps_its_identity_then_stops(self):
        import boss_play
        for line in openings.repertoire():
            with self.subTest(line=line["line_id"]):
                board, decisions = chess.Board(), []
                color = line["color"] == "white"
                for offset, uci in enumerate(line["moves"]):
                    if board.turn == color:
                        self.assertIn(uci, {row["uci"] for row in openings.proposals(board, "shared-prefix")})
                        if decisions:
                            plan = boss_play.plan(board, 1000, "shared-prefix", decisions=decisions)
                            self.assertTrue(plan["eligible"], plan["reason"])
                            self.assertEqual(plan["candidate_uci"], uci)
                            self.assertEqual(plan["line_id"], line["line_id"])
                        decisions.append({"ply": offset + 1, "line_id": line["line_id"],
                                          "selected_uci": uci, "candidate_uci": uci,
                                          "line_complete": offset + 1 == len(line["moves"])})
                    board.push_uci(uci)
                for reply in list(board.legal_moves):
                    board.push(reply)
                    plan = boss_play.plan(board, 1000, "shared-prefix", decisions=decisions)
                    self.assertNotEqual(plan["mode"], "opening")
                    self.assertEqual(plan["candidate_order"], [])
                    self.assertIsNone(plan["line_id"])
                    board.pop()

    def test_returned_metadata_and_proposals_cannot_modify_future_calls(self):
        before = openings.repertoire()
        changed = openings.repertoire()
        changed[0]["moves"].append("a1a8")
        changed[0]["name"] = "changed"
        self.assertEqual(openings.repertoire(), before)
        choices = openings.proposals(played("d2d4"), "same")
        original = [dict(item) for item in choices]
        choices[0]["uci"] = "a1a8"
        self.assertEqual(openings.proposals(played("d2d4"), "same"), original)


if __name__ == "__main__":
    unittest.main(verbosity=2)
