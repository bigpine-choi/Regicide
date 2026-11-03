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
        catalog = {}
        for path in sorted(directory.glob("*.tsv")):
            with path.open(encoding="utf-8") as stream:
                catalog[path.name] = {(row["name"], row["pgn"])
                                      for row in csv.DictReader(stream, delimiter="\t")}
        for line in openings.repertoire():
            board = chess.Board()
            for uci in line["moves"]:
                self.assertIn(chess.Move.from_uci(uci), board.legal_moves, line["line_id"])
                board.push_uci(uci)
            self.assertLessEqual(len(line["moves"]), 8)
            self.assertEqual(board.turn, line["color"] != "white")
            name = line["name"]
            source_file = line["source_url"].rsplit("/", 1)[-1]
            matching = [pgn for row_name, pgn in catalog[source_file] if row_name == name]
            self.assertTrue(matching, line["line_id"])
            source_moves = tuple(line["catalog_moves"])
            self.assertTrue(any(tuple(move.uci() for move in
                                     chess.pgn.read_game(io.StringIO(pgn)).mainline_moves()) == source_moves
                                for pgn in matching), name)
            if line["catalog_relation"] == "exact":
                self.assertEqual(tuple(line["moves"]), source_moves)
            else:
                self.assertEqual(line["catalog_relation"], "same_final_position")
                source_board = played(*source_moves)
                self.assertEqual(board.fen(en_passant="fen"), source_board.fen(en_passant="fen"))
                self.assertEqual(line["moves"][-1], source_moves[-1])
                board.pop()
                source_board.pop()
                self.assertEqual(board.fen(en_passant="fen"), source_board.fen(en_passant="fen"))

    def test_scandinavian_and_caro_replies_keep_a_catalogued_gambit_entry(self):
        tennison = openings.proposals(played("e2e4", "d7d5"), "reply")
        self.assertEqual(tennison[0], {"uci": "g1f3", "line_id": "tennison",
                                      "name": "Zukertort Opening: Tennison Gambit", "signature": True})
        for prefix, expected in [(("e2e4", "c7c6"), "d2d4"),
                                 (("d2d4", "c7c6"), "e2e4")]:
            board = played(*prefix)
            preparation = openings.proposals(board, "reply")
            self.assertEqual(preparation[0]["uci"], expected)
            self.assertFalse(preparation[0]["signature"])
            for move in (expected, "d7d5", "b1c3", "d5e4"):
                board.push_uci(move)
            signature = openings.proposals(board, "reply")
            self.assertEqual(signature[0]["uci"], "f1c4")
            self.assertTrue(signature[0]["signature"])

    def test_immediate_signatures_precede_preparation_and_keep_seeded_variety(self):
        board = played("d2d4")
        for seed in range(64):
            choices = openings.proposals(board, str(seed))
            self.assertEqual(choices[0]["uci"], "e7e5")
            self.assertTrue(choices[0]["signature"])
            self.assertTrue(all(not row["signature"] for row in choices[1:]))
        board = played("e2e4", "e7e5", "g1f3")
        first = set()
        for seed in range(128):
            choices = openings.proposals(board, str(seed))
            flags = [row["signature"] for row in choices]
            self.assertEqual(flags, sorted(flags, reverse=True))
            first.add(choices[0]["uci"])
        self.assertEqual(first, {"d7d5", "f7f5"})

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
            signatures = {item["uci"] for item in openings.proposals(board, "0") if item["signature"]}
            self.assertEqual(first, signatures or expected_moves)
            self.assertEqual((board.fen(en_passant="fen"), tuple(board.move_stack)), before)
        self.assertEqual({item["uci"] for item in openings.proposals(chess.Board(), "root")},
                         {"e2e4", "d2d4"})

    def test_tactical_candidates_have_unique_ids_and_explicit_signature_prefix(self):
        lines = openings.repertoire()
        self.assertEqual(len(lines), 22)
        self.assertEqual(len({line["name"] for line in lines}), 21)
        self.assertEqual(len({line["line_id"] for line in lines}), len(lines))
        self.assertEqual(sum(line["color"] == "white" for line in lines), 12)
        for line in lines:
            self.assertEqual(line["prefix_to_signature"], line["moves"])

    def test_every_authored_prefix_remains_available_then_stops_at_its_signature(self):
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
                            available = openings.proposals(board, "shared-prefix")
                            self.assertIn(plan["candidate_uci"], {row["uci"] for row in available})
                            self.assertIn(plan["line_id"], {row["line_id"] for row in available})
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
