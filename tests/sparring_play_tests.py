"""Adaptive practice evidence, candidate safeguards and late full-strength play."""
import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import chess
import chess.engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import sparring_play as policy

LINE = "e4 e5 Nf3 Nc6 Bc4 Bc5 d3 Nf6 Nc3 d6 O-O O-O h3 h6 a3 a6 Be3 Be6 Qd2 Qd7 Rae1 Rae8 Kh1 Kh8 Nh2 Nh7 f4 f5".split()


def quality_game(losses):
    board, rows = chess.Board(), []
    for index, loss in enumerate(losses):
        prefix = [move.uci() for move in board.move_stack]
        fen = board.fen(en_passant="fen")
        human = board.parse_san(LINE[2 * index])
        board.push(human)
        row = {"before_fen": board.fen(en_passant="fen"), "history_uci": [move.uci() for move in board.move_stack],
               "opponent_observation": {"valid": True, "forced": False, "ply": len(prefix) + 1,
                                        "history_before": prefix, "before_fen": fen, "played_uci": human.uci(),
                                        "loss_cp": loss, "depth": 6}}
        bot = board.parse_san(LINE[2 * index + 1])
        row["selected_uci"] = bot.uci()
        rows.append(row)
        board.push(bot)
    if 2 * len(losses) < len(LINE):
        board.push_san(LINE[2 * len(losses)])
    return board, rows


def packet(board, uci, cp=100, depth=6, **extra):
    return {"depth": depth, "score": chess.engine.PovScore(chess.engine.Cp(cp), board.turn),
            "pv": [chess.Move.from_uci(uci)], **extra}


class ProfileTests(unittest.TestCase):
    def test_no_evidence_or_one_blunder_does_not_create_a_weak_opponent(self):
        initial = policy.derive_profile(chess.Board(), [])
        self.assertEqual(initial["status"], "calibrating")
        self.assertEqual(initial["target_loss_cp"], 0)
        self.assertEqual(initial["confidence"], "low")
        board, rows = quality_game([2000])
        one = policy.derive_profile(board, rows)
        self.assertEqual(one["target_loss_cp"], 0)
        self.assertLessEqual(one["smoothed_loss_cp"] - initial["smoothed_loss_cp"], policy.MAX_PROFILE_STEP_CP)

    def test_repeated_errors_increase_concessions_and_accurate_moves_reduce_them(self):
        board, rows = quality_game([250] * 6)
        weak = policy.derive_profile(board, rows)
        self.assertGreater(weak["target_loss_cp"], 60)
        self.assertEqual(weak["status"], "adapting")
        board, rows = quality_game([250] * 6 + [0] * 7)
        improving = policy.derive_profile(board, rows)
        self.assertLess(improving["target_loss_cp"], weak["target_loss_cp"])
        self.assertEqual(len(improving["recent_observations"]), 12)
        board, rows = quality_game([0] * 6)
        strong = policy.derive_profile(board, rows)
        self.assertEqual(strong["target_loss_cp"], 0)
        self.assertGreater(strong["level"], weak["level"])

    def test_duplicates_dictionary_undo_and_new_game_reconstruct_only_actual_history(self):
        board, rows = quality_game([200] * 6)
        original = policy.derive_profile(board, rows)
        self.assertEqual(original, policy.derive_profile(board, rows + rows))
        self.assertEqual(original, policy.derive_profile(board, dict(enumerate(rows))))
        board.pop()
        board.pop()
        board.pop()
        undone = policy.derive_profile(board, rows)
        self.assertEqual(undone["observations"], 5)
        self.assertEqual(policy.derive_profile(chess.Board(), rows)["observations"], 0)

    def test_invalid_forced_stale_wrong_fen_and_conflicting_evidence_is_unknown(self):
        board, rows = quality_game([200])
        for change in ({"valid": False}, {"forced": True}, {"depth": 3}, {"loss_cp": float("nan")},
                       {"loss_cp": True}, {"before_fen": "wrong"}, {"ply": 2}, {"played_uci": "d2d4"}):
            modified = copy.deepcopy(rows)
            modified[0]["opponent_observation"].update(change)
            self.assertEqual(policy.derive_profile(board, modified)["observations"], 0, change)
        changed_reply = copy.deepcopy(rows)
        changed_reply[0]["selected_uci"] = "c7c5"
        self.assertEqual(policy.derive_profile(board, changed_reply)["observations"], 0)
        conflict = copy.deepcopy(rows[0])
        conflict["opponent_observation"]["loss_cp"] = 50
        self.assertEqual(policy.derive_profile(board, [*rows, conflict])["observations"], 0)

    def test_one_new_observation_has_bounded_effect_even_after_long_history(self):
        for count in range(1, 14):
            board, rows = quality_game([0] * (count - 1) + [300])
            before_board, before_rows = quality_game([0] * (count - 1))
            before = policy.derive_profile(before_board, before_rows)["smoothed_loss_cp"]
            current = policy.derive_profile(board, rows)["smoothed_loss_cp"]
            self.assertLessEqual(abs(current - before), policy.MAX_PROFILE_STEP_CP + .001)

    def test_provisional_observation_only_matches_the_actual_latest_human_move(self):
        board, rows = quality_game([200])
        board.pop()
        board.pop()
        pending = {"opponent_observation": rows[0]["opponent_observation"]}
        self.assertEqual(policy.derive_profile(board, [pending])["observations"], 1)
        board.pop()
        self.assertEqual(policy.derive_profile(board, [pending])["observations"], 0)

    def test_profile_exposes_skill_target_and_applies_ramp_exactly_once(self):
        board, rows = quality_game([250] * 6)
        sequence = ["g8h8", "g1h1", "h8g8", "h1g1"]
        index = 0
        while len(board.move_stack) < 120:
            board.push_uci(sequence[index % 4])
            index += 1
        profile = policy.derive_profile(board, rows)
        self.assertEqual(profile["phase"], "ramp")
        self.assertAlmostEqual(profile["target_loss_cp"], profile["skill_target_loss_cp"] / 2, places=2)
        self.assertAlmostEqual(policy._target(board, profile, 300)["target_loss_cp"], profile["target_loss_cp"], places=2)


class ScheduleAndNaturalnessTests(unittest.TestCase):
    def test_fifty_full_moves_then_twenty_move_ramp_without_material_shortcut(self):
        board = chess.Board()
        sequence = ["g1f3", "g8f6", "f3g1", "f6g8"]
        for ply in range(141):
            expected = "finish" if ply >= 140 else "ramp" if ply >= 100 else "calibration" if ply < 6 else "sparring"
            self.assertEqual(policy.phase(board), expected, ply)
            if ply < 140:
                board.push_uci(sequence[ply % 4])
        sparse = chess.Board("7k/8/8/8/8/8/8/KQ6 w - - 0 1")
        self.assertEqual(policy.phase(sparse), "calibration")

    def test_budget_and_phase_target_are_bounded_and_decline_smoothly(self):
        self.assertEqual(policy.extra_budget(599), 0)
        self.assertEqual(policy.extra_budget(600), 1200)
        self.assertEqual(policy.extra_budget(1000), 2000)
        self.assertEqual(policy.extra_budget(5000), 2000)
        self.assertEqual(policy.extra_budget(1000, ply=140), 0)
        board = chess.Board()
        profile = {"observations": 12, "target_loss_cp": 90}
        targets = []
        for ply in range(141):
            if ply in (99, 100, 110, 120, 130, 139, 140):
                targets.append(policy._target(board, profile, 300)["target_loss_cp"])
            if ply < 140:
                board.push_uci(["g1f3", "g8f6", "f3g1", "f6g8"][ply % 4])
        self.assertEqual(targets[:2], [90, 90])
        self.assertEqual(targets[-1], 0)
        self.assertTrue(all(a >= b for a, b in zip(targets, targets[1:])))

    def test_obvious_piece_donation_is_screened_but_normal_development_is_allowed(self):
        board = chess.Board("6k1/8/8/2p5/8/2P5/8/R2Q2K1 w - - 0 1")
        rejected = policy.naturalness(board, chess.Move.from_uci("d1d4"))
        self.assertFalse(rejected["admitted"])
        self.assertEqual(rejected["reason"], "obvious_material_donation")
        self.assertEqual(rejected["vulnerable_captures"][0]["net_material_cp"], -800)
        start = chess.Board()
        developed = policy.naturalness(start, chess.Move.from_uci("g1f3"))
        self.assertTrue(developed["admitted"])
        self.assertGreater(developed["style_points"], 0)
        self.assertEqual(start.fen(), chess.STARTING_FEN)

    def test_losing_positions_reduce_concessions_and_a_clear_advantage_keeps_a_floor(self):
        board, _ = quality_game([200] * 3)
        profile = {"observations": 12, "target_loss_cp": 90}
        self.assertEqual(policy._target(board, profile, -100)["target_loss_cp"], 0)
        self.assertLess(policy._target(board, profile, -30)["target_loss_cp"], 90)
        self.assertEqual(policy._target(board, profile, 50)["position_floor_cp"], 0)
        self.assertEqual(policy._target(board, profile, 300)["position_floor_cp"], 150)

    def test_immediate_opponent_mate_is_not_a_sparring_concession(self):
        board = chess.Board()
        board.push_san("f3")
        board.push_san("e5")
        result = policy.naturalness(board, chess.Move.from_uci("g2g4"))
        self.assertFalse(result["admitted"])
        self.assertEqual(result["reason"], "immediate_mate_reply")


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.board, self.rows = quality_game([250] * 6)
        self.legal = [move for move in sorted(self.board.legal_moves, key=lambda m: m.uci())
                      if policy.naturalness(self.board, move)["admitted"]]
        self.base = self.legal[0]
        self.alternative = self.legal[1]
        self.base_info = packet(self.board, self.base.uci(), cp=150)
        self.packets = {depth: packet(self.board, self.base.uci(), cp=150, depth=depth) for depth in (4, 5, 6)}
        self.calls = []
        self.closed = []

    def choose(self, *, cp=85, wrong_depth=False, malformed=False, mate=False, failing=False,
               reliability=None, forced=False, budget=1000, rows=None):
        def search(amount, *, root_move=None, root_moves=None, **options):
            self.calls.append((amount, root_move, root_moves, options))
            self.assertFalse(options["publish"])
            self.assertTrue(options["fresh"])
            if failing:
                raise TimeoutError("controlled")
            move = root_move or (self.alternative if self.alternative in root_moves else root_moves[0])
            depth = 3 if wrong_depth and root_move else options["depth"]
            value = cp if move == self.alternative or cp < 0 else 149
            info = packet(self.board, move.uci(), value, depth)
            if mate and root_move:
                info["score"] = chess.engine.PovScore(chess.engine.Mate(-2), self.board.turn)
            if malformed and root_move:
                info["upperbound"] = True
            return chess.engine.BestMove(move, None), info, {depth: info}
        invalid = {"valid": False, "forced": False, "reason": "test_unknown", "loss_cp": None}
        with patch.object(policy.boss_play, "observe_human_move", return_value=(invalid, False)):
            if reliability is not None:
                with patch.object(policy.evaluation_stability, "assess", return_value={"reliability": reliability}):
                    return policy.choose(self.board, self.base, self.base_info, self.packets, search,
                                         lambda: self.closed.append(True), decisions=self.rows if rows is None else rows,
                                         seed="seed", movetime_ms=budget, forced=forced)
            return policy.choose(self.board, self.base, self.base_info, self.packets, search,
                                 lambda: self.closed.append(True), decisions=self.rows if rows is None else rows,
                                 seed="seed", movetime_ms=budget, forced=forced)

    def test_verified_weak_profile_changes_the_move_without_changing_the_board(self):
        saved = self.board.copy(stack=True)
        result, discarded = self.choose()
        self.assertFalse(discarded)
        self.assertEqual(result["reason"], "accepted_sparring_alternative")
        self.assertEqual(result["selected_uci"], self.alternative.uci())
        self.assertEqual(result["loss_cp"], 65)
        self.assertEqual(result["compared_depth"], 6)
        self.assertEqual(result["baseline_budget_ms"], 1000)
        self.assertLessEqual(result["extra_budget_ms"], 2000)
        self.assertTrue(result["naturalness"]["admitted"])
        self.assertEqual(self.board.fen(), saved.fen())
        self.assertEqual(self.board.move_stack, saved.move_stack)
        self.assertTrue(all(row["line"]["depth"] == 3 for row in result["discovery"]))
        self.assertEqual(result["candidate_compared"]["depth"], 6)
        self.assertTrue(any(call[1] is not None and call[3]["depth"] == 6 for call in self.calls))

    def test_accurate_or_uncalibrated_profile_keeps_baseline(self):
        for losses in ([0] * 6, [250] * 2):
            board, rows = quality_game(losses)
            self.board, self.rows = board, rows
            self.base = sorted(board.legal_moves, key=lambda m: m.uci())[0]
            self.base_info = packet(board, self.base.uci())
            self.packets = {d: packet(board, self.base.uci(), depth=d) for d in (4, 5, 6)}
            result, _ = self.choose()
            self.assertEqual(result["selected_uci"], self.base.uci())
        self.assertEqual(self.calls, [])

    def test_unreliable_mate_bounded_shallow_and_excessive_loss_candidates_keep_baseline(self):
        for options in ({"reliability": "volatile"}, {"reliability": "provisional"},
                        {"wrong_depth": True}, {"malformed": True}, {"mate": True}, {"cp": -200}):
            result, _ = self.choose(**options)
            self.assertEqual(result["selected_uci"], self.base.uci(), options)
        self.base_info["score"] = chess.engine.PovScore(chess.engine.Mate(3), self.board.turn)
        result, _ = self.choose()
        self.assertEqual(result["reason"], "baseline_unreliable_or_mate")

    def test_forced_short_budget_finish_and_probe_failure_keep_full_baseline(self):
        for options, reason in (({"forced": True}, "user_forced_game"), ({"budget": 500}, "short_budget"),
                                ({"failing": True}, "candidate_search_failed")):
            result, discarded = self.choose(**options)
            self.assertEqual(result["selected_uci"], self.base.uci())
            self.assertEqual(result["reason"], reason)
            self.assertEqual(discarded, options.get("failing", False))
            self.assertEqual(result["profile"]["status"], "holding")
            self.assertEqual(result["profile"]["target_loss_cp"], 0)
        with patch.object(policy, "phase", return_value="finish"):
            result, _ = self.choose()
            self.assertEqual(result["reason"], "finish_full_strength")

    def test_selection_seed_is_reproducible_and_pgn_metadata_is_explicit(self):
        a, _ = self.choose()
        b, _ = self.choose()
        self.assertEqual(a["selected_uci"], b["selected_uci"])
        self.assertEqual(a["loss_cp"], b["loss_cp"])
        headers = policy.pgn_headers({"enabled": True, "policy": policy.POLICY, "seed": "ab" * 16})
        self.assertEqual(headers["SparringMode"], "1")
        self.assertEqual(headers["SparringSeed"], "ab" * 16)
        self.assertEqual(policy.pgn_headers({"enabled": 1, "policy": policy.POLICY}), {})


if __name__ == "__main__":
    unittest.main()
