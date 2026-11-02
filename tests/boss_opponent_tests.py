"""Observed move quality changes frequency; refusing a bait does not imply skill."""
import copy
from pathlib import Path
import sys
import unittest

import chess

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import boss_opponent as opponent

LINE = "e4 e5 Nf3 Nc6 Bc4 Bc5 d3 Nf6 Nc3 d6 O-O O-O h3 h6 a3 a6 Be3 Be6 Qd2 Qd7 Rae1 Rae8 Kh1 Kh8 Nh2 Nh7 f4 f5".split()


def quality_game(losses):
    board, rows = chess.Board(), []
    for index, loss in enumerate(losses):
        history = [move.uci() for move in board.move_stack]
        before_fen = board.fen(en_passant="fen")
        human = board.parse_san(LINE[2 * index])
        board.push(human)
        row = {"history_uci": [move.uci() for move in board.move_stack],
               "before_fen": board.fen(en_passant="fen"),
               "opponent_observation": {"valid": True, "forced": False, "ply": len(history) + 1,
                                        "history_before": history, "played_uci": human.uci(),
                                        "before_fen": before_fen, "loss_cp": loss, "depth": 5}}
        bot = board.parse_san(LINE[2 * index + 1])
        row["selected_uci"] = bot.uci()
        rows.append(row)
        board.push(bot)
    return board, rows


def trap_game(reply):
    board = chess.Board()
    for san in "e4 e5 Nf3 Nc6 Bc4 d6 Nc3 Bg4".split():
        board.push_san(san)
    row = {"history_uci": [move.uci() for move in board.move_stack],
           "before_fen": board.fen(en_passant="fen"), "selected_uci": "f3e5",
           "best_defense_pv_uci": ["f3e5", "c6e5"],
           "trap_evidence": {"verified": True, "uci": "f3e5", "capture_uci": "g4d1"}}
    board.push_uci("f3e5")
    board.push_uci(reply)
    return board, row


class BossOpponentTests(unittest.TestCase):
    def test_initial_strong_prior_then_errors_raise_and_good_moves_lower_frequency(self):
        initial = opponent.derive_profile(chess.Board(), [])
        self.assertAlmostEqual(initial["frequency"], .1)
        self.assertEqual(initial["quality_observations"], 0)
        board, rows = quality_game([300] * 6)
        weak = opponent.derive_profile(board, rows)
        self.assertGreater(weak["frequency"], initial["frequency"] + .3)
        board, rows = quality_game([300] * 6 + [0] * 8)
        recovered = opponent.derive_profile(board, rows)
        self.assertLess(recovered["frequency"], weak["frequency"])
        self.assertEqual(recovered["quality_observations"], 14)

    def test_duplicate_rows_undo_and_new_game_do_not_leak_observations(self):
        board, rows = quality_game([300, 300, 300])
        original = opponent.derive_profile(board, rows)
        self.assertEqual(original, opponent.derive_profile(board, rows + rows))
        board.pop()
        board.pop()
        undo = opponent.derive_profile(board, rows)
        self.assertEqual(undo["quality_observations"], 2)
        self.assertLess(undo["frequency"], original["frequency"])
        reset = opponent.derive_profile(chess.Board(), rows)
        self.assertEqual(reset["quality_observations"], 0)
        self.assertAlmostEqual(reset["frequency"], .1)

    def test_invalid_forced_shallow_wrong_history_and_nan_evidence_are_ignored(self):
        board, rows = quality_game([300])
        patches = ({"valid": False}, {"forced": True}, {"depth": 3}, {"loss_cp": float("nan")},
                   {"loss_cp": -10}, {"history_before": ["d2d4"]}, {"played_uci": "d2d4"},
                   {"before_fen": "wrong"}, {"ply": 2})
        for patch in patches:
            modified = copy.deepcopy(rows)
            modified[0]["opponent_observation"].update(patch)
            result = opponent.derive_profile(board, modified)
            self.assertEqual(result["quality_observations"], 0, patch)
            self.assertEqual(result["ignored_quality_observations"], 1)

    def test_current_provisional_observation_is_used_before_bot_decision_exists(self):
        board, rows = quality_game([300])
        board.pop()  # Human e4 exists, the engine has not committed a reply.
        provisional = {"opponent_observation": rows[0]["opponent_observation"]}
        profile = opponent.derive_profile(board, [provisional])
        self.assertEqual(profile["quality_observations"], 1)
        self.assertGreater(profile["frequency"], .1)
        board.pop()
        self.assertEqual(opponent.derive_profile(board, [provisional])["quality_observations"], 0)

    def test_taken_reference_and_unknown_are_separate_and_do_not_change_quality(self):
        for reply, expected in (("g4d1", "taken"), ("c6e5", "reference_defense"), ("g4e6", "unknown")):
            board, row = trap_game(reply)
            profile = opponent.derive_profile(board, [row, row])
            self.assertEqual(profile["trap_opportunities"], 1)
            self.assertEqual(profile["trap_outcomes"][expected], 1)
            self.assertEqual(profile["observed_take_fraction"], float(expected == "taken"))
            self.assertEqual(profile["quality_observations"], 0)
            self.assertAlmostEqual(profile["frequency"], .1)

    def test_only_actual_verified_selected_trap_with_real_reply_is_counted(self):
        board, row = trap_game("g4d1")
        for patch in ({"selected_uci": "a2a3"}, {"history_uci": []}, {"before_fen": "wrong"},
                      {"trap_evidence": {"verified": False}}):
            self.assertEqual(opponent.derive_profile(board, [{**row, **patch}])["trap_opportunities"], 0)
        board.pop()
        self.assertEqual(opponent.derive_profile(board, [row])["trap_opportunities"], 0)

    def test_gate_reproducibility_and_high_low_overrides(self):
        board, rows = quality_game([300, 300])
        saved = board.copy(stack=True)
        a = opponent.gate(board, rows, "seed-7")
        self.assertEqual(a, opponent.gate(saved, copy.deepcopy(rows), "seed-7"))
        strong_count, weak_count = 0, 0
        for seed in range(100):
            strong = opponent.gate(board, rows, seed, override={"error_indicator": 0})
            weak = opponent.gate(board, rows, seed, override={"error_indicator": 1})
            strong_count += strong["allow"]
            weak_count += weak["allow"]
            self.assertEqual(strong["draw"], weak["draw"])
            self.assertFalse(strong["allow"] and not weak["allow"])
        self.assertGreater(weak_count, strong_count + 50)
        self.assertFalse(opponent.gate(board, rows, "seed", override={"frequency": 0})["allow"])
        self.assertTrue(opponent.gate(board, rows, "seed", override={"frequency": 1})["allow"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
